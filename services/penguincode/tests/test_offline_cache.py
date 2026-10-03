"""Tests for `client/offline_cache.py` -- the O8 scope-safe local read cache.

Covers: hit (fresh and stale), miss (never written, undecodable token), write-then-read
round trip, and -- the load-bearing property -- that two different tokens (different
tenant/user claims) NEVER share a cache entry even when they use the identical namespace
and key, proving scope isolation at the cache layer.

# regression: ops-audit O8 (CLI resilience -- offline read cache, scope-safe)
"""

from __future__ import annotations

from pathlib import Path

import jwt
import pytest

from penguincode_cli.client.offline_cache import OfflineCache, scope_key_from_token

_SIGNING_KEY = "test-signing-key-at-least-32-bytes-long-for-hs256"


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_OFFLINE_CACHE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)


def _token(tenant: str = "tenant-a", sub: str = "user-1") -> str:
    return jwt.encode({"tenant": tenant, "sub": sub}, _SIGNING_KEY, algorithm="HS256")


class TestScopeKeyFromToken:
    def test_same_claims_produce_same_key(self) -> None:
        assert scope_key_from_token(_token()) == scope_key_from_token(_token())

    def test_different_tenant_produces_different_key(self) -> None:
        assert scope_key_from_token(_token(tenant="tenant-a")) != scope_key_from_token(
            _token(tenant="tenant-b")
        )

    def test_different_user_produces_different_key(self) -> None:
        assert scope_key_from_token(_token(sub="user-1")) != scope_key_from_token(
            _token(sub="user-2")
        )

    def test_malformed_token_returns_none(self) -> None:
        assert scope_key_from_token("not-a-jwt-at-all") is None

    def test_token_missing_tenant_claim_returns_none(self) -> None:
        token = jwt.encode({"sub": "user-1"}, _SIGNING_KEY, algorithm="HS256")
        assert scope_key_from_token(token) is None

    def test_token_missing_sub_claim_returns_none(self) -> None:
        token = jwt.encode({"tenant": "tenant-a"}, _SIGNING_KEY, algorithm="HS256")
        assert scope_key_from_token(token) is None

    def test_key_never_contains_raw_claims(self) -> None:
        key = scope_key_from_token(_token(tenant="super-secret-tenant", sub="alice"))
        assert key is not None
        assert "super-secret-tenant" not in key
        assert "alice" not in key


class TestGetSetRoundTrip:
    def test_miss_when_never_written(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        assert cache.get(token=_token(), namespace="query", key="hello") is None

    def test_write_then_read_hits(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="hello", value={"hits": [1, 2, 3]})

        entry = cache.get(token=_token(), namespace="query", key="hello")
        assert entry is not None
        assert entry.value == {"hits": [1, 2, 3]}
        assert entry.is_stale is False

    def test_different_namespace_is_a_miss(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="hello", value={"hits": []})
        assert cache.get(token=_token(), namespace="index_status", key="hello") is None

    def test_different_key_is_a_miss(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="hello", value={"hits": []})
        assert cache.get(token=_token(), namespace="query", key="world") is None

    def test_set_with_undecodable_token_is_a_noop(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token="garbage", namespace="query", key="hello", value={"hits": []})
        assert cache.get(token=_token(), namespace="query", key="hello") is None
        assert not any(tmp_path.iterdir())  # nothing was written to disk at all

    def test_get_with_undecodable_token_is_none(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="hello", value={"hits": []})
        assert cache.get(token="garbage", namespace="query", key="hello") is None

    def test_corrupt_cache_file_is_treated_as_a_miss(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        token = _token()
        cache.set(token=token, namespace="query", key="hello", value={"hits": []})

        scope_key = scope_key_from_token(token)
        assert scope_key is not None
        for path in (tmp_path / scope_key / "query").iterdir():
            path.write_text("not valid json {{{", encoding="utf-8")

        assert cache.get(token=token, namespace="query", key="hello") is None


class TestStaleness:
    def test_fresh_entry_is_not_stale(self, tmp_path: Path) -> None:
        now = [1000.0]
        cache = OfflineCache(cache_dir=str(tmp_path), ttl_seconds=60.0, clock=lambda: now[0])
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})

        now[0] += 10.0  # well within the 60s TTL
        entry = cache.get(token=_token(), namespace="query", key="q")
        assert entry is not None
        assert entry.is_stale is False

    def test_entry_older_than_ttl_is_stale_but_still_returned(self, tmp_path: Path) -> None:
        now = [1000.0]
        cache = OfflineCache(cache_dir=str(tmp_path), ttl_seconds=60.0, clock=lambda: now[0])
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})

        now[0] += 120.0  # past the 60s TTL
        entry = cache.get(token=_token(), namespace="query", key="q")
        assert entry is not None  # stale, not discarded -- degraded reads are the point
        assert entry.is_stale is True
        assert entry.age_seconds(now=now[0]) == 120.0


class TestKillSwitch:
    """`penguincode.disable-offline-cache` -- unseen/OFF = cache ON (default); ON =
    legacy no-cache behavior.
    """

    def test_disabled_set_is_a_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_OFFLINE_CACHE", "true")
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})
        assert not any(tmp_path.iterdir())

    def test_disabled_get_is_always_a_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})

        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_OFFLINE_CACHE", "true")
        assert cache.get(token=_token(), namespace="query", key="q") is None

    def test_enabled_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_OFFLINE_CACHE", raising=False)
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})
        assert cache.get(token=_token(), namespace="query", key="q") is not None


class TestScopeIsolation:
    """The load-bearing property: tenant/user A's cached read is never visible to B."""

    def test_different_tenants_never_share_a_cache_entry(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        token_a = _token(tenant="tenant-a", sub="user-1")
        token_b = _token(tenant="tenant-b", sub="user-1")

        cache.set(token=token_a, namespace="query", key="shared-query-text", value={"hits": ["A"]})

        assert cache.get(token=token_b, namespace="query", key="shared-query-text") is None
        entry_a = cache.get(token=token_a, namespace="query", key="shared-query-text")
        assert entry_a is not None
        assert entry_a.value == {"hits": ["A"]}

    def test_different_users_same_tenant_never_share_a_cache_entry(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        token_a = _token(tenant="tenant-a", sub="user-1")
        token_b = _token(tenant="tenant-a", sub="user-2")

        cache.set(token=token_a, namespace="query", key="shared-query-text", value={"hits": ["A"]})

        assert cache.get(token=token_b, namespace="query", key="shared-query-text") is None

    def test_cache_files_are_owner_only(self, tmp_path: Path) -> None:
        cache = OfflineCache(cache_dir=str(tmp_path))
        cache.set(token=_token(), namespace="query", key="q", value={"a": 1})

        for path in tmp_path.rglob("*.json"):
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600
