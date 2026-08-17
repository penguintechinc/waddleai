"""Tests for /api/v1/memory-scoping + /api/v1/memory/<id>/{promote,correct,dispute} (§9.4/§9.7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.unit.management.conftest import make_select_result


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
