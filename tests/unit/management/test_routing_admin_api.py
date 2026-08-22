"""Unit tests for the smart-routing admin API (spec §7.6, Task 14).

Covers routing_assignments, routing_policies, routing_rules, model_aliases,
and routing_decisions.
"""

from datetime import datetime
from unittest.mock import MagicMock, Mock

import pytest

import services.management.app.api.v1.routing_assignments as routing_assignments_mod
from tests.unit.management.conftest import make_select_result


def _assignment_row(**overrides) -> Mock:
    """Build a mock model_assignments row with sane defaults."""
    defaults = dict(
        id=1,
        tool_type="code",
        complexity=None,
        region=None,
        model_name="gpt-4o",
        model_params=None,
        vram_gb=None,
        capability_score=0.8,
        enabled=True,
        credential_label=None,
        escalation_model=None,
        fallback_models=[],
        scope="global",
        scope_ref=None,
        created_at=None,
    )
    defaults.update(overrides)
    return Mock(**defaults)


def _policy_row(**overrides) -> Mock:
    """Build a mock routing_policies row with sane defaults."""
    defaults = dict(
        id=1,
        organization_id=1,
        mode="local_first",
        escalation_threshold=3,
        escalation_target=None,
        classifier_prompt=None,
        de_escalation="idle_reset",
        idle_reset_minutes=10,
        sensitivity_routing="local_only",
        budget_pressure_enabled=True,
        provider_failover="off",
        created_at=None,
        updated_at=None,
    )
    defaults.update(overrides)
    return Mock(**defaults)


def _rule_row(**overrides) -> Mock:
    """Build a mock routing_rules_v2 row with sane defaults.

    Uses configure_mock() rather than Mock(**kwargs) because 'name' is a
    reserved Mock() constructor kwarg (sets the mock's repr, not a 'name'
    attribute) -- configure_mock() sets it as a genuine attribute instead.
    """
    defaults = dict(
        id=1,
        name="prefer-local-chat",
        priority=100,
        match={"endpoint": "/v1/chat/completions"},
        action={"tool_type": "chat"},
        enabled=True,
        organization_id=None,
        created_at=None,
    )
    defaults.update(overrides)
    row = Mock()
    row.configure_mock(**defaults)
    return row


def _alias_row(**overrides) -> Mock:
    """Build a mock model_aliases row with sane defaults."""
    defaults = dict(
        id=1,
        organization_id=None,
        source_model="gpt-4o",
        target_model="mistral-large",
        target_provider=None,
        enabled=True,
        created_at=None,
    )
    defaults.update(overrides)
    return Mock(**defaults)


def _trace_row(**overrides) -> Mock:
    """Build a mock routing_decision_traces row with sane defaults."""
    defaults = dict(
        id=1,
        request_id="req-1",
        organization_id=1,
        timestamp=None,
        requirements={"min_context": 100},
        tool_type="chat",
        tool_type_source="heuristic",
        rules_fired=[],
        classifier_output=None,
        assignment_model="gpt-4o",
        capability_veto=False,
        veto_reason=None,
        qualified_candidates=[],
        pressure_signals=None,
        final_model="gpt-4o",
        routed_from=None,
        escalated=False,
    )
    defaults.update(overrides)
    return Mock(**defaults)


# ---------------------------------------------------------------------------
# routing_assignments
# ---------------------------------------------------------------------------


