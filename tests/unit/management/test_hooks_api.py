"""Unit tests for the agent-hooks adapter contract: /api/v1/hooks/{evaluate,telemetry,policy}."""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result


class TestEvaluateAuthAndFlag:
    """Auth requirement + feature-flag-off proof (§14.2 flag-off = zero behavior change)."""

    async def test_evaluate_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.post("/api/v1/hooks/evaluate", json={})
        assert resp.status_code == 401

    async def test_evaluate_flag_off_always_allows(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """With the flag off (no env override, default OFF), evaluate always returns allow."""
        resp = await client.post(
            "/api/v1/hooks/evaluate",
            headers=auth_headers,
            json={
                "hook_version": "1",
                "ecosystem": "claude-code",
                "event": "pre_tool_use",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "cat .env"},
                "workspace_path": None,
                "occurred_at": "2026-08-19T00:00:00Z",
            },
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["decision"] == "allow"
        assert data["rule_id"] is None
        assert "evaluated_in_ms" in data


class TestEvaluateContractValidation:
    """Request-shape validation (flag on -- an off flag returns allow before body parsing)."""

    async def test_missing_body_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An empty body is rejected with 400."""
        monkeypatch.setenv("WADDLEAI_FLAG_AGENT_HOOKS", "1")
        resp = await client.post("/api/v1/hooks/evaluate", headers=auth_headers, json=None)
        assert resp.status_code in (400, 415)


class TestEvaluateTier1Denylist:
    """Flag-on: Tier 1 denylist is checked and is unconditional."""

    async def test_denylist_hit_denies(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A `.env` tool_input is denied via the builtin Tier-1 denylist, flag on."""
        monkeypatch.setenv("WADDLEAI_FLAG_AGENT_HOOKS", "1")
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/hooks/evaluate",
            headers=auth_headers,
            json={
                "hook_version": "1",
                "ecosystem": "claude-code",
                "event": "pre_tool_use",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "cat .env"},
                "workspace_path": "/repo",
                "occurred_at": "2026-08-19T00:00:00Z",
            },
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["decision"] == "deny"
        assert data["rule_id"] is None

    async def test_benign_tool_call_allowed(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A benign tool call with no matching rules falls through to allow (Tier 2 disabled)."""
        monkeypatch.setenv("WADDLEAI_FLAG_AGENT_HOOKS", "1")
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/hooks/evaluate",
            headers=auth_headers,
            json={
                "hook_version": "1",
                "ecosystem": "claude-code",
                "event": "pre_tool_use",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
                "workspace_path": "/repo",
                "occurred_at": "2026-08-19T00:00:00Z",
            },
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["decision"] == "allow"


class TestTelemetry:
    """POST /api/v1/hooks/telemetry -- fire-and-forget, never blocks."""

    async def test_telemetry_accepted(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A well-formed telemetry event returns 202 immediately."""
        resp = await client.post(
            "/api/v1/hooks/telemetry",
            headers=auth_headers,
            json={
                "hook_version": "1",
                "ecosystem": "claude-code",
                "event": "post_tool_use",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "occurred_at": "2026-08-19T00:00:00Z",
            },
        )
        assert resp.status_code == 202
        data = await resp.get_json()
        assert data["accepted"] is True

    async def test_telemetry_invalid_event_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown event value is rejected with 400 before any background task is scheduled."""
        resp = await client.post(
            "/api/v1/hooks/telemetry",
            headers=auth_headers,
            json={"ecosystem": "claude-code", "event": "not_a_real_event", "tool_name": "Bash"},
        )
        assert resp.status_code == 400


class TestTelemetryPrivacy:
    """§18.5 privacy constraint: tool_input is hashed always, raw only when opted in.

    Exercised directly against `_persist_telemetry` rather than through the
    HTTP round trip: the route schedules persistence via
    `current_app.add_background_task`, which is fire-and-forget by design
    (never awaited by the request handler) -- asserting on it here, at the
    function that actually performs the write, is the deterministic place
    to prove the privacy behavior rather than racing a background task.
    """

    async def test_raw_payload_not_persisted_when_opt_in_off(
        self, app_mock_db: MagicMock, flask_app
    ) -> None:
        """capture_raw_payloads defaults False -- tool_input_raw is always None in the insert."""
        from services.management.app.api.v1.hooks import _persist_telemetry

        # no hook_configs row -> resolves to the hardcoded floor
        app_mock_db.return_value.select.return_value = make_select_result([])

        async with flask_app.app_context():
            await _persist_telemetry(
                "claude-code", "pre_tool_use", "Bash", "s1",
                {"command": "cat ~/.ssh/id_rsa"}, "org-1", "2026-08-19T00:00:00Z",
            )

        insert_kwargs = app_mock_db.hook_telemetry_events.insert.call_args.kwargs
        assert insert_kwargs["tool_input_raw"] is None
        assert insert_kwargs["tool_input_hash"]  # always populated
        assert len(insert_kwargs["tool_input_hash"]) == 64  # sha256 hex

    async def test_raw_payload_persisted_when_opt_in_on(
        self, app_mock_db: MagicMock, flask_app
    ) -> None:
        """capture_raw_payloads=True on the resolved config -- tool_input_raw is populated."""
        from services.management.app.api.v1.hooks import _persist_telemetry

        config_row = MagicMock()
        config_row.scope_type = "org"
        config_row.scope_ref = "org-1"
        config_row.remote_eval_enabled = None
        config_row.remote_eval_timeout_ms = None
        config_row.remote_eval_fail_mode = None
        config_row.capture_raw_payloads = True
        app_mock_db.return_value.select.return_value = make_select_result([config_row])

        async with flask_app.app_context():
            await _persist_telemetry(
                "claude-code", "pre_tool_use", "Bash", "s1",
                {"command": "ls"}, "org-1", None,
            )

        insert_kwargs = app_mock_db.hook_telemetry_events.insert.call_args.kwargs
        assert insert_kwargs["tool_input_raw"] == {"command": "ls"}


class TestGetPolicy:
    """GET /api/v1/hooks/policy -- Tier-1 denylist sync endpoint."""

    async def test_returns_builtin_patterns(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """With no admin-added entries, the response is exactly the builtin seed list."""
        from shared.security.hooks_denylist import BUILTIN_DENYLIST_PATTERNS

        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/hooks/policy", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert set(data["denylist_patterns"]) == set(BUILTIN_DENYLIST_PATTERNS)
        assert "updated_at" in data

    async def test_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/hooks/policy")
        assert resp.status_code == 401
