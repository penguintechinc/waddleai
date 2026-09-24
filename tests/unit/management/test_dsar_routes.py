"""DSAR (data-subject-rights) route tests: export + erasure.

Covers the release-audit G2 remediation: a real, non-tier-gated right of access
(GDPR Art. 15) and right to erasure (GDPR Art. 17) on the ``users`` identity
table. Every test is marked ``regression: release-audit-2026-09-23``.

Properties proven here:
  * a user can EXPORT their own data, and CANNOT export another's (403) unless
    org-admin/admin scoped;
  * a user can ERASE their own account -- PII is anonymized in place (tombstone),
    the row is retained (PII-tokenization) and the account disabled;
  * a user CANNOT erase another user (403) unless org-admin/admin scoped, and an
    org-admin may not erase an admin account;
  * both rights work for a plain user with NO enterprise entitlement -- there is
    no licence/tier gate anywhere on the path.
"""

from unittest.mock import MagicMock

from services.management.app import dsar
from tests.unit.management.route_conftest import make_mock_user

# ---------------------------------------------------------------------------
# GET /api/v1/users/<id>/export  (right of access)
# ---------------------------------------------------------------------------


class TestExportUserData:
    """Tests for GET /api/v1/users/<user_id>/export."""

    async def test_export_own_data(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user can export their own record → 200 with identity + manifest.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=2, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/2/export", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["subject_user_id"] == 2
        assert data["identity"]["email"] == "admin@example.com"
        assert isinstance(data["manifest"], list) and len(data["manifest"]) >= 1

    async def test_export_no_auth(self, client) -> None:
        """Missing auth returns 401.

        regression: release-audit-2026-09-23
        """
        resp = await client.get("/api/v1/users/2/export")
        assert resp.status_code == 401

    async def test_export_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing user returns 404.

        regression: release-audit-2026-09-23
        """
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/users/999/export", headers=auth_headers)
        assert resp.status_code == 404

    async def test_export_other_user_denied_for_plain_user(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user (no org-admin scope) cannot export another user → 403.

        regression: release-audit-2026-09-23
        """
        other = make_mock_user(user_id=99, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = other

        resp = await client.get("/api/v1/users/99/export", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_export_org_admin_same_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """An org-admin (USER_MANAGE) can export a user in its own org → 200.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=5, org_id=1, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/5/export", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_export_org_admin_other_org_denied(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """An org-admin cannot export a user in another org → 403.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=10, org_id=99, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/10/export", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_export_admin_any_user(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An admin (USER_CREATE) can export any user in any org → 200.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=77, org_id=42, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/77/export", headers=auth_headers)
        assert resp.status_code == 200

    async def test_export_never_leaks_password_hash(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """The exported identity field set is exact and excludes the password hash.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=2, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/2/export", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "password_hash" not in data["identity"]
        assert set(data.keys()) == {
            "subject_user_id",
            "generated_at",
            "identity",
            "manifest",
            "notice",
        }
        assert set(data["identity"].keys()) == {
            "id",
            "username",
            "email",
            "role",
            "organization_id",
            "enabled",
            "default_model",
            "token_quota_daily",
            "token_quota_monthly",
            "created_at",
            "last_login_at",
            "current_login_at",
            "last_login_ip",
            "current_login_ip",
            "login_count",
        }

    async def test_export_not_tier_gated(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user with NO enterprise entitlement can still export → 200.

        The test app configures no licence/tier entitlement at all; a plain user
        succeeding proves the path is statutory (authn + ownership only) and not
        behind any licence gate.
        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=2, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/2/export", headers=user_auth_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/users/<id>/erase  (right to erasure)
# ---------------------------------------------------------------------------


class TestEraseUserData:
    """Tests for POST /api/v1/users/<user_id>/erase."""

    async def test_erase_own_account(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user can erase their own account → 200, PII anonymized in place.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=2, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/2/erase", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["erased"] is True
        assert data["user_id"] == 2

        # Anonymization actually written: tombstone PII + disabled, row retained.
        app_mock_db.return_value.update.assert_called_once()
        _args, kwargs = app_mock_db.return_value.update.call_args
        assert kwargs["username"] == "erased-user-2"
        assert kwargs["email"] == "erased-2@deleted.invalid"
        assert kwargs["password_hash"] == dsar.ERASED_PASSWORD_SENTINEL
        assert kwargs["last_login_ip"] is None
        assert kwargs["current_login_ip"] is None
        assert kwargs["enabled"] is False
        app_mock_db.return_value.commit.assert_not_called()  # closure commits on db, not query
        app_mock_db.commit.assert_called_once()

    async def test_erase_no_auth(self, client) -> None:
        """Missing auth returns 401.

        regression: release-audit-2026-09-23
        """
        resp = await client.post("/api/v1/users/2/erase")
        assert resp.status_code == 401

    async def test_erase_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing user returns 404.

        regression: release-audit-2026-09-23
        """
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post("/api/v1/users/999/erase", headers=auth_headers)
        assert resp.status_code == 404

    async def test_erase_other_user_denied_for_plain_user(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user cannot erase another user → 403, and nothing is written.

        regression: release-audit-2026-09-23
        """
        other = make_mock_user(user_id=99, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = other

        resp = await client.post("/api/v1/users/99/erase", headers=user_auth_headers)
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()
        app_mock_db.commit.assert_not_called()

    async def test_erase_org_admin_same_org_non_admin_target(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """An org-admin (USER_MANAGE) can erase a non-admin user in its own org → 200.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=5, org_id=1, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/5/erase", headers=rm_auth_headers)
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once()

    async def test_erase_org_admin_cannot_erase_admin(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """An org-admin may not erase an admin account, even in-org → 403.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=7, org_id=1, role="admin")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/7/erase", headers=rm_auth_headers)
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_erase_org_admin_other_org_denied(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """An org-admin cannot erase a user in another org → 403.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=10, org_id=99, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/10/erase", headers=rm_auth_headers)
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_erase_admin_any_user(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An admin (USER_CREATE) can erase any user in any org → 200.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=77, org_id=42, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/77/erase", headers=auth_headers)
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once()

    async def test_erase_self_admin_allowed(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An admin erasing their OWN account is allowed despite the admin-user guard → 200.

        The guard only blocks erasing *another* admin without USER_CREATE; self
        always wins (the whole point of the right).
        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=1, org_id=1, role="admin")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/1/erase", headers=auth_headers)
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once()

    async def test_erase_not_tier_gated(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user with NO enterprise entitlement can still erase → 200.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=2, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/2/erase", headers=user_auth_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Pure dsar-module logic (no DB, no HTTP)
# ---------------------------------------------------------------------------


class TestDsarModule:
    """Unit tests for the storage-agnostic DSAR helpers."""

    def test_anonymized_values_tombstones(self) -> None:
        """anonymized_values replaces every PII column and disables the account.

        regression: release-audit-2026-09-23
        """
        vals = dsar.anonymized_values(42)
        assert vals["username"] == "erased-user-42"
        assert vals["email"] == "erased-42@deleted.invalid"
        assert vals["password_hash"] == dsar.ERASED_PASSWORD_SENTINEL
        assert vals["last_login_ip"] is None
        assert vals["current_login_ip"] is None
        assert vals["enabled"] is False

    def test_export_identity_excludes_password_hash(self) -> None:
        """export_identity discloses held fields but never the password hash.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=3, role="user")
        payload = dsar.export_identity(user)
        assert "password_hash" not in payload
        assert payload["id"] == 3
        assert payload["created_at"] is not None  # datetime -> ISO string

    def test_manifest_marks_users_as_pii_others_by_reference(self) -> None:
        """The manifest flags the identity table as PII, others as id-reference only.

        regression: release-audit-2026-09-23
        """
        manifest = dsar.data_holding_manifest()
        by_table = {e["table"]: e for e in manifest}
        assert by_table["users"]["contains_pii"] is True
        assert by_table["api_keys"]["contains_pii"] is False
        assert "users.id" in by_table["api_keys"]["reference"]

    def test_is_erased_detects_tombstone(self) -> None:
        """is_erased is False before and True after anonymization.

        regression: release-audit-2026-09-23
        """
        user = make_mock_user(user_id=8, role="user")
        assert dsar.is_erased(user) is False
        user.username = dsar.anonymized_values(8)["username"]
        assert dsar.is_erased(user) is True
