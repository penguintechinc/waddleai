"""Tests for /api/v1/memory-scoping + /api/v1/memory/<id>/{promote,correct,dispute} (§9.4/§9.7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.unit.management.conftest import make_dal_row, make_select_result


@pytest.fixture(autouse=True)
def _stub_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip NER/transformers init in ContentFilter -- deterministic, no network."""
    monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")


def _memory_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    defaults = dict(
        id=1,
        user_id=1,
        organization_id=1,
        session_id="session-1",
        content="the API port is 8000",
        role="user",
        scope_type="session",
        scope_ref="session-1",
        author_user_id=1,
        trust_tier="unverified",
        version=1,
        status="active",
        provenance=None,
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(row, key, value)
    return row


class TestMemoryConfigDefaults:
    """(a) POST/GET /api/v1/memory-scoping seeds/returns §9.4 defaults (0.7 cutoff, top-3)."""

    async def test_get_returns_seeded_defaults_when_unconfigured(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """No existing config row -> the §9.4 hardcoded defaults are returned."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/memory-scoping?organization_id=1", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["relevance_cutoff"] == 0.7
        assert data["top_k"] == 3
        assert data["configured"] is False

    async def test_post_creates_config_with_custom_cutoff(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """Posting a config with an explicit cutoff persists it."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/memory-scoping",
            headers=auth_headers,
            json={"organization_id": 1, "relevance_cutoff": 0.8},
        )

        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["status"] == "created"
        app_mock_db.conversation_memory_configs.insert.assert_called_once()
        insert_kwargs = app_mock_db.conversation_memory_configs.insert.call_args.kwargs
        assert insert_kwargs["similarity_threshold"] == 0.8

    async def test_post_updates_existing_config_via_db_update(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """Verify the update path uses db(id==...).update(), not Row.update_record().

        Real penguin_dal Rows have no update_record() (regression: see
        shared/auth/rbac.py). `existing` is built with make_dal_row (spec'd,
        no update_record), so a regression back to the old call raises
        AttributeError here exactly like it would in production, instead of
        silently succeeding against an auto-attribute MagicMock.
        """
        existing = make_dal_row(id=7, organization_id=1, enabled=True, similarity_threshold=0.7)
        app_mock_db.return_value.select.return_value = make_select_result([existing])

        resp = await client.post(
            "/api/v1/memory-scoping",
            headers=auth_headers,
            json={"organization_id": 1, "relevance_cutoff": 0.85, "enabled": False},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "updated"
        app_mock_db.return_value.update.assert_called_once()
        call_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert call_kwargs["similarity_threshold"] == 0.85
        assert call_kwargs["enabled"] is False


class TestMemoryPromote:
    """(b) memory_promote moves session-scope items broader; explicit-only, owner/admin-gated."""

    async def test_owner_can_promote_session_memory_to_repo(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """The memory's owner (admin token here) can promote it to repo scope."""
        row = _memory_row(author_user_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/memory/1/promote",
            headers=auth_headers,
            json={"target_scope": "repo", "scope_ref": "repo-42"},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["scope_type"] == "repo"
        assert data["scope_ref"] == "repo-42"
        app_mock_db.return_value.update.assert_called_once()
        assert app_mock_db.return_value.update.call_args.kwargs["scope_type"] == "repo"

    async def test_non_owner_non_admin_promote_rejected(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """A non-owner, non-admin caller cannot promote someone else's memory -- security."""
        row = _memory_row(author_user_id=999)  # owned by a different user
        app_mock_db.return_value.select.return_value = make_select_result([row])
        calls_before = app_mock_db.return_value.update.call_count

        resp = await client.post(
            "/api/v1/memory/1/promote",
            headers=user_auth_headers,
            json={"target_scope": "repo", "scope_ref": "repo-42"},
        )

        assert resp.status_code == 403
        assert app_mock_db.return_value.update.call_count == calls_before

    async def test_invalid_target_scope_rejected(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """session/user are not valid promotion targets -- only repo/project/org."""
        resp = await client.post(
            "/api/v1/memory/1/promote", headers=auth_headers, json={"target_scope": "session"}
        )
        assert resp.status_code == 400


class TestMemoryCorrect:
    """(c)+(e) memory_correct versions + supersedes; contradiction resolved by trust."""

    async def test_higher_trust_correction_supersedes_original(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """A confirmed-trust correction supersedes an unverified original."""
        row = _memory_row(author_user_id=1, trust_tier="unverified", version=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        app_mock_db.memory_embeddings.insert.return_value = 99

        resp = await client.post(
            "/api/v1/memory/1/correct",
            headers=auth_headers,
            json={"content": "the API port is 9000"},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "corrected"
        assert data["new_id"] == 99
        assert data["version"] == 2
        insert_kwargs = app_mock_db.memory_embeddings.insert.call_args.kwargs
        assert insert_kwargs["status"] == "active"
        assert insert_kwargs["version"] == 2
        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert update_kwargs["status"] == "quarantined"
        assert update_kwargs["superseded_by"] == 99

    async def test_lower_trust_correction_of_verified_fact_is_quarantined_instead(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """A correction attempt against a verified fact loses -- the correction is quarantined."""
        row = _memory_row(author_user_id=1, trust_tier="verified", version=3)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        app_mock_db.memory_embeddings.insert.return_value = 100

        resp = await client.post(
            "/api/v1/memory/1/correct", headers=auth_headers, json={"content": "actually it's 1234"}
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "correction_quarantined"
        insert_kwargs = app_mock_db.memory_embeddings.insert.call_args.kwargs
        assert insert_kwargs["status"] == "quarantined"
        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        # The original stays active -- it won by trust.
        assert update_kwargs["status"] == "active"
        assert update_kwargs["superseded_by"] is None

    async def test_non_owner_non_admin_correction_rejected(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """A non-owner, non-admin cannot correct someone else's memory."""
        row = _memory_row(author_user_id=999)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/memory/1/correct", headers=user_auth_headers, json={"content": "new content"}
        )

        assert resp.status_code == 403

    async def test_all_mutations_attributable(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """(g) The correcting user is recorded as author_user_id -- no anonymous writes."""
        row = _memory_row(author_user_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        app_mock_db.memory_embeddings.insert.return_value = 101

        await client.post(
            "/api/v1/memory/1/correct", headers=auth_headers, json={"content": "corrected content"}
        )

        insert_kwargs = app_mock_db.memory_embeddings.insert.call_args.kwargs
        assert insert_kwargs["author_user_id"] is not None


class TestMemoryDispute:
    """(d) memory_dispute sets status='quarantined' pending review; attributable."""

    async def test_dispute_quarantines_and_records_disputer(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """Disputing a memory sets status='quarantined' and records who disputed it."""
        row = _memory_row()
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post("/api/v1/memory/1/dispute", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "quarantined"
        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert update_kwargs["status"] == "quarantined"
        assert update_kwargs["provenance"]["disputed_by"] is not None

    async def test_dispute_missing_memory_404s(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """Disputing a nonexistent/other-org memory 404s."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post("/api/v1/memory/999/dispute", headers=auth_headers)

        assert resp.status_code == 404


class TestNoAuth:
    """Every memory-scoping route requires authentication."""

    async def test_promote_requires_auth(self, client) -> None:
        """No auth header -> 401 on the promote route too."""
        resp = await client.post("/api/v1/memory/1/promote", json={"target_scope": "repo"})
        assert resp.status_code == 401


class TestGetMemoryScopingTenantIsolation:
    """GET /api/v1/memory-scoping must not honour a caller-supplied organization_id.

    regression: audit-2026-09-14 (MEDIUM, memory_scoping.py:83) -- the
    ``organization_id`` query parameter used to win over the token's own
    org unconditionally, so any authenticated user could read another
    org's memory-injection settings with ``?organization_id=<other>``.
    """

    async def test_cross_org_read_via_query_param_is_refused(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- org-1 user asking for org 2 gets 403.

        The refusal must not carry org 2's config in any form.
        """
        other_org_config = make_dal_row(
            id=99, organization_id=2, enabled=True, similarity_threshold=0.42
        )
        app_mock_db.return_value.select.return_value = make_select_result([other_org_config])

        resp = await client.get(
            "/api/v1/memory-scoping?organization_id=2", headers=user_auth_headers
        )

        assert resp.status_code == 403
        body = await resp.get_data(as_text=True)
        # The refusal must not carry the other org's settings in any form.
        assert "0.42" not in body
        data = await resp.get_json()
        assert data.get("organization_id") is None
        assert "configured" not in data

    async def test_cross_org_read_is_refused_not_silently_substituted(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- explicit mismatch 403s, never a silent substitution."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/memory-scoping?organization_id=2", headers=user_auth_headers
        )

        # A silent fallback to the caller's own org would return 200 here and
        # make "does org 2 exist" indistinguishable from "org 2 is mine".
        assert resp.status_code == 403

    async def test_own_org_read_still_allowed_for_non_admin(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- naming your own org explicitly is still fine."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/memory-scoping?organization_id=1", headers=user_auth_headers
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["organization_id"] == 1

    async def test_omitted_param_falls_back_to_callers_own_org(
        self, client, app_mock_db: MagicMock, user_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- with no parameter the token's org is used."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/memory-scoping", headers=user_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["organization_id"] == 1

    async def test_memory_scoping_admin_may_read_another_org(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- a memory_scoping:admin holder keeps cross-org reads."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/memory-scoping?organization_id=2", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["organization_id"] == 2

    async def test_resource_manager_lacking_the_scope_is_refused(
        self, client, app_mock_db: MagicMock, rm_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- resource_manager holds no memory_scoping:admin."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/memory-scoping?organization_id=2", headers=rm_auth_headers)

        assert resp.status_code == 403


class TestMemoryMutationTenantIsolation:
    """promote/correct/dispute must refuse a memory belonging to another org.

    regression: audit-2026-09-14 -- these three routes carry @require_auth
    with no @require_scope, so their tenant boundary rests entirely on the
    `organization_id` term in each handler's own select. These tests pin
    that boundary: the row the DB hands back is deliberately one from org 2
    while the caller is in org 1, which is what the route would see if that
    query term were ever dropped or widened.
    """

    async def test_promote_refuses_another_orgs_memory(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- an org-2 row is "not found" to an org-1 admin."""
        foreign = _memory_row(organization_id=2, author_user_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])

        resp = await client.post(
            "/api/v1/memory/1/promote", headers=auth_headers, json={"target_scope": "org"}
        )

        assert resp.status_code == 404
        # Admin bypasses the *ownership* check but never the tenant check.
        app_mock_db.return_value.update.assert_not_called()

    async def test_correct_refuses_another_orgs_memory(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- correction of an org-2 row is refused."""
        foreign = _memory_row(organization_id=2, author_user_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])

        resp = await client.post(
            "/api/v1/memory/1/correct", headers=auth_headers, json={"content": "new text"}
        )

        assert resp.status_code == 404
        app_mock_db.return_value.update.assert_not_called()

    async def test_dispute_refuses_another_orgs_memory(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- disputing an org-2 row cannot quarantine it."""
        foreign = _memory_row(organization_id=2, author_user_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])

        resp = await client.post("/api/v1/memory/1/dispute", headers=auth_headers, json={})

        assert resp.status_code == 404
        # /dispute has no ownership check by design (any member may dispute a
        # shared memory), so the tenant check is its only guard -- it must
        # never reach the quarantining update.
        app_mock_db.return_value.update.assert_not_called()

    async def test_dispute_still_quarantines_an_own_org_memory(
        self, client, app_mock_db: MagicMock, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14 -- the tenant guard does not block legitimate disputes."""
        own = _memory_row(organization_id=1, author_user_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([own])

        resp = await client.post("/api/v1/memory/1/dispute", headers=auth_headers, json={})

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "quarantined"


# ============================================================================
# Wave-2 audit: role-name -> scope conversion + response-schema hardening
# ============================================================================


def _scoped_nonadmin_headers() -> dict[str, str]:
    """A bearer token whose role is NOT admin but whose scope carries memory_scoping:admin.

    This is the token that distinguishes a scope check from a role-name
    check: under the old ``role == "admin"`` gate it is refused (the role is
    resource_manager), under the scope gate it is allowed. Minted through the
    same provider ``flask_app`` patches ``auth._get_oidc_provider`` to, so it
    verifies like any other test token.
    """
    from services.management.app.api.v1 import auth as auth_mod
    from shared.auth.penguin_auth import issue_token
    from shared.auth.rbac import Permission, Role, UserContext

    provider = auth_mod._get_oidc_provider()
    ctx = UserContext(
        user_id=4242,
        username="scoped-nonadmin",
        role=Role.RESOURCE_MANAGER,
        organization_id=1,
        managed_orgs=[],
        permissions={Permission.MEMORY_SCOPING_ADMIN},
    )
    token = issue_token(ctx, provider)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class TestPromoteCorrectScopeAuthorization:
    """promote/correct authorize on the memory_scoping:admin SCOPE, not the role name.

    regression: audit-2026-09-14-wave2 (house policy, security.md: "middleware
    checks scopes only, never role names") -- these two routes gated the
    non-owner path on ``role == "admin"``. The tokens below carry the admin
    SCOPE while their role claim is resource_manager, so they are refused by
    the old role check and allowed by the new scope check -- the divergence
    that proves the conversion.
    """

    async def test_scope_holder_without_admin_role_may_promote_others_memory(
        self, client, flask_app, app_mock_db
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- memory_scoping:admin scope permits promote."""
        headers = _scoped_nonadmin_headers()
        row = _memory_row(author_user_id=999, organization_id=1)  # owned by another user
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/memory/1/promote",
            headers=headers,
            json={"target_scope": "repo", "scope_ref": "repo-9"},
        )

        assert resp.status_code == 200

    async def test_scope_holder_without_admin_role_may_correct_others_memory(
        self, client, flask_app, app_mock_db
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- memory_scoping:admin scope permits correct."""
        headers = _scoped_nonadmin_headers()
        row = _memory_row(author_user_id=999, organization_id=1, trust_tier="unverified")
        app_mock_db.return_value.select.return_value = make_select_result([row])
        app_mock_db.memory_embeddings.insert.return_value = 555

        resp = await client.post(
            "/api/v1/memory/1/correct",
            headers=headers,
            json={"content": "the corrected value"},
        )

        assert resp.status_code == 200

    async def test_plain_non_owner_without_the_scope_still_refused(
        self, client, app_mock_db, user_auth_headers
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- a non-owner lacking the scope is still 403."""
        row = _memory_row(author_user_id=999, organization_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.post(
            "/api/v1/memory/1/promote",
            headers=user_auth_headers,
            json={"target_scope": "repo", "scope_ref": "repo-9"},
        )

        assert resp.status_code == 403


class TestMemoryScopingBounds:
    """POST /memory-scoping range-checks relevance_cutoff before it reaches the DB."""

    async def test_relevance_cutoff_out_of_range_400(
        self, client, app_mock_db, auth_headers
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- a >1 cutoff is refused, not persisted."""
        app_mock_db.conversation_memory_configs.insert.reset_mock()
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/memory-scoping",
            headers=auth_headers,
            json={"organization_id": 1, "relevance_cutoff": 9.0},
        )

        assert resp.status_code == 400
        app_mock_db.conversation_memory_configs.insert.assert_not_called()


class TestMemoryScopingResponseSchema:
    """@validate_response pins the exact field set every route emits."""

    async def test_get_field_set(self, client, app_mock_db, auth_headers) -> None:
        """# regression: audit-2026-09-14-wave2 -- GET /memory-scoping field set is fixed."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get("/api/v1/memory-scoping?organization_id=1", headers=auth_headers)
        data = await resp.get_json()
        assert set(data.keys()) == {
            "organization_id",
            "enabled",
            "relevance_cutoff",
            "top_k",
            "configured",
        }

    async def test_post_field_set(self, client, app_mock_db, auth_headers) -> None:
        """# regression: audit-2026-09-14-wave2 -- POST /memory-scoping carries status+org+top_k."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.post(
            "/api/v1/memory-scoping", headers=auth_headers, json={"organization_id": 1}
        )
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "organization_id", "top_k"}

    async def test_promote_field_set(self, client, app_mock_db, auth_headers) -> None:
        """# regression: audit-2026-09-14-wave2 -- promote carries the fixed field set."""
        row = _memory_row(author_user_id=1, organization_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        resp = await client.post(
            "/api/v1/memory/1/promote",
            headers=auth_headers,
            json={"target_scope": "repo", "scope_ref": "repo-1"},
        )
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "id", "scope_type", "scope_ref"}

    async def test_dispute_field_set(self, client, app_mock_db, auth_headers) -> None:
        """# regression: audit-2026-09-14-wave2 -- dispute carries status+id+disputed_by."""
        row = _memory_row(organization_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        resp = await client.post("/api/v1/memory/1/dispute", headers=auth_headers, json={})
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "id", "disputed_by"}
