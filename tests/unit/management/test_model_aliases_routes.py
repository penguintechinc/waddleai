"""Unit tests for model alias routes: /api/v1/routing/aliases/*.

Covers full CRUD, validation 400s, the create-time 409-equivalent upsert
branch, org-scoped write authorization (`_can_write`), visibility-query
construction (`_visible_query`), and the response envelope shape.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.management.app.api.v1 import model_aliases
from tests.unit.management.conftest import make_dal_row, make_select_result

# ---------------------------------------------------------------------------
# Local fixtures/helpers
# ---------------------------------------------------------------------------


def _make_alias_row(
    *,
    alias_id: int = 1,
    organization_id: int | None = None,
    source_model: str = "gpt-4o",
    target_model: str = "mistral-large",
    target_provider: str | None = None,
    enabled: bool = True,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a spec'd fake `model_aliases` row (mirrors `_row_to_dict`'s field set).

    Uses `make_dal_row` (spec'd, no `.update_record()`) rather than a bare
    `MagicMock()` so a route accidentally calling a PyDAL-only method on the
    row would fail loudly instead of silently succeeding.
    """
    return make_dal_row(
        id=alias_id,
        organization_id=organization_id,
        source_model=source_model,
        target_model=target_model,
        target_provider=target_provider,
        enabled=enabled,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class _Expr:
    """Minimal, comparable stand-in for a PyDAL query expression.

    Records the operation performed instead of behaving like a real query,
    so `_visible_query`'s branch outcome can be asserted by value equality.
    """

    op: str
    field: str = ""
    value: Any = None
    left: "_Expr | None" = None
    right: "_Expr | None" = None

    def __or__(self, other: "_Expr") -> "_Expr":
        """Combine two expressions the way PyDAL's `|` operator would."""
        return _Expr(op="or", left=self, right=other)


class _FakeAliasField:
    """Records the comparison performed against a `model_aliases` column."""

    def __init__(self, name: str) -> None:
        """Bind this fake field to the column name it represents."""
        self._name = name

    def __gt__(self, other: Any) -> _Expr:
        """Record a greater-than comparison."""
        return _Expr(op="gt", field=self._name, value=other)

    def __eq__(self, other: Any) -> _Expr:  # type: ignore[override]
        """Record an equality comparison."""
        return _Expr(op="eq", field=self._name, value=other)


class _FakeAliasTable:
    """Fake `db.model_aliases` exposing only the columns `_visible_query` reads."""

    def __init__(self) -> None:
        """Build the two fake fields `_visible_query` touches."""
        self.id = _FakeAliasField("id")
        self.organization_id = _FakeAliasField("organization_id")


class _FakeDBHandle:
    """Stand-in for the module-level `db` object, scoped to `_visible_query`."""

    def __init__(self) -> None:
        """Expose a single fake `model_aliases` table."""
        self.model_aliases = _FakeAliasTable()


# ---------------------------------------------------------------------------
# _row_to_dict
# ---------------------------------------------------------------------------


class TestRowToDict:
    """Tests for the row -> serializable dict conversion."""

    def test_with_created_at(self) -> None:
        """A populated created_at is rendered as an ISO-8601 string."""
        row = _make_alias_row(alias_id=1, created_at=datetime(2025, 1, 1, 12, 0, 0))
        result = model_aliases._row_to_dict(row)
        assert result["created_at"] == "2025-01-01T12:00:00"

    def test_without_created_at(self) -> None:
        """A NULL created_at is rendered as None, not an error."""
        row = _make_alias_row(
            alias_id=2, organization_id=1, target_provider="openai", created_at=None
        )
        result = model_aliases._row_to_dict(row)
        assert result["created_at"] is None
        assert result["organization_id"] == 1
        assert result["target_provider"] == "openai"


# ---------------------------------------------------------------------------
# _visible_query
# ---------------------------------------------------------------------------


class TestVisibleQuery:
    """Tests for the admin-vs-scoped visibility query builder."""

    def test_admin_sees_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admins get an unrestricted id>0 filter regardless of org."""
        monkeypatch.setattr(model_aliases, "db", _FakeDBHandle())
        result = model_aliases._visible_query("admin", 1)
        assert result == _Expr(op="gt", field="id", value=0)

    def test_non_admin_scopes_to_global_and_own_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-admins see global (NULL org) rows OR their own org's rows -- never another org's."""
        monkeypatch.setattr(model_aliases, "db", _FakeDBHandle())
        result = model_aliases._visible_query("resource_manager", 7)
        assert result == _Expr(
            op="or",
            left=_Expr(op="eq", field="organization_id", value=None),
            right=_Expr(op="eq", field="organization_id", value=7),
        )
        # A different org's id never appears in the built expression.
        assert result.right.value != 8


# ---------------------------------------------------------------------------
# _can_write
# ---------------------------------------------------------------------------


class TestCanWrite:
    """Tests for the write-authorization predicate."""

    def test_admin_can_write_global_and_scoped(self) -> None:
        """Admin may write any alias, global or org-scoped."""
        assert model_aliases._can_write("admin", 1, None) is True
        assert model_aliases._can_write("admin", 1, 99) is True

    def test_resource_manager_can_write_own_org(self) -> None:
        """resource_manager may write an alias scoped to their own org."""
        assert model_aliases._can_write("resource_manager", 5, 5) is True

    def test_resource_manager_cannot_write_global(self) -> None:
        """resource_manager may never write a NULL-org (global) alias."""
        assert model_aliases._can_write("resource_manager", 5, None) is False

    def test_resource_manager_cannot_write_other_org(self) -> None:
        """resource_manager cannot write another org's alias -- tenant isolation."""
        assert model_aliases._can_write("resource_manager", 5, 6) is False

    def test_plain_user_cannot_write(self) -> None:
        """A role with no write privilege at all is always denied."""
        assert model_aliases._can_write("user", 5, 5) is False


# ---------------------------------------------------------------------------
# GET /api/v1/routing/aliases/
# ---------------------------------------------------------------------------


class TestListAliases:
    """Tests for GET /api/v1/routing/aliases/."""

    async def test_list_admin_sees_all(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin listing returns every seeded alias."""
        rows = [_make_alias_row(alias_id=1), _make_alias_row(alias_id=2, organization_id=3)]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get("/api/v1/routing/aliases/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert [e["id"] for e in data["data"]] == [1, 2]
        assert data["meta"]["total"] == 2

    async def test_list_filters_by_source_model_query_param(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """?source_model=<x> exercises the optional filter branch."""
        rows = [_make_alias_row(source_model="gpt-4o")]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get(
            "/api/v1/routing/aliases/?source_model=gpt-4o", headers=auth_headers
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"][0]["source_model"] == "gpt-4o"

    async def test_list_empty(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """No aliases returns an empty list, not an error."""
        resp = await client.get("/api/v1/routing/aliases/", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_list_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/routing/aliases/")
        assert resp.status_code == 401

    async def test_list_response_envelope_shape(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Response matches the {status,data,meta} envelope with the exact field set."""
        app_mock_db.return_value.select.return_value = make_select_result([_make_alias_row()])

        resp = await client.get("/api/v1/routing/aliases/", headers=auth_headers)
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "data", "meta"}
        assert set(data["meta"].keys()) == {"total", "timestamp"}
        assert set(data["data"][0].keys()) == {
            "id",
            "organization_id",
            "source_model",
            "target_model",
            "target_provider",
            "enabled",
            "created_at",
        }


# ---------------------------------------------------------------------------
# GET /api/v1/routing/aliases/<id>
# ---------------------------------------------------------------------------


class TestGetAlias:
    """Tests for GET /api/v1/routing/aliases/<alias_id>."""

    async def test_get_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """An existing alias is returned with its full field set."""
        row = _make_alias_row(alias_id=7, source_model="claude-3", target_model="local-llama")
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.get("/api/v1/routing/aliases/7", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["id"] == 7
        assert data["data"]["source_model"] == "claude-3"

    async def test_get_not_found(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A missing alias returns 404 with an error envelope."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/routing/aliases/999", headers=auth_headers)
        assert resp.status_code == 404
        data = await resp.get_json()
        assert data["status"] == "error"


# ---------------------------------------------------------------------------
# POST /api/v1/routing/aliases/
# ---------------------------------------------------------------------------


class TestCreateAlias:
    """Tests for POST /api/v1/routing/aliases/."""

    async def test_no_body(self, client, auth_headers: dict) -> None:
        """A JSON `null` body (falsy, but valid JSON) hits the `if not data` guard directly."""
        resp = await client.post("/api/v1/routing/aliases/", headers=auth_headers, data="null")
        assert resp.status_code == 400

    async def test_missing_source_model(self, client, auth_headers: dict) -> None:
        """Missing source_model returns 400 naming the field."""
        resp = await client.post(
            "/api/v1/routing/aliases/", headers=auth_headers, json={"target_model": "x"}
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "source_model" in data["error"]

    async def test_missing_target_model(self, client, auth_headers: dict) -> None:
        """Missing target_model returns 400 naming the field."""
        resp = await client.post(
            "/api/v1/routing/aliases/", headers=auth_headers, json={"source_model": "x"}
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "target_model" in data["error"]

    async def test_source_equals_target_rejected(self, client, auth_headers: dict) -> None:
        """Identical source_model/target_model returns 400."""
        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=auth_headers,
            json={"source_model": "gpt-4o", "target_model": "gpt-4o"},
        )
        assert resp.status_code == 400

    async def test_creates_new_row_when_none_exists(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """No existing (org, source) row inserts a new one and returns 201/created."""
        new_row = _make_alias_row(alias_id=42, source_model="gpt-4o", target_model="local-mixtral")
        app_mock_db.return_value.select.return_value.first.side_effect = [None, new_row]
        app_mock_db.model_aliases.insert.return_value = 42

        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=auth_headers,
            json={"source_model": "gpt-4o", "target_model": "local-mixtral"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["meta"]["action"] == "created"
        assert data["data"]["id"] == 42

    async def test_upserts_existing_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An existing (org, source) row is updated in place, returning 200/updated."""
        existing = _make_alias_row(alias_id=5, target_model="old-target")
        updated = _make_alias_row(alias_id=5, target_model="new-target")
        app_mock_db.return_value.select.return_value.first.side_effect = [existing, updated]

        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=auth_headers,
            json={"source_model": "gpt-4o", "target_model": "new-target"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "updated"
        assert data["data"]["target_model"] == "new-target"

    async def test_resource_manager_cannot_create_global_alias(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager may never write a NULL-org (global) alias."""
        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=rm_auth_headers,
            json={"source_model": "a", "target_model": "b"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_cannot_create_for_other_org(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager (org=1) cannot write an alias scoped to a different org."""
        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=rm_auth_headers,
            json={"source_model": "a", "target_model": "b", "organization_id": 2},
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_create_for_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager (org=1) may write an alias scoped to their own org."""
        new_row = _make_alias_row(alias_id=9, organization_id=1)
        app_mock_db.return_value.select.return_value.first.side_effect = [None, new_row]

        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=rm_auth_headers,
            json={"source_model": "a", "target_model": "b", "organization_id": 1},
        )
        assert resp.status_code == 201

    async def test_plain_user_forbidden_by_scope(self, client, user_auth_headers: dict) -> None:
        """A user without model_alias:write scope is rejected before route logic runs."""
        resp = await client.post(
            "/api/v1/routing/aliases/",
            headers=user_auth_headers,
            json={"source_model": "a", "target_model": "b"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/routing/aliases/<id>
# ---------------------------------------------------------------------------


class TestUpdateAlias:
    """Tests for PUT /api/v1/routing/aliases/<alias_id>."""

    async def test_no_body(self, client, auth_headers: dict) -> None:
        """A JSON `null` body (falsy, but valid JSON) hits the `if not data` guard directly."""
        resp = await client.put("/api/v1/routing/aliases/1", headers=auth_headers, data="null")
        assert resp.status_code == 400

    async def test_not_found(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A missing alias returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/routing/aliases/999", headers=auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 404

    async def test_forbidden_cross_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """An org-2 resource_manager cannot update an org-1-scoped alias (tenant isolation)."""
        row = _make_alias_row(alias_id=3, organization_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.put(
            "/api/v1/routing/aliases/3", headers=rm_org2_auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 403

    async def test_no_valid_fields(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A body with no writable fields returns 400."""
        row = _make_alias_row(alias_id=3)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.put(
            "/api/v1/routing/aliases/3", headers=auth_headers, json={"bogus_field": "x"}
        )
        assert resp.status_code == 400

    async def test_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A valid update returns the refreshed row."""
        existing = _make_alias_row(alias_id=3, enabled=True)
        updated = _make_alias_row(alias_id=3, enabled=False)
        app_mock_db.return_value.select.return_value.first.side_effect = [existing, updated]

        resp = await client.put(
            "/api/v1/routing/aliases/3", headers=auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["enabled"] is False


# ---------------------------------------------------------------------------
# DELETE /api/v1/routing/aliases/<id>
# ---------------------------------------------------------------------------


class TestDeleteAlias:
    """Tests for DELETE /api/v1/routing/aliases/<alias_id>."""

    async def test_not_found(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A missing alias returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/routing/aliases/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_forbidden_cross_org(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """An org-2 resource_manager cannot delete an org-1-scoped alias (tenant isolation)."""
        row = _make_alias_row(alias_id=4, organization_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.delete("/api/v1/routing/aliases/4", headers=rm_org2_auth_headers)
        assert resp.status_code == 403

    async def test_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """A valid delete returns the deleted id and action=deleted."""
        row = _make_alias_row(alias_id=4)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.delete("/api/v1/routing/aliases/4", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["meta"]["action"] == "deleted"
        assert data["data"]["id"] == 4
