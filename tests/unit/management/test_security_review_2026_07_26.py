"""Security regression tests for four confirmed vulnerabilities in management API.

Each test proves the vulnerability is blocked and the legitimate flow works.
Tests use TDD: write failing test first, then implement minimal fix.

regression: security review 2026-07-26
"""

from datetime import datetime
from unittest.mock import MagicMock


def make_mock_user(user_id: int = 1, role: str = "admin", org_id: int = 1, **kwargs) -> MagicMock:
    """Utility to create mock user objects."""
    user = MagicMock()
    user.id = user_id
    user.username = kwargs.get("username", f"user{user_id}")
    user.email = kwargs.get("email", f"user{user_id}@example.com")
    user.role = role
    user.organization_id = org_id
    user.enabled = kwargs.get("enabled", True)
    user.password_hash = "$2b$12$test"  # noqa: S105 -- fixed test hash, not a real secret
    user.token_quota_daily = 10000
    user.token_quota_monthly = 100000
    user.created_at = datetime(2025, 1, 1)
    return user


def make_mock_key(key_id: int = 1, user_id: int = 1, org_id: int = 1, **kwargs) -> MagicMock:
    """Utility to create mock virtual key objects."""
    key = MagicMock()
    key.id = key_id
    key.name = kwargs.get("name", f"key{key_id}")
    key.user_id = user_id
    key.organization_id = org_id
    key.key_prefix = "wa-prefix..."
    key.key_hash = "$2b$12$test"
    key.allowed_models = None
    key.allowed_providers = None
    key.budget_limit_daily = None
    key.budget_limit_monthly = None
    key.tpm_limit = 10000
    key.rpm_limit = 60
    key.enabled = kwargs.get("enabled", True)
    key.expires_at = None
    key.created_at = datetime(2025, 1, 1)
    return key


class TestVulnAPrivilegeEscalationKeyCreation:
    """Vuln A: Privilege escalation via virtual-key creation.

    regression: security review 2026-07-26 — privilege escalation via key creation
    """

    async def test_admin_cannot_create_key_for_non_existent_user(
        self, client, app_mock_db, admin_token: str
    ):
        """Admin tries to create key for user_id that doesn't exist in org.

        regression: security review 2026-07-26 — Vuln A: key creation for non-existent user
        """

        # Mock DB: no user exists with ID 999
        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            result.__iter__ = MagicMock(return_value=iter([]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
        payload = {"name": "test_key", "user_id": 999, "organization_id": 1}

        resp = await client.post("/api/v1/keys", json=payload, headers=headers)

        # Expect: 404 or 403 (not 201 Created)
        assert resp.status_code in [400, 403, 404], (
            f"Expected 400/403/404, got {resp.status_code} — "
            "Vuln A: Admin should not create key for non-existent user"
        )

    async def test_resource_manager_cannot_create_key_for_admin_in_org(
        self, client, app_mock_db, resource_manager_token: str
    ):
        """Resource manager tries to create key for admin user in same org.

        regression: security review 2026-07-26 — Vuln A: role-boundary check on key creation
        """
        # Mock: target user is admin
        admin_user = make_mock_user(user_id=1, role="admin", org_id=1)

        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=admin_user)
            result.__iter__ = MagicMock(return_value=iter([admin_user]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {
            "Authorization": f"Bearer {resource_manager_token}",
            "Content-Type": "application/json",
        }
        payload = {"name": "test_key", "user_id": 1, "organization_id": 1}

        resp = await client.post("/api/v1/keys", json=payload, headers=headers)

        # Expect: 403 Forbidden (not 201 Created)
        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code} — "
            "Vuln A: resource_manager should not create key for admin user"
        )


class TestVulnCAdminAccountTakeoverViaUserUpdate:
    """Vuln C: Admin-account takeover via user update (no role boundary check).

    regression: security review 2026-07-26 — missing role-boundary check on user update
    """

    async def test_resource_manager_cannot_update_admin_password(
        self, client, app_mock_db, resource_manager_token: str
    ):
        """Resource manager tries to reset admin user's password.

        regression: security review 2026-07-26 — Vuln C: password reset on admin
        """
        admin_user = make_mock_user(user_id=1, role="admin", org_id=1)

        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=admin_user)
            result.__iter__ = MagicMock(return_value=iter([admin_user]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {
            "Authorization": f"Bearer {resource_manager_token}",
            "Content-Type": "application/json",
        }
        payload = {"password": "new_password_123"}

        resp = await client.put("/api/v1/users/1", json=payload, headers=headers)

        # Expect: 403 Forbidden (not 200 OK)
        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code} — "
            "Vuln C: resource_manager should not update admin password"
        )

    async def test_resource_manager_cannot_enable_admin_user(
        self, client, app_mock_db, resource_manager_token: str
    ):
        """Resource manager tries to enable a disabled admin user.

        regression: security review 2026-07-26 — Vuln C: enable admin user
        """
        admin_user = make_mock_user(user_id=1, role="admin", org_id=1, enabled=False)

        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=admin_user)
            result.__iter__ = MagicMock(return_value=iter([admin_user]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {
            "Authorization": f"Bearer {resource_manager_token}",
            "Content-Type": "application/json",
        }

        resp = await client.post("/api/v1/users/1/enable", headers=headers)

        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code} — "
            "Vuln C: resource_manager should not enable admin user"
        )

    async def test_resource_manager_cannot_set_admin_quota(
        self, client, app_mock_db, resource_manager_token: str
    ):
        """Resource manager tries to modify quota for admin user.

        regression: security review 2026-07-26 — Vuln C: quota update on admin
        """
        admin_user = make_mock_user(user_id=1, role="admin", org_id=1)

        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=admin_user)
            result.__iter__ = MagicMock(return_value=iter([admin_user]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {
            "Authorization": f"Bearer {resource_manager_token}",
            "Content-Type": "application/json",
        }
        payload = {"token_quota_daily": 5000}

        resp = await client.put("/api/v1/quotas/user/1", json=payload, headers=headers)

        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code} — "
            "Vuln C: resource_manager should not modify admin quota"
        )


