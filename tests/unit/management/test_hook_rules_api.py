"""Unit tests for agent-hooks admin CRUD: /api/v1/hooks/{rules,denylist,configs}.

Focus: the §18.4 tenant-isolation hard boundary -- a `resource_manager`
(tenant admin) can never read, write, or affect another org's rows, and is
always force-scoped to their own org regardless of what the request body
asks for. `admin` (global admin) is platform-wide.

Uses the `rm_org2_auth_headers` fixture (conftest.py) for the "second
tenant" identity -- see that fixture's docstring for why an ad-hoc
`make_token()` call from this module is unsafe.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result


def _mock_rule_row(
    rule_id: int = 1, scope_type: str = "org", scope_ref: str = "1", decision: str = "deny"
) -> MagicMock:
    """A MagicMock standing in for a db `hook_rules` row."""
    row = MagicMock()
    row.id = rule_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.ecosystem = None
    row.event = None
    row.tool_name_pattern = None
    row.match_pattern = None
    row.decision = decision
    row.reason = "test reason"
    row.enabled = True
    row.priority = 100
    row.created_by = 1
    row.created_at = None
    row.updated_at = None
    return row


class TestListHookRules:
    """GET /api/v1/hooks/rules."""

    async def test_requires_admin_or_resource_manager(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain-user token is rejected with 403."""
        resp = await client.get("/api/v1/hooks/rules", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_resource_manager_never_sees_another_orgs_rule(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: a rule scoped to org 1 is invisible to a resource_manager of org 2."""
        org1_rule = _mock_rule_row(rule_id=1, scope_type="org", scope_ref="1")
        global_rule = _mock_rule_row(rule_id=2, scope_type="global", scope_ref=None)
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule, global_rule])

        resp = await client.get("/api/v1/hooks/rules", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        ids = {r["id"] for r in data["data"]}
        assert 1 not in ids  # org 1's rule never visible to org 2
        assert 2 in ids  # global rule is visible (read-only) to every org

    async def test_admin_sees_every_orgs_rules(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Global admin sees rules from every org, unfiltered."""
        org1_rule = _mock_rule_row(rule_id=1, scope_type="org", scope_ref="1")
        org2_rule = _mock_rule_row(rule_id=2, scope_type="org", scope_ref="2")
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule, org2_rule])

        resp = await client.get("/api/v1/hooks/rules", headers=auth_headers)

        data = await resp.get_json()
        assert {r["id"] for r in data["data"]} == {1, 2}


