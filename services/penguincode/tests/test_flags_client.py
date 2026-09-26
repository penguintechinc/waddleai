"""Tests for the penguincode PostHog feature-flag client.

Covers the graceful-degradation contract mandated by the knowledge-platform
spec (docs/superpowers/specs/2026-09-25-penguincode-knowledge-platform-design.md
§10): on -> True, off -> False, PostHog outage falls back to the last-known
cached value, an outage with no cached value defaults OFF, and the client
never raises into the caller regardless of the backend failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from penguincode_cli.flags.client import (
    CODE_GRAPH_FLAG,
    KNOWLEDGE_GRAPH_FLAG,
    MEMORY_GRAPH_FLAG,
    RAG_FLAG,
    FlagClient,
    is_enabled,
)


@dataclass(slots=True, frozen=True)
class _FakeScopeContext:
    """Minimal stand-in satisfying the ScopeContextLike protocol."""

    tenant_id: str
    org_id: str | None
    team_ids: tuple[str, ...]
    user_id: str
    scopes: tuple[str, ...]


def _ctx(tenant_id: str = "tenant-1", user_id: str = "user-1") -> _FakeScopeContext:
    return _FakeScopeContext(
        tenant_id=tenant_id,
        org_id="org-1",
        team_ids=("team-1",),
        user_id=user_id,
        scopes=("rag:read",),
    )


@pytest.fixture(autouse=True)
def _clear_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no stray env override leaks between tests."""
    for name in (
        "POSTHOG_KEY",
        "POSTHOG_HOST",
        "PENGUINCODE_FLAG_RAG",
        "PENGUINCODE_FLAG_CODE_GRAPH",
        "PENGUINCODE_FLAG_KNOWLEDGE_GRAPH",
        "PENGUINCODE_FLAG_MEMORY_GRAPH",
    ):
        monkeypatch.delenv(name, raising=False)


class TestFlagKeys:
    """The four documented flag keys must match the spec exactly."""

    def test_flag_key_constants(self) -> None:
        assert RAG_FLAG == "penguincode.rag"
        assert CODE_GRAPH_FLAG == "penguincode.code-graph"
        assert KNOWLEDGE_GRAPH_FLAG == "penguincode.knowledge-graph"
        assert MEMORY_GRAPH_FLAG == "penguincode.memory-graph"


class TestNoBackendConfigured:
    """No POSTHOG_KEY at all -- a deliberate default, not an outage."""

    def test_never_seen_flag_defaults_off(self) -> None:
        client = FlagClient()
        assert client.is_enabled(RAG_FLAG, _ctx()) is False


class TestEnvOverride:
    """POSTHOG-free env overrides are used by tests/alpha environments."""

    def test_env_override_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        client = FlagClient()
        assert client.is_enabled(RAG_FLAG, _ctx()) is True

    def test_env_override_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "0")
        client = FlagClient()
        assert client.is_enabled(RAG_FLAG, _ctx()) is False


class TestPostHogResolved:
    """A configured, reachable PostHog client resolves definite values."""

    def test_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.return_value = True
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        assert client.is_enabled(RAG_FLAG, _ctx()) is True
        fake_posthog.feature_enabled.assert_called_once()

    def test_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.return_value = False
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        assert client.is_enabled(RAG_FLAG, _ctx()) is False

    def test_undefined_flag_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PostHog reachable but the flag itself is undefined -> None -> OFF."""
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.return_value = None
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        assert client.is_enabled(RAG_FLAG, _ctx()) is False

    def test_distinct_id_derived_from_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.return_value = True
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        client.is_enabled(RAG_FLAG, _ctx(tenant_id="tenant-42"))
        args, kwargs = fake_posthog.feature_enabled.call_args
        assert args[0] == RAG_FLAG
        assert args[1] == "tenant-42"


class TestOutageDegradation:
    """The graceful-degradation contract: outage -> last-known -> else OFF."""

    def test_outage_with_cached_value_returns_last_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        client = FlagClient()
        client._client_factory = lambda: fake_posthog

        fake_posthog.feature_enabled.return_value = True
        assert client.is_enabled(RAG_FLAG, _ctx()) is True

        fake_posthog.feature_enabled.side_effect = ConnectionError("posthog unreachable")
        assert client.is_enabled(RAG_FLAG, _ctx()) is True

    def test_outage_with_no_cache_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.side_effect = TimeoutError("posthog timeout")
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        assert client.is_enabled(RAG_FLAG, _ctx()) is False

    def test_outage_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.side_effect = RuntimeError("boom")
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        with caplog.at_level("WARNING"):
            client.is_enabled(RAG_FLAG, _ctx())
        assert any(record.levelname == "WARNING" for record in caplog.records)

    def test_never_raises_on_arbitrary_backend_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        fake_posthog.feature_enabled.side_effect = Exception("anything at all")
        client = FlagClient()
        client._client_factory = lambda: fake_posthog
        # Must not raise for any of the four documented flags.
        for key in (RAG_FLAG, CODE_GRAPH_FLAG, KNOWLEDGE_GRAPH_FLAG, MEMORY_GRAPH_FLAG):
            assert client.is_enabled(key, _ctx()) is False

    def test_client_construction_failure_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        client = FlagClient()

        def _boom() -> MagicMock:
            raise RuntimeError("cannot construct posthog client")

        client._client_factory = _boom
        assert client.is_enabled(RAG_FLAG, _ctx()) is False

    def test_unexpected_exception_outside_resolve_raw_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer backstop in ``is_enabled`` -- not just the inner PostHog

        try/except -- must also swallow a failure, e.g. a ``ctx`` attribute
        access blowing up while building person_properties.
        """
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")

        class _ExplodingCtx:
            tenant_id = "tenant-1"
            org_id = "org-1"
            team_ids = ("team-1",)
            scopes = ("rag:read",)

            @property
            def user_id(self) -> str:
                raise RuntimeError("boom before feature_enabled is ever called")

        client = FlagClient()
        client._client_factory = lambda: MagicMock()
        assert client.is_enabled(RAG_FLAG, _ExplodingCtx()) is False


class TestDefaultClientFactory:
    """The real (non-test-double) PostHog client construction path."""

    def test_builds_a_real_posthog_client_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        monkeypatch.setenv("POSTHOG_HOST", "https://posthog.invalid")
        from posthog import Posthog

        client = FlagClient()._default_client_factory()
        assert isinstance(client, Posthog)
        client.shutdown()  # avoid leaking the background consumer thread


class TestPerCacheKeyIsolation:
    """Cached values are keyed per (flag, distinct_id) -- no cross-tenant bleed."""

    def test_cache_is_isolated_per_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTHOG_KEY", "phc_test_key")
        fake_posthog = MagicMock()
        client = FlagClient()
        client._client_factory = lambda: fake_posthog

        fake_posthog.feature_enabled.return_value = True
        assert client.is_enabled(RAG_FLAG, _ctx(tenant_id="tenant-a")) is True

        # A different tenant has never resolved -- an outage here must not
        # inherit tenant-a's cached True.
        fake_posthog.feature_enabled.side_effect = ConnectionError("down")
        assert client.is_enabled(RAG_FLAG, _ctx(tenant_id="tenant-b")) is False


class TestModuleLevelSingleton:
    """`flags.client.is_enabled(key, ctx)` -- the exact Shared-Contracts call shape."""

    def test_module_function_defaults_off_when_unconfigured(self) -> None:
        assert is_enabled(RAG_FLAG, _ctx()) is False
