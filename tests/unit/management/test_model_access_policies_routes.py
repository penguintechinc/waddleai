"""Unit tests for model access policy routes: /api/v1/routing/access-policies/*.

Covers the two-layer Enterprise gate (flag off -> 404, unentitled -> 403),
full CRUD, scope/rule validation 400s, tenant-isolation 403s (resource_manager
cross-org and global-scope writes), and the {status,data,meta} envelope shape.
"""

from datetime import datetime
from unittest.mock import MagicMock

from services.management.app.api.v1 import model_access_policies
from tests.unit.management.conftest import make_dal_row, make_select_result

ENDPOINT_PATH = "/api/v1/routing/access-policies/"


def _enable_flag(monkeypatch) -> None:
    """Turn `waddleai.model_access_policy` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "1")


def _entitled(monkeypatch, entitled: bool = True) -> None:
    """Patch the license-entitlement check for one test."""
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "services.management.app.api.v1.model_access_policies._get_license_client",
        lambda: mock_client,
    )


def _gate_open(monkeypatch, entitled: bool = True) -> None:
    """Flag on + entitled -- the surface is fully usable."""
    _enable_flag(monkeypatch)
    _entitled(monkeypatch, entitled)


def _make_policy_row(
    *,
    policy_id: int = 1,
    scope_type: str = "global",
    scope_ref: str | None = None,
    model_pattern: str = "claude-opus-5*",
    action: str = "reject",
    fallback_model: str | None = None,
    reason: str | None = None,
    enabled: bool = True,
    created_by: int | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a spec'd fake `model_access_policies` row (mirrors `_row_to_dict`'s field set)."""
    return make_dal_row(
        id=policy_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        model_pattern=model_pattern,
        action=action,
        fallback_model=fallback_model,
        reason=reason,
        enabled=enabled,
        created_by=created_by,
        created_at=created_at or datetime(2026, 8, 28, 12, 0, 0),
        updated_at=created_at or datetime(2026, 8, 28, 12, 0, 0),
    )


# ---------------------------------------------------------------------------
# Pure validation helpers
# ---------------------------------------------------------------------------


class TestValidateScope:
    """Tests for `_validate_scope`."""

    def test_invalid_scope_type(self) -> None:
        """An unrecognized scope_type is rejected."""
        assert model_access_policies._validate_scope("bogus", None) is not None

    def test_global_with_scope_ref_rejected(self) -> None:
        """scope_type='global' with a non-null scope_ref is rejected."""
        error = model_access_policies._validate_scope("global", "1")
        assert error is not None
        assert "scope_ref must be null" in error

    def test_org_without_scope_ref_rejected(self) -> None:
        """scope_type='org' with a null scope_ref is rejected."""
        error = model_access_policies._validate_scope("org", None)
        assert error is not None
        assert "scope_ref is required" in error

    def test_global_without_scope_ref_ok(self) -> None:
        """scope_type='global' with a null scope_ref is valid."""
        assert model_access_policies._validate_scope("global", None) is None

    def test_key_with_scope_ref_ok(self) -> None:
        """scope_type='key' with a populated scope_ref is valid."""
        assert model_access_policies._validate_scope("key", "42") is None


class TestValidateRule:
    """Tests for `_validate_rule`."""

    def test_empty_pattern_rejected(self) -> None:
        """An empty model_pattern is rejected."""
        error = model_access_policies._validate_rule("", "reject", None)
        assert error is not None
        assert "model_pattern" in error

    def test_whitespace_only_pattern_rejected(self) -> None:
        """A whitespace-only model_pattern is rejected."""
        assert model_access_policies._validate_rule("   ", "reject", None) is not None

    def test_invalid_action_rejected(self) -> None:
        """An action outside {reject, reroute} is rejected."""
        error = model_access_policies._validate_rule("gpt-4o", "bogus", None)
        assert error is not None
        assert "action" in error

    def test_reroute_without_fallback_rejected(self) -> None:
        """action='reroute' with no fallback_model is rejected."""
        error = model_access_policies._validate_rule("gpt-4o", "reroute", None)
        assert error is not None
        assert "fallback_model" in error

    def test_reroute_with_fallback_ok(self) -> None:
        """action='reroute' with a fallback_model is valid."""
        assert model_access_policies._validate_rule("gpt-4o", "reroute", "gpt-4o-mini") is None

    def test_reject_ok(self) -> None:
        """action='reject' never requires a fallback_model."""
        assert model_access_policies._validate_rule("gpt-4o", "reject", None) is None


