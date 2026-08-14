"""Unit tests for the smart-routing admin API (spec §7.6, Task 14).

Covers routing_assignments, routing_policies, routing_rules, model_aliases,
and routing_decisions.
"""

from unittest.mock import MagicMock, Mock

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

    async def test_create_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_create_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