class TestVulnDCommandInjectionLlamaCpp:
    """Vuln D: Command injection in llama.cpp deployment.

    regression: security review 2026-07-26 — command injection via shell metacharacters
    """

    async def test_create_deployment_rejects_url_with_shell_metacharacters(
        self, client, app_mock_db, admin_token: str
    ):
        """Attempt to create llama.cpp deployment with shell metacharacters in model_url.

        regression: security review 2026-07-26 — Vuln D: model_url validation
        """
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name": "test_deployment",
            "model_name": "llama-7b",
            "model_url": "https://example.com/model.gguf; rm -rf /",
            "model_filename": "model.gguf",
        }

        resp = await client.post("/api/v1/llamacpp/deployments", json=payload, headers=headers)

        # Expect: 400 Bad Request (not 201 Created)
        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code} — "
            "Vuln D: should reject model_url with shell metacharacters"
        )

    async def test_create_deployment_rejects_filename_with_path_traversal(
        self, client, app_mock_db, admin_token: str
    ):
        """Attempt to create deployment with path traversal in model_filename.

        regression: security review 2026-07-26 — Vuln D: model_filename validation
        """
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name": "test_deployment",
            "model_name": "llama-7b",
            "model_url": "https://example.com/model.gguf",
            "model_filename": "../../../etc/passwd",
        }

        resp = await client.post("/api/v1/llamacpp/deployments", json=payload, headers=headers)

        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code} — "
            "Vuln D: should reject model_filename with path traversal"
        )

    async def test_create_deployment_accepts_valid_url_and_filename(
        self, client, app_mock_db, admin_token: str
    ):
        """Create deployment with valid model_url and model_filename (legitimate case).

        regression: security review 2026-07-26 — Vuln D: allow valid inputs
        """
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name": "test_deployment",
            "model_name": "llama-7b",
            "model_url": "https://example.com/models/llama-7b.gguf",
            "model_filename": "llama-7b.gguf",
        }

        resp = await client.post("/api/v1/llamacpp/deployments", json=payload, headers=headers)

        # Expect: 201 Created (legitimate case should succeed)
        assert resp.status_code == 201, (
            f"Expected 201, got {resp.status_code} — "
            "Vuln D: should accept valid model_url and filename"
        )

    async def test_update_deployment_rejects_url_with_injection(
        self, client, app_mock_db, admin_token: str
    ):
        """Attempt to update deployment with injection payload in model_url.

        regression: security review 2026-07-26 — Vuln D: model_url in PATCH
        """
        deployment = MagicMock()
        deployment.id = 1
        deployment.name = "existing"
        deployment.status = "stopped"
        deployment.model_filename = "model.gguf"
        deployment.created_at = datetime(2025, 1, 1)
        deployment.modified_at = datetime(2025, 1, 1)

        def mock_db_call(*args, **kwargs):
            query = MagicMock()
            query.select = MagicMock()
            result = MagicMock()
            result.first = MagicMock(return_value=deployment)
            result.__iter__ = MagicMock(return_value=iter([deployment]))
            query.select.return_value = result
            return query

        app_mock_db.side_effect = mock_db_call

        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model_url": "https://example.com/$(malicious_command).gguf",
        }

        resp = await client.patch("/api/v1/llamacpp/deployments/1", json=payload, headers=headers)

        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code} — "
            "Vuln D: should reject model_url with command substitution"
        )