class TestCreateHookRule:
    """POST /api/v1/hooks/rules."""

    async def test_admin_can_create_global_rule(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Global admin may author a scope_type='global' rule."""
        row = _mock_rule_row(rule_id=1, scope_type="global", scope_ref=None)
        app_mock_db.hook_rules.insert.return_value = 1
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={"scope_type": "global", "decision": "deny", "reason": "org-wide block"},
        )

        assert resp.status_code == 201
        insert_kwargs = app_mock_db.hook_rules.insert.call_args.kwargs
        assert insert_kwargs["scope_type"] == "global"
        assert insert_kwargs["scope_ref"] is None

    async def test_resource_manager_is_force_scoped_to_own_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: a resource_manager's write is forced to their own org.

        Even if the request body asks for a global (deployment-wide) scope.
        """
        row = _mock_rule_row(rule_id=3, scope_type="org", scope_ref="2")
        app_mock_db.hook_rules.insert.return_value = 3
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=rm_org2_auth_headers,
            # Attempts a global (deployment-wide) rule -- must be silently
            # overridden to the caller's own org, not honored.
            json={"scope_type": "global", "decision": "deny", "reason": "trying to go global"},
        )

        assert resp.status_code == 201
        insert_kwargs = app_mock_db.hook_rules.insert.call_args.kwargs
        assert insert_kwargs["scope_type"] == "org"
        assert insert_kwargs["scope_ref"] == "2"

    async def test_resource_manager_cannot_author_for_another_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: even an explicit scope_ref for a *different* org is overridden, never honored."""
        row = _mock_rule_row(rule_id=4, scope_type="org", scope_ref="2")
        app_mock_db.hook_rules.insert.return_value = 4
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=rm_org2_auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "decision": "allow", "reason": "sneaky"},
        )

        assert resp.status_code == 201
        insert_kwargs = app_mock_db.hook_rules.insert.call_args.kwargs
        assert insert_kwargs["scope_ref"] == "2"  # org 2's own id, never "1"

    async def test_invalid_decision_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown decision value is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={"scope_type": "global", "decision": "maybe", "reason": "x"},
        )
        assert resp.status_code == 400

    async def test_missing_body_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty request body is rejected with 400 (or 415 for missing JSON content)."""
        resp = await client.post("/api/v1/hooks/rules", headers=auth_headers, json=None)
        assert resp.status_code in (400, 415)

    async def test_admin_invalid_scope_type_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin's own scope_type (not forced) must still be one of _SCOPE_TYPES."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={"scope_type": "planet", "decision": "deny", "reason": "x"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "scope_type" in data["error"]

    async def test_admin_global_scope_with_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A global-scope rule must not also carry a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={
                "scope_type": "global",
                "scope_ref": "1",
                "decision": "deny",
                "reason": "x",
            },
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "scope_ref" in data["error"]

    async def test_admin_org_scope_missing_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An org-scope rule requires a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={"scope_type": "org", "decision": "deny", "reason": "x"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "scope_ref" in data["error"]

    async def test_missing_reason_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A rule with no reason is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={"scope_type": "global", "decision": "deny"},
        )
        assert resp.status_code == 400

    async def test_invalid_ecosystem_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An ecosystem outside HOOK_ECOSYSTEMS is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={
                "scope_type": "global",
                "decision": "deny",
                "reason": "x",
                "ecosystem": "not-a-real-ecosystem",
            },
        )
        assert resp.status_code == 400

    async def test_invalid_event_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An event outside HOOK_EVENTS is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/rules",
            headers=auth_headers,
            json={
                "scope_type": "global",
                "decision": "deny",
                "reason": "x",
                "event": "not-a-real-event",
            },
        )
        assert resp.status_code == 400


