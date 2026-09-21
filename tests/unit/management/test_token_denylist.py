"""Unit tests for the JWT revocation denylist.

regression: audit-2026-09-14 (HIGH -- logout was a no-op, tokens had no
kill switch). Covers durable and in-process storage plus both degraded-mode
policies.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from services.management.app.services.token_denylist import (
    TokenDenylist,
    _LocalDenylist,
    get_token_denylist,
    reset_token_denylist,
)


class _FakeStore:
    """Minimal stand-in for penguin-aaa's TokenStore revocation surface."""

    def __init__(self) -> None:
        """Start empty, with no simulated failures."""
        self.revoked: dict[str, timedelta] = {}
        self.fail = False

    def add_revoked_jti(self, jti: str, ttl: timedelta) -> None:
        """Record *jti* as revoked for *ttl*."""
        if self.fail:
            raise ConnectionError("store down")
        self.revoked[jti] = ttl

    def is_jti_revoked(self, jti: str) -> bool:
        """Return whether *jti* is recorded as revoked."""
        if self.fail:
            raise ConnectionError("store down")
        return jti in self.revoked


def _in_an_hour() -> datetime:
    """Return a timestamp one hour from now, matching the default token exp."""
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.fixture
def store() -> _FakeStore:
    """Return a fresh fake revocation store."""
    return _FakeStore()


@pytest.fixture
def denylist(store: _FakeStore) -> TokenDenylist:
    """Return a denylist backed by the fake store, degrading open."""
    return TokenDenylist(store_provider=lambda: store, fail_closed=False)


class TestRevocation:
    """Core revoke/is_revoked behaviour."""

    def test_revoked_token_is_rejected(self, denylist: TokenDenylist) -> None:
        """A revoked jti reports as revoked afterwards."""
        assert denylist.is_revoked("jti-1") is False
        result = denylist.revoke("jti-1", _in_an_hour())
        assert (result.revoked, result.durable) == (True, True)
        assert denylist.is_revoked("jti-1") is True

    def test_unrelated_tokens_are_unaffected(self, denylist: TokenDenylist) -> None:
        """Revoking one jti leaves others alone."""
        denylist.revoke("jti-1", _in_an_hour())
        assert denylist.is_revoked("jti-2") is False

    def test_entry_ttl_matches_the_token_expiry(
        self, denylist: TokenDenylist, store: _FakeStore
    ) -> None:
        """The store entry expires with the token, so the denylist self-cleans."""
        denylist.revoke("jti-1", datetime.now(UTC) + timedelta(minutes=30))
        assert 1770 <= store.revoked["jti-1"].total_seconds() <= 1800

    def test_already_expired_token_is_not_stored(
        self, denylist: TokenDenylist, store: _FakeStore
    ) -> None:
        """Revoking a dead token is a no-op, never a zero/negative TTL write."""
        result = denylist.revoke("jti-old", datetime.now(UTC) - timedelta(minutes=1))
        assert (result.revoked, result.durable) == (True, True)
        assert store.revoked == {}

    def test_naive_expiry_is_treated_as_utc(self, denylist: TokenDenylist) -> None:
        """A tz-naive exp must not be read as local time and mis-TTL'd."""
        naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
        assert denylist.revoke("jti-naive", naive).revoked is True
        assert denylist.is_revoked("jti-naive") is True

    def test_missing_jti_is_never_revoked(self, denylist: TokenDenylist) -> None:
        """API-key auth carries no jti; refusing those would break a working path."""
        assert denylist.is_revoked(None) is False
        assert denylist.is_revoked("") is False
        assert denylist.revoke("", _in_an_hour()).revoked is False


