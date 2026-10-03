"""Unit tests for DELETE /api/v1/proxy-keys/<key_id> (services/management/app/api/v1/proxy_keys.py).

Covers the management-side half of release-audit-2026-10-02 O7-a/O11's auth
cache: disabling the proxy's `api_keys` row and best-effort invalidating the
proxy's Valkey-backed auth-lookup cache entry for the same `key_id`.
"""

from datetime import datetime
from unittest.mock import MagicMock

from shared.auth.rbac import auth_cache_key


def make_mock_proxy_key(
    row_id: int = 1,
    key_id: str = "abc123",
    user_id: int = 1,
    org_id: int = 1,
    enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing an `api_keys` row (proxy auth table, not virtual_keys)."""
    key = MagicMock()
    key.id = row_id
    key.key_id = key_id
    key.user_id = user_id
    key.organization_id = org_id
    key.enabled = enabled
    key.name = "Test Proxy Key"
    key.last_used = None
    key.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return key


class TestRevokeProxyApiKey:
    """Tests for DELETE /api/v1/proxy-keys/<key_id>."""

    async def test_revoke_success_disables_and_invalidates(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Admin revokes a key: row gets disabled and the cache delete fires."""
        key = make_mock_proxy_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        import services.management.app.extensions as ext_mod

        mock_redis = MagicMock()
        monkeypatch.setattr(ext_mod, "redis_client", mock_redis)

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["message"]

        # The DB write is a soft-disable, mirroring keys.py's own revoke.
        app_mock_db.return_value.update.assert_called_once_with(enabled=False)
        # Best-effort Valkey invalidation uses the shared key-naming helper,
        # so management and the proxy can never drift on the cache key shape.
        mock_redis.delete.assert_called_once_with(auth_cache_key("abc123"))

    async def test_revoke_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Unknown key_id returns 404 and never touches the cache."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        import services.management.app.extensions as ext_mod

        mock_redis = MagicMock()
        monkeypatch.setattr(ext_mod, "redis_client", mock_redis)

        resp = await client.delete("/api/v1/proxy-keys/does-not-exist", headers=auth_headers)
        assert resp.status_code == 404
        mock_redis.delete.assert_not_called()

    async def test_revoke_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.delete("/api/v1/proxy-keys/abc123")
        assert resp.status_code == 401

    async def test_owner_can_revoke_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user may revoke their own key without any admin scope."""
        key = make_mock_proxy_key(user_id=2, org_id=1)  # user_auth_headers' subject is user_id=2
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_user_cannot_revoke_others_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user without apikey:delete cannot revoke another user's key."""
        key = make_mock_proxy_key(row_id=10, user_id=99, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_resource_manager_cannot_revoke_other_org_key(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A resource_manager without apikey:delete cannot revoke a key outside their own org."""
        key = make_mock_proxy_key(row_id=10, user_id=50, org_id=99)  # rm's org is 1
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_missing_cache_client_does_not_fail_the_request(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """No cache client configured: the DB write still succeeds (bounded by the TTL instead)."""
        key = make_mock_proxy_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        import services.management.app.extensions as ext_mod

        monkeypatch.setattr(ext_mod, "redis_client", None)

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=auth_headers)
        assert resp.status_code == 200

    async def test_cache_delete_failure_does_not_fail_the_request(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A Valkey error on invalidate is logged, not surfaced -- the revoke still succeeds."""
        key = make_mock_proxy_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        import services.management.app.extensions as ext_mod

        mock_redis = MagicMock()
        mock_redis.delete.side_effect = ConnectionError("valkey unreachable")
        monkeypatch.setattr(ext_mod, "redis_client", mock_redis)

        resp = await client.delete("/api/v1/proxy-keys/abc123", headers=auth_headers)
        assert resp.status_code == 200