class TestUpdateDeleteHookRule:
    """PUT/DELETE /api/v1/hooks/rules/<id> -- scope-checked against the row's current scope."""

    async def test_resource_manager_cannot_update_another_orgs_rule(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: a PUT against org 1's rule from an org-2 resource_manager is forbidden."""
        org1_rule = _mock_rule_row(rule_id=1, scope_type="org", scope_ref="1")
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule])

        resp = await client.put(
            "/api/v1/hooks/rules/1", headers=rm_org2_auth_headers, json={"decision": "allow"}
        )

        assert resp.status_code == 403

    async def test_resource_manager_cannot_delete_another_orgs_rule(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: a DELETE against org 1's rule from an org-2 resource_manager is forbidden."""
        org1_rule = _mock_rule_row(rule_id=1, scope_type="org", scope_ref="1")
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule])

        resp = await client.delete("/api/v1/hooks/rules/1", headers=rm_org2_auth_headers)

        assert resp.status_code == 403

    async def test_resource_manager_can_delete_own_orgs_rule(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """A resource_manager CAN delete a rule scoped to their own org."""
        org2_rule = _mock_rule_row(rule_id=7, scope_type="org", scope_ref="2")
        app_mock_db.return_value.select.return_value = make_select_result([org2_rule])

        resp = await client.delete("/api/v1/hooks/rules/7", headers=rm_org2_auth_headers)

        assert resp.status_code == 200

    async def test_admin_can_update_any_orgs_rule(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Global admin may update a rule scoped to any org."""
        org1_rule = _mock_rule_row(rule_id=1, scope_type="org", scope_ref="1")
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule])

        resp = await client.put(
            "/api/v1/hooks/rules/1", headers=auth_headers, json={"decision": "allow"}
        )

        assert resp.status_code == 200

    async def test_update_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A PUT against a nonexistent id returns 404."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.put(
            "/api/v1/hooks/rules/999", headers=auth_headers, json={"decision": "allow"}
        )

        assert resp.status_code == 404

    async def test_update_missing_body_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty PUT body is rejected with 400 (or 415 for missing JSON content)."""
        resp = await client.put("/api/v1/hooks/rules/1", headers=auth_headers, json=None)
        assert resp.status_code in (400, 415)

    async def test_update_invalid_decision_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown decision value on update is rejected with 400, before any DB lookup."""
        resp = await client.put(
            "/api/v1/hooks/rules/1", headers=auth_headers, json={"decision": "maybe"}
        )
        assert resp.status_code == 400

    async def test_delete_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A DELETE against a nonexistent id returns 404."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.delete("/api/v1/hooks/rules/999", headers=auth_headers)

        assert resp.status_code == 404


def _mock_denylist_row(
    entry_id: int = 1, scope_type: str = "org", scope_ref: str = "2"
) -> MagicMock:
    """A MagicMock standing in for a db `hook_denylist_entries` row."""
    row = MagicMock()
    row.id = entry_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.pattern = "*.tfstate"
    row.reason = "terraform state"
    row.enabled = True
    row.created_by = 1
    row.created_at = None
    return row


class TestListHookDenylistEntries:
    """GET /api/v1/hooks/denylist."""

    async def test_resource_manager_scoping_and_response_shape(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: org 1's entry invisible; org 2's own + global entries visible with full schema."""
        org1_entry = _mock_denylist_row(entry_id=1, scope_type="org", scope_ref="1")
        org2_entry = _mock_denylist_row(entry_id=2, scope_type="org", scope_ref="2")
        global_entry = _mock_denylist_row(entry_id=3, scope_type="global", scope_ref=None)
        app_mock_db.return_value.select.return_value = make_select_result(
            [org1_entry, org2_entry, global_entry]
        )

        resp = await client.get("/api/v1/hooks/denylist", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        ids = {e["id"] for e in data["data"]}
        assert ids == {2, 3}  # own org + global, never another org
        entry = next(e for e in data["data"] if e["id"] == 2)
        assert set(entry.keys()) == {
            "id",
            "scope_type",
            "scope_ref",
            "pattern",
            "reason",
            "enabled",
            "created_by",
            "created_at",
        }

    async def test_admin_sees_every_orgs_entries(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Global admin sees denylist entries from every org, unfiltered."""
        org1_entry = _mock_denylist_row(entry_id=1, scope_type="org", scope_ref="1")
        org2_entry = _mock_denylist_row(entry_id=2, scope_type="org", scope_ref="2")
        app_mock_db.return_value.select.return_value = make_select_result([org1_entry, org2_entry])

        resp = await client.get("/api/v1/hooks/denylist", headers=auth_headers)

        data = await resp.get_json()
        assert {e["id"] for e in data["data"]} == {1, 2}


class TestHookDenylistEntries:
    """CRUD for admin-added Tier-1 denylist entries -- additive only, org-scoped for tenants."""

    async def test_resource_manager_forced_to_own_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """A resource_manager's denylist addition is always scoped to their own org."""
        row = _mock_denylist_row(entry_id=5, scope_type="org", scope_ref="2")
        app_mock_db.hook_denylist_entries.insert.return_value = 5
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/hooks/denylist",
            headers=rm_org2_auth_headers,
            json={"scope_type": "global", "pattern": "*.tfstate"},
        )

        assert resp.status_code == 201
        insert_kwargs = app_mock_db.hook_denylist_entries.insert.call_args.kwargs
        assert insert_kwargs["scope_type"] == "org"
        assert insert_kwargs["scope_ref"] == "2"

    async def test_admin_can_add_global_entry(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Global admin may add a deployment-wide denylist entry."""
        row = _mock_denylist_row(entry_id=6, scope_type="global", scope_ref=None)
        app_mock_db.hook_denylist_entries.insert.return_value = 6
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/hooks/denylist",
            headers=auth_headers,
            json={"scope_type": "global", "pattern": "*.tfstate"},
        )

        assert resp.status_code == 201
        insert_kwargs = app_mock_db.hook_denylist_entries.insert.call_args.kwargs
        assert insert_kwargs["scope_type"] == "global"

    async def test_resource_manager_cannot_delete_another_orgs_entry(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """An org-1-scoped denylist entry cannot be deleted by an org-2 resource_manager."""
        org1_entry = _mock_denylist_row(entry_id=9, scope_type="org", scope_ref="1")
        app_mock_db.return_value.select.return_value = make_select_result([org1_entry])

        resp = await client.delete("/api/v1/hooks/denylist/9", headers=rm_org2_auth_headers)

        assert resp.status_code == 403

    async def test_resource_manager_can_delete_own_orgs_entry(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """A resource_manager CAN delete a denylist entry scoped to their own org."""
        org2_entry = _mock_denylist_row(entry_id=10, scope_type="org", scope_ref="2")
        app_mock_db.return_value.select.return_value = make_select_result([org2_entry])

        resp = await client.delete("/api/v1/hooks/denylist/10", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == {"id": 10}
        app_mock_db.return_value.delete.assert_called_once()

    async def test_delete_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A DELETE against a nonexistent denylist entry id returns 404."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.delete("/api/v1/hooks/denylist/999", headers=auth_headers)

        assert resp.status_code == 404

    async def test_missing_body_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty POST body is rejected with 400 (or 415 for missing JSON content)."""
        resp = await client.post("/api/v1/hooks/denylist", headers=auth_headers, json=None)
        assert resp.status_code in (400, 415)

    async def test_admin_invalid_scope_type_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin's own scope_type (not forced) must still be one of _SCOPE_TYPES."""
        resp = await client.post(
            "/api/v1/hooks/denylist",
            headers=auth_headers,
            json={"scope_type": "planet", "pattern": "*.tfstate"},
        )
        assert resp.status_code == 400

    async def test_admin_global_scope_with_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A global-scope entry must not also carry a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/denylist",
            headers=auth_headers,
            json={"scope_type": "global", "scope_ref": "1", "pattern": "*.tfstate"},
        )
        assert resp.status_code == 400

    async def test_admin_org_scope_missing_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An org-scope entry requires a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/denylist",
            headers=auth_headers,
            json={"scope_type": "org", "pattern": "*.tfstate"},
        )
        assert resp.status_code == 400

    async def test_missing_pattern_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A denylist entry with no pattern is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/denylist", headers=auth_headers, json={"scope_type": "global"}
        )
        assert resp.status_code == 400


def _mock_config_row(
    config_id: int = 1, scope_type: str = "org", scope_ref: str | None = "2"
) -> MagicMock:
    """A MagicMock standing in for a db `hook_configs` row."""
    row = MagicMock()
    row.id = config_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.remote_eval_enabled = True
    row.remote_eval_timeout_ms = 200
    row.remote_eval_fail_mode = "open"
    row.capture_raw_payloads = False
    row.updated_at = None
    return row


class TestListHookConfigs:
    """GET /api/v1/hooks/configs."""

    async def test_resource_manager_scoping_and_response_shape(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: org 1's config invisible; org 2's own + global configs visible."""
        org1_cfg = _mock_config_row(config_id=1, scope_type="org", scope_ref="1")
        org2_cfg = _mock_config_row(config_id=2, scope_type="org", scope_ref="2")
        global_cfg = _mock_config_row(config_id=3, scope_type="global", scope_ref=None)
        app_mock_db.return_value.select.return_value = make_select_result(
            [org1_cfg, org2_cfg, global_cfg]
        )

        resp = await client.get("/api/v1/hooks/configs", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        ids = {c["id"] for c in data["data"]}
        assert ids == {2, 3}  # own org + global, never another org
        entry = next(c for c in data["data"] if c["id"] == 2)
        assert set(entry.keys()) == {
            "id",
            "scope_type",
            "scope_ref",
            "remote_eval_enabled",
            "remote_eval_timeout_ms",
            "remote_eval_fail_mode",
            "capture_raw_payloads",
            "updated_at",
        }


class TestHookConfigsUpsert:
    """POST /api/v1/hooks/configs -- upsert by scope, force-scoped for resource_manager."""

    async def test_resource_manager_forced_to_own_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """A resource_manager cannot set the global (deployment-wide) Tier-2/telemetry defaults."""
        row = MagicMock()
        row.id = 1
        row.scope_type = "org"
        row.scope_ref = "2"
        row.remote_eval_enabled = True
        row.remote_eval_timeout_ms = 200
        row.remote_eval_fail_mode = "open"
        row.capture_raw_payloads = False
        row.updated_at = None
        app_mock_db.hook_configs.insert.return_value = 1
        # First `.first()` call (existing-row check) -> None so the create
        # path runs; second call (post-insert refetch) -> the new row.
        select_result = make_select_result([row])
        select_result.first.side_effect = [None, row]
        app_mock_db.return_value.select.return_value = select_result

        resp = await client.post(
            "/api/v1/hooks/configs",
            headers=rm_org2_auth_headers,
            json={"scope_type": "global", "remote_eval_enabled": True},
        )

        assert resp.status_code in (200, 201)
        insert_kwargs = app_mock_db.hook_configs.insert.call_args.kwargs
        assert insert_kwargs["scope_type"] == "org"
        assert insert_kwargs["scope_ref"] == "2"

    async def test_admin_updates_existing_config_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """POST /configs upserts by UPDATING an existing scope row, never a duplicate insert."""
        row = _mock_config_row(config_id=9, scope_type="global", scope_ref=None)
        select_result = make_select_result([row])
        # Both `.first()` calls (existing-row check, post-update refetch)
        # resolve to the same pre-existing row.
        select_result.first.side_effect = [row, row]
        app_mock_db.return_value.select.return_value = select_result

        resp = await client.post(
            "/api/v1/hooks/configs",
            headers=auth_headers,
            json={"scope_type": "global", "remote_eval_enabled": False},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "updated"
        # `db.return_value` is fresh per test (see `app_mock_db` fixture) so
        # this proves the update path ran, unlike the module-scoped
        # `db.hook_configs.insert` table mock which persists across tests.
        app_mock_db.return_value.update.assert_called_once()

    async def test_invalid_fail_mode_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown remote_eval_fail_mode value is rejected with 400."""
        resp = await client.post(
            "/api/v1/hooks/configs",
            headers=auth_headers,
            json={"scope_type": "global", "remote_eval_fail_mode": "sideways"},
        )
        assert resp.status_code == 400

    async def test_missing_body_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty POST body is rejected with 400 (or 415 for missing JSON content)."""
        resp = await client.post("/api/v1/hooks/configs", headers=auth_headers, json=None)
        assert resp.status_code in (400, 415)

    async def test_admin_invalid_scope_type_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin's own scope_type (not forced) must still be one of _SCOPE_TYPES."""
        resp = await client.post(
            "/api/v1/hooks/configs", headers=auth_headers, json={"scope_type": "planet"}
        )
        assert resp.status_code == 400

    async def test_admin_global_scope_with_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A global-scope config must not also carry a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/configs",
            headers=auth_headers,
            json={"scope_type": "global", "scope_ref": "1"},
        )
        assert resp.status_code == 400

    async def test_admin_org_scope_missing_scope_ref_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An org-scope config requires a scope_ref."""
        resp = await client.post(
            "/api/v1/hooks/configs", headers=auth_headers, json={"scope_type": "org"}
        )
        assert resp.status_code == 400
