"""Unit tests for Cilium status/reconcile routes: /api/v1/cilium/*."""

from unittest.mock import MagicMock, patch

from services.management.app.services.cilium_policy import ReconcileStatus

MODULE = "services.management.app.api.v1.cilium"


class TestCiliumStatus:
    """Tests for GET /api/v1/cilium/status."""

    async def test_status_admin_ok(self, client, auth_headers: dict) -> None:
        """An admin request returns 200 with capabilities, flag state, and reconcile fields."""
        caps = {"network_policy": True, "envoy_config": True, "available": True}
        with (
            patch(f"{MODULE}.cilium_capabilities", return_value=caps),
            patch(f"{MODULE}.is_native_rate_limit_enabled", return_value=True),
            patch(f"{MODULE}.get_last_status", return_value=None),
        ):
            resp = await client.get("/api/v1/cilium/status", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["capabilities"] == caps
        assert data["flag_enabled"] is True
        assert data["last_reconcile"] is None
        assert data["applied"] == []
        assert data["degraded"] is False

    async def test_status_no_auth(self, client) -> None:
        """A request with no Authorization header is rejected with 401."""
        resp = await client.get("/api/v1/cilium/status")
        assert resp.status_code == 401

    async def test_status_non_admin_forbidden(self, client, user_auth_headers: dict) -> None:
        """A non-admin caller is rejected with 403."""
        resp = await client.get("/api/v1/cilium/status", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_status_crds_absent_still_200(self, client, auth_headers: dict) -> None:
        """CRD-absent is a normal state on non-Cilium clusters, not a server error."""
        caps = {"network_policy": False, "envoy_config": False, "available": False}
        with (
            patch(f"{MODULE}.cilium_capabilities", return_value=caps),
            patch(f"{MODULE}.is_native_rate_limit_enabled", return_value=False),
            patch(
                f"{MODULE}.get_last_status",
                return_value=ReconcileStatus(skipped=True, reason="crds_absent"),
            ),
        ):
            resp = await client.get("/api/v1/cilium/status", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["capabilities"]["available"] is False
        assert data["last_reconcile"]["reason"] == "crds_absent"

    async def test_status_reports_last_reconcile_applied(self, client, auth_headers: dict) -> None:
        """The response echoes the most recent reconcile's applied object names."""
        last = ReconcileStatus(
            applied=["waddleai-default-deny", "waddleai-org-ratelimit"], degraded=False
        )
        with (
            patch(
                f"{MODULE}.cilium_capabilities",
                return_value={"network_policy": True, "envoy_config": True, "available": True},
            ),
            patch(f"{MODULE}.is_native_rate_limit_enabled", return_value=True),
            patch(f"{MODULE}.get_last_status", return_value=last),
        ):
            resp = await client.get("/api/v1/cilium/status", headers=auth_headers)

        data = await resp.get_json()
        assert data["applied"] == ["waddleai-default-deny", "waddleai-org-ratelimit"]
        assert data["last_reconcile"]["skipped"] is False


class TestCiliumReconcile:
    """Tests for POST /api/v1/cilium/reconcile."""

    async def test_reconcile_admin_invokes_reconciler_once(
        self, client, auth_headers: dict
    ) -> None:
        """An admin POST constructs the reconciler exactly once and returns its status."""
        mock_instance = MagicMock()
        mock_instance.reconcile.return_value = ReconcileStatus(applied=["waddleai-default-deny"])
        mock_cls = MagicMock(return_value=mock_instance)

        with patch(f"{MODULE}.CiliumPolicyReconciler", mock_cls):
            resp = await client.post("/api/v1/cilium/reconcile", headers=auth_headers)

        assert resp.status_code == 202
        data = await resp.get_json()
        assert data["applied"] == ["waddleai-default-deny"]
        assert data["skipped"] is False
        assert data["degraded"] is False
        mock_cls.assert_called_once()
        mock_instance.reconcile.assert_called_once()

    async def test_reconcile_no_auth(self, client) -> None:
        """A request with no Authorization header is rejected with 401."""
        resp = await client.post("/api/v1/cilium/reconcile")
        assert resp.status_code == 401

    async def test_reconcile_non_admin_forbidden(self, client, user_auth_headers: dict) -> None:
        """A non-admin caller is rejected with 403."""
        resp = await client.post("/api/v1/cilium/reconcile", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_reconcile_skipped_flag_off_still_202(self, client, auth_headers: dict) -> None:
        """A skipped (flag-off) reconcile still returns 202 with the skip reason, not an error."""
        mock_instance = MagicMock()
        mock_instance.reconcile.return_value = ReconcileStatus(skipped=True, reason="flag_off")
        mock_cls = MagicMock(return_value=mock_instance)

        with patch(f"{MODULE}.CiliumPolicyReconciler", mock_cls):
            resp = await client.post("/api/v1/cilium/reconcile", headers=auth_headers)

        assert resp.status_code == 202
        data = await resp.get_json()
        assert data["skipped"] is True
        assert data["reason"] == "flag_off"


class TestOrganizationWriteTriggersReconcile:
    """Org create/update fires a non-blocking reconcile trigger exactly once."""

    async def test_create_organization_triggers_reconcile(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Creating an organization constructs the Cilium reconciler exactly once."""
        app_mock_db.return_value.select.return_value.first.return_value = None  # name not taken
        app_mock_db.organizations.insert.return_value = 42

        mock_cls = MagicMock()
        with patch("services.management.app.api.v1.organizations.CiliumPolicyReconciler", mock_cls):
            resp = await client.post(
                "/api/v1/organizations",
                headers=auth_headers,
                json={"name": "acme"},
            )

        assert resp.status_code == 201
        mock_cls.assert_called_once()

    async def test_update_organization_triggers_reconcile(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Updating an organization constructs the Cilium reconciler exactly once."""
        org = MagicMock()
        org.id = 1
        org.name = "acme"
        app_mock_db.return_value.select.return_value.first.return_value = org

        mock_cls = MagicMock()
        with patch("services.management.app.api.v1.organizations.CiliumPolicyReconciler", mock_cls):
            resp = await client.put(
                "/api/v1/organizations/1",
                headers=auth_headers,
                json={"description": "updated"},
            )

        assert resp.status_code == 200
        mock_cls.assert_called_once()