class TestRowToDict:
    """Tests for the row -> serializable dict conversion."""

    def test_full_field_set(self) -> None:
        """`_row_to_dict` emits exactly the documented field set."""
        row = _make_policy_row(policy_id=1, created_by=7)
        result = model_access_policies._row_to_dict(row)
        assert set(result.keys()) == {
            "id",
            "scope_type",
            "scope_ref",
            "model_pattern",
            "action",
            "fallback_model",
            "reason",
            "enabled",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert result["created_by"] == 7


# ---------------------------------------------------------------------------
# Two-layer gate (flag + license entitlement)
# ---------------------------------------------------------------------------


class TestGate:
    """Tests for the flag-off / unentitled two-layer gate, across every route."""

    async def test_list_flag_off_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """With `waddleai.model_access_policy` off, the endpoint is inert (404)."""
        monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "0")
        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 404

    async def test_list_flag_on_but_unentitled_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Flag on but no Enterprise entitlement refuses with a tier-named 403."""
        _gate_open(monkeypatch, entitled=False)
        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 403
        data = await resp.get_json()
        assert "Enterprise" in data["error"]

    async def test_create_flag_off_returns_404(
        self, client, auth_headers: dict, monkeypatch
    ) -> None:
        """Create is also gated: flag off returns 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "0")
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "global", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 404

    async def test_create_unentitled_returns_403(
        self, client, auth_headers: dict, monkeypatch
    ) -> None:
        """Create is also gated: unentitled returns 403."""
        _gate_open(monkeypatch, entitled=False)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "global", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 403

    async def test_delete_flag_off_returns_404(
        self, client, auth_headers: dict, monkeypatch
    ) -> None:
        """Delete is also gated: flag off returns 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "0")
        resp = await client.delete(f"{ENDPOINT_PATH}1", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET (list / get) -- require gate open for the rest of this module
# ---------------------------------------------------------------------------


class TestListPolicies:
    """Tests for GET /api/v1/routing/access-policies/."""

    async def test_list_admin_sees_all(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Admin listing returns every seeded policy."""
        _gate_open(monkeypatch)
        rows = [
            _make_policy_row(policy_id=1),
            _make_policy_row(policy_id=2, scope_type="org", scope_ref="9"),
        ]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert [e["id"] for e in data["data"]] == [1, 2]
        assert data["meta"]["total"] == 2

    async def test_list_empty(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """No policies returns an empty list, not an error."""
        _gate_open(monkeypatch)
        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_list_no_auth(self, client, monkeypatch) -> None:
        """Missing auth returns 401."""
        _gate_open(monkeypatch)
        resp = await client.get(ENDPOINT_PATH)
        assert resp.status_code == 401

    async def test_list_response_envelope_shape(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Response matches the {status,data,meta} envelope."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([_make_policy_row()])

        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "data", "meta"}
        assert set(data["meta"].keys()) == {"total", "timestamp"}

    async def test_list_resource_manager_scoped_query(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """A non-admin caller exercises `_visible_query`'s global+org+user branch."""
        _gate_open(monkeypatch)
        rows = [_make_policy_row(policy_id=1, scope_type="org", scope_ref="1")]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get(ENDPOINT_PATH, headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["total"] == 1


class TestGetPolicy:
    """Tests for GET /api/v1/routing/access-policies/<id>."""

    async def test_get_flag_off_returns_404(self, client, auth_headers: dict, monkeypatch) -> None:
        """Get-by-id is also gated: flag off returns 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "0")
        resp = await client.get(f"{ENDPOINT_PATH}1", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An existing policy is returned with its full field set."""
        _gate_open(monkeypatch)
        row = _make_policy_row(policy_id=7, model_pattern="claude-opus-5*")
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.get(f"{ENDPOINT_PATH}7", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["id"] == 7
        assert data["data"]["model_pattern"] == "claude-opus-5*"

    async def test_get_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A missing policy returns 404."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get(f"{ENDPOINT_PATH}999", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST (create)
# ---------------------------------------------------------------------------


class TestCreatePolicy:
    """Tests for POST /api/v1/routing/access-policies/."""

    async def test_no_body(self, client, auth_headers: dict, monkeypatch) -> None:
        """A JSON `null` body (falsy, but valid JSON) hits the `if not data` guard directly."""
        _gate_open(monkeypatch)
        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, data="null")
        assert resp.status_code == 400

    async def test_invalid_scope_type(self, client, auth_headers: dict, monkeypatch) -> None:
        """An invalid scope_type returns 400."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "bogus", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 400

    async def test_empty_model_pattern(self, client, auth_headers: dict, monkeypatch) -> None:
        """An empty model_pattern returns 400."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "global", "model_pattern": ""},
        )
        assert resp.status_code == 400

    async def test_reroute_without_fallback(self, client, auth_headers: dict, monkeypatch) -> None:
        """action='reroute' with no fallback_model returns 400."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "global", "model_pattern": "gpt-4o", "action": "reroute"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "fallback_model" in data["error"]

    async def test_admin_creates_global_policy(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An admin may create a global-scope policy."""
        _gate_open(monkeypatch)
        new_row = _make_policy_row(policy_id=1, scope_type="global", model_pattern="claude-opus-5*")
        app_mock_db.model_access_policies.insert.return_value = 1
        app_mock_db.return_value.select.return_value.first.return_value = new_row

        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json={"scope_type": "global", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["meta"]["action"] == "created"
        assert data["data"]["scope_type"] == "global"

    async def test_resource_manager_cannot_create_global_policy(
        self, client, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """resource_manager may never write a global (scope_type='global') policy."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "global", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_create_for_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """resource_manager (org=1, per make_token default) may write scope_ref='1'."""
        _gate_open(monkeypatch)
        new_row = _make_policy_row(policy_id=2, scope_type="org", scope_ref="1")
        app_mock_db.model_access_policies.insert.return_value = 2
        app_mock_db.return_value.select.return_value.first.return_value = new_row

        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 201

    async def test_resource_manager_cannot_create_for_other_org(
        self, client, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """resource_manager (org=1) cannot write a policy scoped to a different org."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "org", "scope_ref": "2", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_create_user_scoped_within_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """A user-scoped write is allowed when the target user belongs to the caller's org."""
        _gate_open(monkeypatch)
        target_user = MagicMock(organization_id=1)
        new_row = _make_policy_row(policy_id=5, scope_type="user", scope_ref="8")
        app_mock_db.model_access_policies.insert.return_value = 5
        app_mock_db.return_value.select.return_value.first.side_effect = [target_user, new_row]

        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "user", "scope_ref": "8", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 201

    async def test_resource_manager_cannot_create_user_scoped_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """A user-scoped write is refused when the target user belongs to a different org."""
        _gate_open(monkeypatch)
        target_user = MagicMock(organization_id=2)
        app_mock_db.return_value.select.return_value.first.side_effect = [target_user]

        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "user", "scope_ref": "99", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_create_key_scoped_within_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """A key-scoped write is allowed when the target virtual_key belongs to the caller's org."""
        _gate_open(monkeypatch)
        target_key = MagicMock(organization_id=1)
        new_row = _make_policy_row(policy_id=6, scope_type="key", scope_ref="12")
        app_mock_db.model_access_policies.insert.return_value = 6
        app_mock_db.return_value.select.return_value.first.side_effect = [target_key, new_row]

        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "key", "scope_ref": "12", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 201

    async def test_resource_manager_cannot_create_key_scoped_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """A key-scoped write is refused when the target virtual_key belongs to another org."""
        _gate_open(monkeypatch)
        target_key = MagicMock(organization_id=2)
        app_mock_db.return_value.select.return_value.first.side_effect = [target_key]

        resp = await client.post(
            ENDPOINT_PATH,
            headers=rm_auth_headers,
            json={"scope_type": "key", "scope_ref": "77", "model_pattern": "claude-opus-5*"},
        )
        assert resp.status_code == 403

    async def test_plain_user_forbidden_by_scope(
        self, client, user_auth_headers: dict, monkeypatch
    ) -> None:
        """A user without model_access_policy:write is rejected before route logic runs."""
        _gate_open(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=user_auth_headers,
            json={"scope_type": "global", "model_pattern": "gpt-4o"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT (update)
# ---------------------------------------------------------------------------


class TestUpdatePolicy:
    """Tests for PUT /api/v1/routing/access-policies/<id>."""

    async def test_flag_off_returns_404(self, client, auth_headers: dict, monkeypatch) -> None:
        """Update is also gated: flag off returns 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_MODEL_ACCESS_POLICY", "0")
        resp = await client.put(f"{ENDPOINT_PATH}1", headers=auth_headers, json={"enabled": False})
        assert resp.status_code == 404

    async def test_no_body(self, client, auth_headers: dict, monkeypatch) -> None:
        """A JSON `null` body (falsy, but valid JSON) hits the `if not data` guard directly."""
        _gate_open(monkeypatch)
        resp = await client.put(f"{ENDPOINT_PATH}1", headers=auth_headers, data="null")
        assert resp.status_code == 400

    async def test_no_valid_fields(self, client, auth_headers: dict, monkeypatch) -> None:
        """A body with no updatable fields returns 400."""
        _gate_open(monkeypatch)
        resp = await client.put(
            f"{ENDPOINT_PATH}1", headers=auth_headers, json={"bogus_field": "x"}
        )
        assert resp.status_code == 400

    async def test_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A missing policy returns 404."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            f"{ENDPOINT_PATH}999", headers=auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 404

    async def test_forbidden_cross_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict, monkeypatch
    ) -> None:
        """An org-2 resource_manager cannot update an org-1-scoped policy."""
        _gate_open(monkeypatch)
        row = _make_policy_row(policy_id=3, scope_type="org", scope_ref="1")
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.put(
            f"{ENDPOINT_PATH}3", headers=rm_org2_auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 403

    async def test_reroute_without_fallback_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Switching to action='reroute' without also supplying fallback_model is rejected."""
        _gate_open(monkeypatch)
        existing = _make_policy_row(policy_id=3, action="reject", fallback_model=None)
        app_mock_db.return_value.select.return_value.first.return_value = existing

        resp = await client.put(
            f"{ENDPOINT_PATH}3", headers=auth_headers, json={"action": "reroute"}
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "fallback_model" in data["error"]

    async def test_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A valid update returns the refreshed row."""
        _gate_open(monkeypatch)
        existing = _make_policy_row(policy_id=3, enabled=True)
        updated = _make_policy_row(policy_id=3, enabled=False)
        app_mock_db.return_value.select.return_value.first.side_effect = [existing, updated]

        resp = await client.put(f"{ENDPOINT_PATH}3", headers=auth_headers, json={"enabled": False})
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["enabled"] is False


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDeletePolicy:
    """Tests for DELETE /api/v1/routing/access-policies/<id>."""

    async def test_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A missing policy returns 404."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete(f"{ENDPOINT_PATH}999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_resource_manager_forbidden_by_scope(
        self, client, rm_auth_headers: dict, monkeypatch
    ) -> None:
        """DELETE requires MODEL_ACCESS_POLICY_DELETE -- admin only, resource_manager lacks it."""
        _gate_open(monkeypatch)
        resp = await client.delete(f"{ENDPOINT_PATH}1", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_admin_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A valid delete returns the deleted id and action=deleted."""
        _gate_open(monkeypatch)
        row = _make_policy_row(policy_id=4)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.delete(f"{ENDPOINT_PATH}4", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "deleted"
        assert data["data"]["id"] == 4

    async def test_no_auth(self, client, monkeypatch) -> None:
        """Missing auth returns 401."""
        _gate_open(monkeypatch)
        resp = await client.delete(f"{ENDPOINT_PATH}1")
        assert resp.status_code == 401
