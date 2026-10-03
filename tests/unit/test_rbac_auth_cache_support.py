"""Unit tests for the RBACManager/rbac.py additions backing the proxy's auth cache.

release-audit-2026-10-02 O7-a/O11: `authenticate_api_key` stays untouched (and
fully covered by tests/unit/test_rbac_additional.py); these are the new,
narrower primitives `proxy/apps/proxy_server/auth_cache.py` composes instead
of calling `authenticate_api_key` directly on every request.
"""

from unittest.mock import MagicMock

import pytest

from shared.auth.rbac import (
    AuthenticationError,
    Permission,
    RBACManager,
    Role,
    auth_cache_key,
    parse_wa_key_id,
)


@pytest.fixture
def mock_db():
    """Create a mock database connection."""
    return MagicMock()


@pytest.fixture
def rbac_manager(mock_db):
    """Create an RBACManager instance with mocked DB."""
    return RBACManager(mock_db)


class TestParseWaKeyId:
    """parse_wa_key_id: the key_id parser shared by authenticate_api_key and the auth cache."""

    def test_valid_key_extracts_key_id(self):
        """A well-shaped credential yields its key_id segment."""
        assert parse_wa_key_id("wa-abc123-somesecret") == "abc123"

    def test_secret_may_contain_dashes(self):
        """Only the key_id segment is split out; the secret may itself contain dashes."""
        assert parse_wa_key_id("wa-abc123-some-secret-with-dashes") == "abc123"

    def test_too_short_raises(self):
        """Fewer than 3 dash-separated segments is malformed."""
        with pytest.raises(AuthenticationError, match="Invalid API key format"):
            parse_wa_key_id("wa-short")

    def test_wrong_prefix_raises(self):
        """A credential not prefixed `wa-` is rejected."""
        with pytest.raises(AuthenticationError, match="Invalid API key format"):
            parse_wa_key_id("sk-abc123-somesecret")

    def test_empty_key_id_raises(self):
        """An empty key_id segment (`wa--secret`) is rejected."""
        with pytest.raises(AuthenticationError, match="Invalid API key format"):
            parse_wa_key_id("wa--somesecret")


class TestAuthCacheKey:
    """auth_cache_key: the shared Valkey key-naming helper (proxy writes, mgmt reads)."""

    def test_builds_prefixed_key(self):
        """The key_id is embedded verbatim behind a fixed, documented prefix."""
        assert auth_cache_key("abc123") == "waddleai:auth:apikey:abc123"

    def test_distinct_key_ids_never_collide(self):
        """Two different key_ids never produce the same cache key."""
        assert auth_cache_key("abc123") != auth_cache_key("abc124")


class TestFetchKeyAndUser:
    """RBACManager.fetch_key_and_user: the cache-populating lookup, split from authenticate_api_key.

    Performs no bcrypt check and no `last_used` write -- the caller
    (auth_cache.py) verifies the secret itself and debounces the write
    separately.
    """

    def test_unknown_key_id_returns_none(self, rbac_manager, mock_db):
        """An unknown key_id returns None (negative-cacheable), not an exception."""
        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = None
        mock_db.side_effect = [mock_key_lookup]

        assert rbac_manager.fetch_key_and_user("nonexistent") is None

    def test_known_key_id_returns_key_and_user(self, rbac_manager, mock_db):
        """A known, enabled key_id returns (key_record, user) with no write."""
        mock_key_record = MagicMock(id=100, user_id=5, key_hash="hashed")
        mock_user = MagicMock(id=5, username="api_user", enabled=True)

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = mock_user
        mock_db.side_effect = [mock_key_lookup, mock_user_lookup]

        result = rbac_manager.fetch_key_and_user("somekey")
        assert result == (mock_key_record, mock_user)
        # Exactly two DB calls (key lookup, user lookup) -- no third call for
        # a `last_used` write, unlike authenticate_api_key.
        assert mock_db.call_count == 2

    def test_disabled_user_raises(self, rbac_manager, mock_db):
        """A key_id whose owning user is disabled raises, is never negative-cached."""
        mock_key_record = MagicMock(id=1, user_id=5, key_hash="hashed")
        mock_user = MagicMock(enabled=False)

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = mock_user
        mock_db.side_effect = [mock_key_lookup, mock_user_lookup]

        with pytest.raises(AuthenticationError, match="API key user is disabled"):
            rbac_manager.fetch_key_and_user("somekey")

    def test_missing_user_raises(self, rbac_manager, mock_db):
        """A key_id whose owning user record is gone raises the same error as a disabled user."""
        mock_key_record = MagicMock(id=1, user_id=999, key_hash="hashed")

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = None
        mock_db.side_effect = [mock_key_lookup, mock_user_lookup]

        with pytest.raises(AuthenticationError, match="API key user is disabled"):
            rbac_manager.fetch_key_and_user("somekey")


class TestTouchApiKeyLastUsed:
    """RBACManager.touch_api_key_last_used: the debounced background write."""

    def test_updates_last_used_by_key_record_id(self, rbac_manager, mock_db):
        """Writes `last_used` for exactly the given key_record_id, nothing else."""
        mock_update_call = MagicMock()
        mock_db.side_effect = [mock_update_call]

        rbac_manager.touch_api_key_last_used(42)

        mock_db.assert_called_once()
        assert mock_update_call.update.call_count == 1
        assert "last_used" in mock_update_call.update.call_args.kwargs


class TestBuildUserContext:
    """RBACManager.build_user_context: assembles a UserContext with no DB access."""

    def test_builds_context_with_list_managed_orgs(self, rbac_manager):
        """A plain list of org ids passes through unchanged."""
        context = rbac_manager.build_user_context(
            user_id=5,
            username="api_user",
            role="admin",
            organization_id=1,
            managed_orgs=[2, 3],
            api_key_id=100,
        )
        assert context.user_id == 5
        assert context.username == "api_user"
        assert context.role == Role.ADMIN
        assert context.organization_id == 1
        assert context.managed_orgs == [2, 3]
        assert context.api_key_id == 100
        assert Permission.USER_CREATE in context.permissions

    def test_builds_context_with_no_managed_orgs(self, rbac_manager):
        """An empty/None managed_orgs normalizes to an empty list via _build_user_context."""
        context = rbac_manager.build_user_context(
            user_id=4,
            username="regular",
            role="user",
            organization_id=1,
            managed_orgs=None,
        )
        assert context.managed_orgs == []
        assert context.api_key_id is None

    def test_permissions_match_role_bundle(self, rbac_manager):
        """Permissions are the same ROLE_PERMISSIONS bundle _build_user_context would expand."""
        context = rbac_manager.build_user_context(
            user_id=3,
            username="reporter",
            role="reporter",
            organization_id=1,
            managed_orgs=[],
        )
        assert context.role == Role.REPORTER
        assert Permission.ANALYTICS_READ in context.permissions
        assert Permission.USER_CREATE not in context.permissions