class TestRoutingAssignments:
    """CRUD tests for /api/v1/routing/assignments."""

    async def test_list_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/routing/assignments/")
        assert resp.status_code == 401

    async def test_list_returns_entries(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin lists visible assignments."""
        app_mock_db.return_value.select.return_value = make_select_result([_assignment_row()])

        resp = await client.get("/api/v1/routing/assignments/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "success"
        assert data["data"][0]["model_name"] == "gpt-4o"
        assert data["data"][0]["scope"] == "global"

    async def test_get_not_found(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """Unknown id returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/routing/assignments/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_create_global_as_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can create a global assignment."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row()

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "code", "model_name": "gpt-4o"},
        )
        assert resp.status_code in (200, 201)
        data = await resp.get_json()
        assert data["status"] == "success"
        assert "warnings" in data["meta"]

    async def test_create_global_as_resource_manager_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot create a GLOBAL assignment (affects every org)."""
        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=rm_auth_headers,
            json={"tool_type": "code", "model_name": "gpt-4o", "scope": "global"},
        )
        assert resp.status_code == 403

    async def test_create_org_scoped_as_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager can create an org-scoped assignment for their own org."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row(
            scope="org", scope_ref=1
        )

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=rm_auth_headers,
            json={"tool_type": "code", "model_name": "gpt-4o", "scope": "org", "scope_ref": 1},
        )
        assert resp.status_code in (200, 201)

    async def test_create_org_scoped_other_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot create an assignment scoped to a different org."""
        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=rm_auth_headers,
            json={"tool_type": "code", "model_name": "gpt-4o", "scope": "org", "scope_ref": 999},
        )
        assert resp.status_code == 403

    async def test_create_missing_required_field(self, client, auth_headers: dict) -> None:
        """Missing model_name returns 400."""
        resp = await client.post(
            "/api/v1/routing/assignments/", headers=auth_headers, json={"tool_type": "code"}
        )
        assert resp.status_code == 400

    async def test_update_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Updating a non-existent assignment returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/routing/assignments/999", headers=auth_headers, json={"model_name": "gpt-4o"}
        )
        assert resp.status_code == 404

    async def test_delete_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """Admin can delete an assignment."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row()

        resp = await client.delete("/api/v1/routing/assignments/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "deleted"

    async def test_seed_admin_only(self, client, rm_auth_headers: dict) -> None:
        """resource_manager cannot call the seed convenience endpoint."""
        resp = await client.post("/api/v1/routing/assignments/seed", headers=rm_auth_headers)
        assert resp.status_code == 403

    # -- list filters / pagination-adjacent behaviour --------------------

    async def test_list_filter_tool_type(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """tool_type query param takes the tool_type-filter branch."""
        app_mock_db.return_value.select.return_value = make_select_result([_assignment_row()])

        resp = await client.get("/api/v1/routing/assignments/?tool_type=code", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"][0]["tool_type"] == "code"

    async def test_list_filter_scope(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Scope query param takes the scope-filter branch."""
        app_mock_db.return_value.select.return_value = make_select_result([_assignment_row()])

        resp = await client.get("/api/v1/routing/assignments/?scope=global", headers=auth_headers)
        assert resp.status_code == 200

    async def test_list_filter_enabled_true_and_false(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """enabled=true and enabled=false both take the enabled-filter branch."""
        app_mock_db.return_value.select.return_value = make_select_result([_assignment_row()])

        resp_true = await client.get(
            "/api/v1/routing/assignments/?enabled=true", headers=auth_headers
        )
        resp_false = await client.get(
            "/api/v1/routing/assignments/?enabled=false", headers=auth_headers
        )
        assert resp_true.status_code == 200
        assert resp_false.status_code == 200

    async def test_list_all_filters_combined(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """tool_type + scope + enabled together all narrow the same query."""
        app_mock_db.return_value.select.return_value = make_select_result([_assignment_row()])

        resp = await client.get(
            "/api/v1/routing/assignments/?tool_type=code&scope=global&enabled=true",
            headers=auth_headers,
        )
        assert resp.status_code == 200

    async def test_list_empty_results(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """No matching rows returns 200 with an empty list, not an error."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/routing/assignments/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_list_non_admin_uses_org_scoped_visibility(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A non-admin caller exercises the org-scoped branch of _visible_query."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_assignment_row(scope="org", scope_ref=1)]
        )

        resp = await client.get("/api/v1/routing/assignments/", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"][0]["scope"] == "org"

    # -- get_entry ---------------------------------------------------------

    async def test_get_entry_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A known id returns its full row, including a formatted created_at."""
        row = _assignment_row(created_at=datetime(2025, 1, 1, 12, 0, 0))
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.get("/api/v1/routing/assignments/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["created_at"] == "2025-01-01T12:00:00"

    async def test_get_entry_non_admin(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A non-admin get exercises the org-scoped branch of _visible_query."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row(
            scope="org", scope_ref=1
        )

        resp = await client.get("/api/v1/routing/assignments/1", headers=rm_auth_headers)
        assert resp.status_code == 200

    # -- create validation ---------------------------------------------------

    async def test_create_requires_body(self, client, auth_headers: dict) -> None:
        """An empty JSON body ({}) is falsy and returns 400 before any field is checked."""
        resp = await client.post("/api/v1/routing/assignments/", headers=auth_headers, json={})
        assert resp.status_code == 400

    @pytest.mark.parametrize("missing_field", ["tool_type", "model_name"])
    async def test_create_missing_required_field_each(
        self, client, auth_headers: dict, missing_field: str
    ) -> None:
        """Each of tool_type/model_name is individually required."""
        payload = {"tool_type": "chat", "model_name": "gpt-4o"}
        del payload[missing_field]

        resp = await client.post("/api/v1/routing/assignments/", headers=auth_headers, json=payload)
        assert resp.status_code == 400

    async def test_create_tool_type_too_long_rejected(self, client, auth_headers: dict) -> None:
        """tool_type longer than 50 characters is rejected before any DB write."""
        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "x" * 51, "model_name": "gpt-4o"},
        )
        assert resp.status_code == 400

    async def test_create_invalid_scope_rejected(self, client, auth_headers: dict) -> None:
        """Scope must be 'global' or 'org'."""
        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "chat", "model_name": "gpt-4o", "scope": "tenant"},
        )
        assert resp.status_code == 400

    async def test_create_org_scope_without_scope_ref_rejected(
        self, client, auth_headers: dict
    ) -> None:
        """scope='org' requires an explicit scope_ref."""
        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "chat", "model_name": "gpt-4o", "scope": "org"},
        )
        assert resp.status_code == 400

    async def test_create_global_scope_forces_scope_ref_none(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A stray scope_ref on a global-scope create is discarded, not persisted."""
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,
            _assignment_row(),
        ]

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={
                "tool_type": "chat",
                "model_name": "gpt-4o",
                "scope": "global",
                "scope_ref": 5,
            },
        )
        assert resp.status_code == 201
        assert app_mock_db.model_assignments.insert.call_args.kwargs["scope_ref"] is None

    async def test_create_new_entry_is_a_true_insert(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """No existing row means action='created' / HTTP 201 (the genuine insert path)."""
        new_row = _assignment_row(id=7, tool_type="embed", model_name="gpt-4o")
        app_mock_db.return_value.select.return_value.first.side_effect = [None, new_row]

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "embed", "model_name": "gpt-4o"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["meta"]["action"] == "created"
        assert data["data"]["id"] == 7

    async def test_create_upsert_updates_existing_entry(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An existing (tool_type, scope, scope_ref) row is updated, not duplicated."""
        existing = _assignment_row(id=3, model_name="gpt-3.5")
        app_mock_db.return_value.select.return_value.first.return_value = existing

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "chat", "model_name": "gpt-4o"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "updated"

    # -- update_entry --------------------------------------------------------

    async def test_update_requires_body(self, client, auth_headers: dict) -> None:
        """An empty JSON body ({}) returns 400."""
        resp = await client.put("/api/v1/routing/assignments/1", headers=auth_headers, json={})
        assert resp.status_code == 400

    async def test_update_forbidden_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot update a row scoped to a different org."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row(
            scope="org", scope_ref=999
        )

        resp = await client.put(
            "/api/v1/routing/assignments/1",
            headers=rm_auth_headers,
            json={"model_name": "gpt-4o"},
        )
        assert resp.status_code == 403

    async def test_update_no_fields_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A body with no recognized writable field returns 400."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row()

        resp = await client.put(
            "/api/v1/routing/assignments/1", headers=auth_headers, json={"bogus_field": "x"}
        )
        assert resp.status_code == 400

    async def test_update_success_with_model_name_runs_capability_check(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Updating model_name populates meta.warnings from capability validation."""
        updated = _assignment_row(model_name="gpt-4o-mini")
        app_mock_db.return_value.select.return_value.first.return_value = updated

        resp = await client.put(
            "/api/v1/routing/assignments/1",
            headers=auth_headers,
            json={"model_name": "gpt-4o-mini"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["warnings"] == ["model is not present in the registry"]

    async def test_update_success_without_model_name_skips_capability_check(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Updating a non-model field leaves meta.warnings empty (no capability lookup run)."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row()

        resp = await client.put(
            "/api/v1/routing/assignments/1", headers=auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["warnings"] == []

    # -- delete_entry ----------------------------------------------------

    async def test_delete_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Deleting an unknown id returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/routing/assignments/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_forbidden_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot delete a row scoped to a different org."""
        app_mock_db.return_value.select.return_value.first.return_value = _assignment_row(
            scope="org", scope_ref=999
        )

        resp = await client.delete("/api/v1/routing/assignments/1", headers=rm_auth_headers)
        assert resp.status_code == 403

    # -- Valkey cache invalidation ----------------------------------------

    async def test_cache_invalidation_skipped_when_redis_unconfigured(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """redis_client is None -> AssignmentResolver is never constructed."""
        monkeypatch.setattr(routing_assignments_mod, "redis_client", None)
        constructed: list = []

        class _FakeResolver:
            """Records constructor calls; must never be invoked when caching is off."""

            def __init__(self, *args, **kwargs) -> None:
                constructed.append((args, kwargs))

            async def invalidate(self, *args, **kwargs) -> None:
                pass

        monkeypatch.setattr("shared.routing.assignments.AssignmentResolver", _FakeResolver)
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,
            _assignment_row(),
        ]

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "chat", "model_name": "gpt-4o", "scope": "global"},
        )
        assert resp.status_code == 201
        assert constructed == []

    async def test_cache_invalidation_runs_when_redis_configured(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A configured redis_client triggers AssignmentResolver.invalidate(org_id, tool_type)."""
        invalidated: list = []

        class _FakeResolver:
            """Records invalidate() calls instead of touching real Valkey/DAL logic."""

            def __init__(self, db, valkey=None, cache_ttl=300) -> None:
                self.valkey = valkey

            async def invalidate(self, org_id, tool_type=None) -> None:
                invalidated.append((org_id, tool_type))

        # Explicit, not ambient: module-import timing determines whether
        # routing_assignments.redis_client binds to the real default (None)
        # or the test app's mock redis, so force a known non-None value
        # rather than relying on when this module happened to first import.
        monkeypatch.setattr(routing_assignments_mod, "redis_client", MagicMock())
        monkeypatch.setattr("shared.routing.assignments.AssignmentResolver", _FakeResolver)
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,
            _assignment_row(),
        ]

        resp = await client.post(
            "/api/v1/routing/assignments/",
            headers=auth_headers,
            json={"tool_type": "chat", "model_name": "gpt-4o", "scope": "global"},
        )
        assert resp.status_code == 201
        assert invalidated == [(None, "chat")]

    # -- _capability_warnings (direct) ------------------------------------

    async def test_capability_warnings_no_matching_model(self, app_mock_db: MagicMock) -> None:
        """An unknown model_name against an empty model_configs table warns, never raises."""
        warnings = await routing_assignments_mod._capability_warnings("unknown-model")
        assert warnings == ["model is not present in the registry"]

    async def test_capability_warnings_matching_available_model(
        self, app_mock_db: MagicMock
    ) -> None:
        """A known, available model_configs row produces no warnings."""
        model_row = Mock(
            model_name="gpt-4o",
            preferred_providers=[],
            cost_per_token={},
            capabilities=[],
            context_length=8192,
        )
        app_mock_db.return_value.select.return_value = make_select_result([model_row])

        warnings = await routing_assignments_mod._capability_warnings("gpt-4o")
        assert warnings == []

    # -- _can_write (direct) -- unreachable-via-HTTP branch --------------

    def test_can_write_denies_roles_other_than_admin_or_resource_manager(self) -> None:
        """A role with neither admin nor resource_manager write access is always denied.

        Unreachable through the HTTP layer (require_scope already blocks any
        other role before _can_write ever runs) -- exercised directly for
        full branch coverage of that guard.
        """
        assert routing_assignments_mod._can_write("user", 1, "org", 1) is False
        assert routing_assignments_mod._can_write("reporter", None, "global", None) is False


class TestSeedAssignments:
    """Coverage for POST /api/v1/routing/assignments/seed (admin-only, idempotent upsert)."""

    async def test_seed_requires_auth(self, client) -> None:
        """Missing auth returns 401 before the seed logic ever runs."""
        resp = await client.post("/api/v1/routing/assignments/seed")
        assert resp.status_code == 401

    async def test_seed_creates_all_when_db_is_empty(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty model_assignments table creates every DEFAULT_ASSIGNMENTS entry."""
        app_mock_db.return_value.select.return_value.first.side_effect = [None, None, None]

        resp = await client.post("/api/v1/routing/assignments/seed", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        n = len(routing_assignments_mod.DEFAULT_ASSIGNMENTS)
        assert data["data"] == {"created": n, "updated": 0, "total": n}

    async def test_seed_is_idempotent_on_rerun(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Re-running seed against already-seeded rows updates in place, creates nothing new."""
        existing = _assignment_row()
        app_mock_db.return_value.select.return_value.first.side_effect = [
            existing,
            existing,
            existing,
        ]

        resp = await client.post("/api/v1/routing/assignments/seed", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        n = len(routing_assignments_mod.DEFAULT_ASSIGNMENTS)
        assert data["data"] == {"created": 0, "updated": n, "total": n}

    async def test_seed_handles_partial_existing_rows(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """One entry already exists, the other two don't -- mixed created/updated counts."""
        existing = _assignment_row()
        app_mock_db.return_value.select.return_value.first.side_effect = [None, existing, None]

        resp = await client.post("/api/v1/routing/assignments/seed", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == {"created": 2, "updated": 1, "total": 3}


# ---------------------------------------------------------------------------
# routing_policies
# ---------------------------------------------------------------------------


class TestRoutingPolicies:
    """CRUD tests for /api/v1/routing/policies/<organization_id>."""

    async def test_get_defaults_when_no_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """No policy row yet -- engine defaults returned, defaulted=True."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/routing/policies/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["defaulted"] is True
        assert data["data"]["mode"] == "local_first"

    async def test_get_existing_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An existing policy row is returned as-is."""
        app_mock_db.return_value.select.return_value.first.return_value = _policy_row(mode="cost")

        resp = await client.get("/api/v1/routing/policies/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["mode"] == "cost"
        assert data["meta"]["defaulted"] is False

    async def test_get_other_org_forbidden_for_non_admin(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager (org 1) cannot read org 2's policy."""
        resp = await client.get("/api/v1/routing/policies/2", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_put_invalid_mode_rejected(self, client, auth_headers: dict) -> None:
        """An invalid mode value returns 400."""
        resp = await client.put(
            "/api/v1/routing/policies/1", headers=auth_headers, json={"mode": "not-a-real-mode"}
        )
        assert resp.status_code == 400

    async def test_put_deferred_de_escalation_rejected(self, client, auth_headers: dict) -> None:
        """task_detect de_escalation is deferred (spec §7.3/§14.1) -- rejected at save time."""
        resp = await client.put(
            "/api/v1/routing/policies/1",
            headers=auth_headers,
            json={"de_escalation": "task_detect"},
        )
        assert resp.status_code == 400

    async def test_put_creates_or_updates(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A valid PUT upserts the policy row."""
        app_mock_db.return_value.select.return_value.first.return_value = _policy_row()

        resp = await client.put(
            "/api/v1/routing/policies/1",
            headers=auth_headers,
            json={"mode": "cost", "classifier_prompt": "Classify this request."},
        )
        assert resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# routing_rules
# ---------------------------------------------------------------------------


class TestRoutingRules:
    """CRUD tests for /api/v1/routing/rules."""

    async def test_list_returns_entries(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A list request returns the seeded rule."""
        app_mock_db.return_value.select.return_value = make_select_result([_rule_row()])

        resp = await client.get("/api/v1/routing/rules/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"][0]["name"] == "prefer-local-chat"

    async def test_create_requires_match_and_action_objects(
        self, client, auth_headers: dict
    ) -> None:
        """match/action must be JSON objects, not strings."""
        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=auth_headers,
            json={"name": "bad-rule", "match": "not-an-object", "action": {}},
        )
        assert resp.status_code == 400

    async def test_create_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A valid rule is created and returns 201."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row()

        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=auth_headers,
            json={
                "name": "prefer-local-chat",
                "match": {"endpoint": "/v1/chat/completions"},
                "action": {"tool_type": "chat"},
            },
        )
        assert resp.status_code == 201

    async def test_create_org_scoped_other_org_forbidden(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot create a rule scoped to a different org."""
        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=rm_auth_headers,
            json={"name": "x", "match": {}, "action": {}, "organization_id": 999},
        )
        assert resp.status_code == 403

    # -- list_rules --------------------------------------------------------

    async def test_list_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/routing/rules/")
        assert resp.status_code == 401

    async def test_list_filters_enabled_true_and_false(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """enabled=true and enabled=false both take the enabled-filter branch."""
        app_mock_db.return_value.select.return_value = make_select_result([_rule_row()])

        resp_true = await client.get("/api/v1/routing/rules/?enabled=true", headers=auth_headers)
        resp_false = await client.get("/api/v1/routing/rules/?enabled=false", headers=auth_headers)
        assert resp_true.status_code == 200
        assert resp_false.status_code == 200

    async def test_list_non_admin_uses_org_scoped_visibility(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A non-admin caller exercises the org-scoped branch of _visible_query."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_rule_row(organization_id=1)]
        )

        resp = await client.get("/api/v1/routing/rules/", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_list_preserves_db_priority_order(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """The endpoint trusts the DB's priority-ordered result set, returning rows as given."""
        rows = [
            _rule_row(id=1, name="highest", priority=10),
            _rule_row(id=2, name="middle", priority=50),
            _rule_row(id=3, name="lowest", priority=200),
        ]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get("/api/v1/routing/rules/", headers=auth_headers)
        data = await resp.get_json()
        assert [r["name"] for r in data["data"]] == ["highest", "middle", "lowest"]
        assert [r["priority"] for r in data["data"]] == [10, 50, 200]

    # -- get_rule ------------------------------------------------------------

    async def test_get_rule_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A known id returns its full row, including a formatted created_at."""
        row = _rule_row(created_at=datetime(2025, 1, 1, 12, 0, 0))
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.get("/api/v1/routing/rules/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["created_at"] == "2025-01-01T12:00:00"

    async def test_get_rule_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown id returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/routing/rules/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_rule_non_admin(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A non-admin get exercises the org-scoped branch of _visible_query."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            organization_id=1
        )

        resp = await client.get("/api/v1/routing/rules/1", headers=rm_auth_headers)
        assert resp.status_code == 200

    # -- create_rule validation ----------------------------------------------

    async def test_create_requires_body(self, client, auth_headers: dict) -> None:
        """An empty JSON body ({}) is falsy and returns 400 before any field is checked."""
        resp = await client.post("/api/v1/routing/rules/", headers=auth_headers, json={})
        assert resp.status_code == 400

    @pytest.mark.parametrize("missing_field", ["name", "match", "action"])
    async def test_create_missing_required_field(
        self, client, auth_headers: dict, missing_field: str
    ) -> None:
        """Each of name/match/action is individually required."""
        payload = {"name": "r", "match": {}, "action": {}}
        del payload[missing_field]

        resp = await client.post("/api/v1/routing/rules/", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        data = await resp.get_json()
        assert missing_field in data["error"]

    async def test_create_uses_priority_and_enabled_defaults(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Omitting priority/enabled falls back to priority=100, enabled=True."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            priority=100, enabled=True
        )

        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=auth_headers,
            json={"name": "defaulted", "match": {}, "action": {}},
        )
        assert resp.status_code == 201
        assert app_mock_db.routing_rules_v2.insert.call_args.kwargs["priority"] == 100
        assert app_mock_db.routing_rules_v2.insert.call_args.kwargs["enabled"] is True

    async def test_create_honors_explicit_priority_and_disabled(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An explicit priority/enabled value is passed through, not overridden by the default."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            priority=5, enabled=False
        )

        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=auth_headers,
            json={"name": "custom", "match": {}, "action": {}, "priority": 5, "enabled": False},
        )
        assert resp.status_code == 201
        assert app_mock_db.routing_rules_v2.insert.call_args.kwargs["priority"] == 5
        assert app_mock_db.routing_rules_v2.insert.call_args.kwargs["enabled"] is False

    async def test_create_rm_own_org_success(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager can create a rule scoped to their own org."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            organization_id=1
        )

        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=rm_auth_headers,
            json={"name": "own-org", "match": {}, "action": {}, "organization_id": 1},
        )
        assert resp.status_code == 201

    async def test_create_rm_global_rule_forbidden(self, client, rm_auth_headers: dict) -> None:
        """resource_manager cannot create a GLOBAL rule (organization_id omitted -> None)."""
        resp = await client.post(
            "/api/v1/routing/rules/",
            headers=rm_auth_headers,
            json={"name": "global-attempt", "match": {}, "action": {}},
        )
        assert resp.status_code == 403

    # -- update_rule -----------------------------------------------------

    async def test_update_requires_body(self, client, auth_headers: dict) -> None:
        """An empty JSON body ({}) returns 400."""
        resp = await client.put("/api/v1/routing/rules/1", headers=auth_headers, json={})
        assert resp.status_code == 400

    async def test_update_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Updating a non-existent rule returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/routing/rules/999", headers=auth_headers, json={"name": "x"}
        )
        assert resp.status_code == 404

    async def test_update_forbidden_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot update a rule belonging to a different org."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            organization_id=999
        )

        resp = await client.put(
            "/api/v1/routing/rules/1", headers=rm_auth_headers, json={"name": "hijack"}
        )
        assert resp.status_code == 403

    async def test_update_no_valid_fields_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A body containing only non-writable keys returns 400."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row()

        resp = await client.put(
            "/api/v1/routing/rules/1", headers=auth_headers, json={"id": 999, "created_at": "x"}
        )
        assert resp.status_code == 400

    async def test_update_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A valid field update returns the updated row."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            name="renamed", priority=1, enabled=False
        )

        resp = await client.put(
            "/api/v1/routing/rules/1",
            headers=auth_headers,
            json={"name": "renamed", "priority": 1, "enabled": False},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["name"] == "renamed"
        assert data["data"]["enabled"] is False

    # -- delete_rule -----------------------------------------------------

    async def test_delete_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Deleting an unknown rule returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/routing/rules/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_forbidden_for_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager cannot delete a rule belonging to a different org."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row(
            organization_id=999
        )

        resp = await client.delete("/api/v1/routing/rules/1", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_delete_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """Admin can delete an existing rule."""
        app_mock_db.return_value.select.return_value.first.return_value = _rule_row()

        resp = await client.delete("/api/v1/routing/rules/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "deleted"


# ---------------------------------------------------------------------------
# model_aliases
# ---------------------------------------------------------------------------


class TestModelAliases:
    """CRUD tests for /api/v1/routing/aliases."""

    async def test_list_returns_entries(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A list request returns the seeded alias."""
        app_mock_db.return_value.select.return_value = make_select_result([_alias_row()])

        resp = await client.get("/api/v1/routing/aliases/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"][0]["source_model"] == "gpt-4o"

    async def test_create_rejects_identical_source_and_target(
        self, client, auth_headers: dict
    ) -> None:
        """source_model and target_model must differ."""
        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=auth_headers,
            json={"source_model": "gpt-4o", "target_model": "gpt-4o"},
        )
        assert resp.status_code == 400

    async def test_create_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A valid alias is created or upserted."""
        app_mock_db.return_value.select.return_value.first.return_value = _alias_row()

        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=auth_headers,
            json={"source_model": "gpt-4o", "target_model": "mistral-large"},
        )
        assert resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# routing_decisions
# ---------------------------------------------------------------------------


class TestRoutingDecisions:
    """Read-only view tests for /api/v1/routing/decisions."""

    async def test_get_trace_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown request_id returns 404."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/routing/decisions/unknown-request-id", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_get_trace_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A known request_id returns its full trace."""
        app_mock_db.return_value.select.return_value = make_select_result([_trace_row()])

        resp = await client.get("/api/v1/routing/decisions/req-1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["request_id"] == "req-1"
        assert data["data"]["tool_type_source"] == "heuristic"

    async def test_summary_aggregates_rates(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """veto_rate/escalation_rate reflect the fraction of traces flagged."""
        rows = [
            _trace_row(id=1, capability_veto=True, escalated=False, tool_type_source="heuristic"),
            _trace_row(id=2, capability_veto=False, escalated=True, tool_type_source="classifier"),
            _trace_row(id=3, capability_veto=False, escalated=False, tool_type_source="heuristic"),
            _trace_row(id=4, capability_veto=False, escalated=False, tool_type_source="explicit"),
        ]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get("/api/v1/routing/decisions/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        summary = data["data"]
        assert summary["total"] == 4
        assert summary["veto_rate"] == 0.25
        assert summary["escalation_rate"] == 0.25
        assert summary["by_tool_type_source"] == {"heuristic": 2, "classifier": 1, "explicit": 1}

    async def test_summary_non_admin_scoped_to_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A non-admin caller's summary is always scoped to their own org."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/routing/decisions/?org=999", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        # org query param is ignored for non-admins -- always their own org (1).
        assert data["meta"]["organization_id"] == 1


# ---------------------------------------------------------------------------
# routing_dry_run
# ---------------------------------------------------------------------------

_DRY_RUN_FLAG_ENV = "WADDLEAI_FLAG_ROUTING_DRY_RUN"


class TestRoutingDryRun:
    """Tests for the admin-only /api/v1/routing/dry-run endpoint.

    Genuine replacement for the retired static-response /routing-matrix/test
    -- these assertions specifically prove the response is computed from the
    supplied input (varies with tool_type/prompt) rather than hardcoded, and
    that RoutingEngine.decide(persist=False) writes no decision trace.
    """

    async def test_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.post("/api/v1/routing/dry-run/", json={"prompt": "hi"})
        assert resp.status_code == 401

    async def test_non_admin_forbidden(self, client, rm_auth_headers: dict) -> None:
        """resource_manager (or any non-admin role) cannot run a dry-run decision."""
        resp = await client.post(
            "/api/v1/routing/dry-run/", headers=rm_auth_headers, json={"prompt": "hi"}
        )
        assert resp.status_code == 403

    async def test_missing_prompt_rejected(self, client, auth_headers: dict) -> None:
        """An empty body / missing prompt is rejected before the flag or engine ever run."""
        resp = await client.post("/api/v1/routing/dry-run/", headers=auth_headers, json={})
        assert resp.status_code == 400

    async def test_blank_prompt_rejected(self, client, auth_headers: dict) -> None:
        """A whitespace-only prompt is rejected."""
        resp = await client.post(
            "/api/v1/routing/dry-run/", headers=auth_headers, json={"prompt": "   "}
        )
        assert resp.status_code == 400

    async def test_invalid_tool_type_type_rejected(self, client, auth_headers: dict) -> None:
        """tool_type must be a string, not e.g. an integer."""
        resp = await client.post(
            "/api/v1/routing/dry-run/",
            headers=auth_headers,
            json={"prompt": "hi", "tool_type": 123},
        )
        assert resp.status_code == 400

    async def test_invalid_organization_id_rejected(self, client, auth_headers: dict) -> None:
        """A non-integer organization_id is rejected."""
        resp = await client.post(
            "/api/v1/routing/dry-run/",
            headers=auth_headers,
            json={"prompt": "hi", "organization_id": "not-an-int"},
        )
        assert resp.status_code == 400

    async def test_flag_disabled_returns_403(self, client, auth_headers: dict, monkeypatch) -> None:
        """The endpoint is gated behind waddleai.routing-dry-run, default OFF."""
        monkeypatch.delenv(_DRY_RUN_FLAG_ENV, raising=False)
        monkeypatch.delenv("POSTHOG_KEY", raising=False)

        resp = await client.post(
            "/api/v1/routing/dry-run/", headers=auth_headers, json={"prompt": "hi"}
        )
        assert resp.status_code == 403

    async def test_dry_run_reflects_explicit_tool_type_and_writes_nothing(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A genuine (input-dependent) decision is returned, with zero persistence side effects.

        No model_configs/model_assignments rows are seeded, so RoutingEngine
        falls through its documented last-resort default ("gpt-4") -- the
        point of this test is that tool_type/tool_type_source echo the
        *supplied* input (proving real computation, not a canned response)
        while nothing is written to the database.
        """
        monkeypatch.setenv(_DRY_RUN_FLAG_ENV, "1")

        resp = await client.post(
            "/api/v1/routing/dry-run/",
            headers=auth_headers,
            json={"prompt": "Write a bubble sort in Python", "tool_type": "code"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        data = body["data"]
        assert data["tool_type"] == "code"
        assert data["tool_type_source"] == "explicit"
        assert data["model"] == "gpt-4"
        assert "confidence" not in data
        assert body["meta"]["persisted"] is False

        # Proves the dry run truly mutates nothing: no trace row inserted,
        # no commit issued anywhere during the request.
        app_mock_db.routing_decision_traces.insert.assert_not_called()
        app_mock_db.commit.assert_not_called()

    async def test_dry_run_without_explicit_tool_type_degrades_to_classifier_default(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """No explicit tool_type + no classifier connector -> the documented safe default.

        Different input (no tool_type hint) yields a different tool_type/
        source than the explicit-hint test above -- further proof this is a
        live decision, not a hardcoded string.
        """
        monkeypatch.setenv(_DRY_RUN_FLAG_ENV, "1")

        resp = await client.post(
            "/api/v1/routing/dry-run/", headers=auth_headers, json={"prompt": "hello there"}
        )
        assert resp.status_code == 200
        data = (await resp.get_json())["data"]
        assert data["tool_type"] == "general"
        assert data["tool_type_source"] == "classifier"

        app_mock_db.routing_decision_traces.insert.assert_not_called()
        app_mock_db.commit.assert_not_called()

    async def test_admin_can_target_another_organization_id(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Admin may override organization_id to dry-run against another org's config."""
        monkeypatch.setenv(_DRY_RUN_FLAG_ENV, "1")

        resp = await client.post(
            "/api/v1/routing/dry-run/",
            headers=auth_headers,
            json={"prompt": "hi", "organization_id": 42},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["organization_id"] == 42