class TestDegradedMode:
    """Behaviour when the shared revocation store is unreachable."""

    def test_write_falls_back_to_the_local_denylist(
        self, denylist: TokenDenylist, store: _FakeStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed durable write is still honoured by this process, and logged."""
        store.fail = True
        with caplog.at_level(logging.WARNING):
            result = denylist.revoke("jti-1", _in_an_hour())
        assert (result.revoked, result.durable) == (True, False)
        assert "shared revocation store unavailable" in caplog.text
        assert denylist.is_revoked("jti-1") is True

    def test_read_degrades_open_by_default(
        self, denylist: TokenDenylist, store: _FakeStore
    ) -> None:
        """An unreachable store must not reject every token in the product."""
        store.fail = True
        assert denylist.is_revoked("unknown-jti") is False

    def test_read_can_be_made_fail_closed(self, store: _FakeStore) -> None:
        """Operators who prefer the outage can opt into strict rejection."""
        strict = TokenDenylist(store_provider=lambda: store, fail_closed=True)
        store.fail = True
        assert strict.is_revoked("unknown-jti") is True

    def test_local_hit_short_circuits_a_dead_store(
        self, denylist: TokenDenylist, store: _FakeStore
    ) -> None:
        """A locally known revocation is honoured even with the store down."""
        denylist.revoke("jti-1", _in_an_hour())
        store.fail = True
        assert denylist.is_revoked("jti-1") is True

    def test_no_store_configured(self) -> None:
        """With no shared store the denylist still works in-process."""
        local_only = TokenDenylist(store_provider=lambda: None)
        result = local_only.revoke("jti-1", _in_an_hour())
        assert (result.revoked, result.durable) == (True, False)
        assert local_only.is_revoked("jti-1") is True
        assert local_only.is_revoked("jti-2") is False

    def test_degrade_warning_logged_once(
        self, denylist: TokenDenylist, store: _FakeStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Repeated degraded reads do not flood the log."""
        store.fail = True
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                denylist.is_revoked("jti-x")
        assert caplog.text.count("shared revocation store unavailable") == 1

    def test_fail_closed_default_comes_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOKEN_DENYLIST_FAIL_CLOSED flips the default read policy."""
        monkeypatch.setenv("TOKEN_DENYLIST_FAIL_CLOSED", "true")
        assert TokenDenylist().fail_closed is True
        monkeypatch.setenv("TOKEN_DENYLIST_FAIL_CLOSED", "no")
        assert TokenDenylist().fail_closed is False
        monkeypatch.delenv("TOKEN_DENYLIST_FAIL_CLOSED")
        assert TokenDenylist().fail_closed is False


class TestLocalDenylistBounds:
    """The in-process denylist must stay bounded and self-expiring."""

    def test_expired_entries_stop_matching(self) -> None:
        """An entry past the token's exp is dropped on the next lookup."""
        local = _LocalDenylist()
        local.add("jti-1", (datetime.now(UTC) - timedelta(seconds=1)).timestamp())
        assert local.contains("jti-1") is False

    def test_entries_are_capped(self) -> None:
        """A flood of revocations cannot grow the map without limit."""
        local = _LocalDenylist()
        future = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
        for i in range(_LocalDenylist._MAX_ENTRIES + 200):
            local.add(f"jti-{i}", future + i)
        assert len(local._entries) <= _LocalDenylist._MAX_ENTRIES

    def test_clear_empties_the_denylist(self) -> None:
        """clear() drops every entry."""
        local = _LocalDenylist()
        local.add("jti-1", (datetime.now(UTC) + timedelta(hours=1)).timestamp())
        local.clear()
        assert local.contains("jti-1") is False


class TestSingleton:
    """get_token_denylist()/reset_token_denylist()."""

    def test_returns_a_stable_instance(self) -> None:
        """Repeated calls hand back the same denylist."""
        reset_token_denylist()
        try:
            assert get_token_denylist() is get_token_denylist()
        finally:
            reset_token_denylist()

    def test_reset_installs_a_replacement(self) -> None:
        """Tests can swap in a denylist with a known backend."""
        replacement = TokenDenylist(store_provider=lambda: None)
        reset_token_denylist(replacement)
        try:
            assert get_token_denylist() is replacement
        finally:
            reset_token_denylist()
