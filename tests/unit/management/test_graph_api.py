"""Tests for /api/v1/graph/call-graph and /class-hierarchy: gate, IDOR, DTO, error mapping.

Security-sensitive surface (§4a): org comes from the validated JWT only,
never a query param; the ``repo`` query param (a repo *name*) is resolved to
a repo_id filtered on the caller's org before any graph call, so an unknown
or other-org repo name 404s identically to a nonexistent one; the two-layer
flag+license gate (404/403) runs before any DB or graph I/O; a
``GraphUnavailableError`` -- or any other graph-layer failure -- maps to a
clean 503, never a raw 500.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from services.management.app.api.v1 import graph as graph_routes
from shared.graph.types import GraphPath, GraphUnavailableError, TenantScope
from tests.unit.management.conftest import make_dal_row, make_select_result

# ---------------------------------------------------------------------------
# Gate + fixture helpers (mirrors tests/unit/management/test_model_access_policies_routes.py)
# ---------------------------------------------------------------------------


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn `waddleai.graph` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")


def _entitled(monkeypatch: pytest.MonkeyPatch, entitled: bool = True) -> None:
    """Patch the license-entitlement check for one test."""
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "services.management.app.api.v1.graph._get_license_client",
        lambda: mock_client,
    )


def _gate_open(monkeypatch: pytest.MonkeyPatch, entitled: bool = True) -> None:
    """Flag on + entitled -- the surface is fully usable."""
    _enable_flag(monkeypatch)
    _entitled(monkeypatch, entitled)


def _repo_row(**overrides: object) -> MagicMock:
    """A `code_repos` row for org 7 named "widgets", by default."""
    fields: dict[str, object] = {"id": 42, "org_id": 7, "name": "widgets"}
    fields.update(overrides)
    return make_dal_row(**fields)


class FakeGraphClient:
    """A fake `TenantGraphClient` -- no live Neo4j, no db-backed resolver."""

    def __init__(
        self,
        paths: list[GraphPath] | None = None,
        raise_unavailable: bool = False,
        raise_other: bool = False,
    ) -> None:
        """Configure the fake's canned response or which error it raises."""
        self._paths = paths or []
        self._raise_unavailable = raise_unavailable
        self._raise_other = raise_other

    async def call_graph(
        self, scope: TenantScope, symbol: str, *, direction: str = "out", depth: int = 3
    ) -> list[GraphPath]:
        """Return the canned paths, or raise per this fake's configuration."""
        if self._raise_unavailable:
            raise GraphUnavailableError("graph instance not ready")
        if self._raise_other:
            raise RuntimeError("boom")
        return self._paths

    async def class_hierarchy(
        self, scope: TenantScope, symbol: str, *, direction: str = "out"
    ) -> list[GraphPath]:
        """Return the canned paths, or raise per this fake's configuration."""
        if self._raise_unavailable:
            raise GraphUnavailableError("graph instance not ready")
        if self._raise_other:
            raise RuntimeError("boom")
        return self._paths


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: FakeGraphClient) -> None:
    """Make the route module's `_get_graph_client()` factory return `client`."""
    monkeypatch.setattr(graph_routes, "_get_graph_client", lambda: client)


# ---------------------------------------------------------------------------
# Helper unit tests (drive graph.py's helpers directly -- no HTTP/Quart app)
# ---------------------------------------------------------------------------


def test_serialize_paths_shape() -> None:
    """`_serialize_paths` turns a `GraphPath` into the `{nodes, edges}` DTO shape."""
    paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
    out = graph_routes._serialize_paths(paths)
    assert out == [{"nodes": ["7:42:main:a", "7:42:main:b"], "edges": ["CALLS"]}]


def test_serialize_paths_empty() -> None:
    """`_serialize_paths` on an empty path list returns an empty list."""
    assert graph_routes._serialize_paths([]) == []


@pytest.mark.parametrize(
    ("repo_name", "symbol", "direction", "expected"),
    [
        ("", "a", "out", "repo is required"),
        ("widgets", "", "out", "symbol is required"),
        ("widgets", "a", "sideways", None),  # checked below via substring
    ],
)
def test_validate_common_params(
    repo_name: str, symbol: str, direction: str, expected: str | None
) -> None:
    """Missing repo/symbol and an invalid direction each produce a distinct error message."""
    error = graph_routes._validate_common_params(repo_name, symbol, direction)
    if expected is not None:
        assert error == expected
    else:
        assert error is not None and "direction must be one of" in error


def test_validate_common_params_accepts_all_valid_directions() -> None:
    """Every direction in {in, out, both} passes validation (returns no error)."""
    for direction in ("in", "out", "both"):
        assert graph_routes._validate_common_params("widgets", "a", direction) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 3),  # default
        ("1", 1),
        ("10", 10),
        ("5", 5),
    ],
)
def test_parse_depth_valid(raw: str | None, expected: int) -> None:
    """A missing depth defaults to 3; any integer in [1, 10] parses through unchanged."""
    assert graph_routes._parse_depth(raw) == expected


@pytest.mark.parametrize("raw", ["0", "11", "-1", "abc", ""])
def test_parse_depth_invalid(raw: str) -> None:
    """Zero, negative, out-of-range, non-numeric, and empty depth values all reject."""
    assert graph_routes._parse_depth(raw) is None


async def test_run_call_graph_maps_unavailable_to_503() -> None:
    """A `GraphUnavailableError` from the client maps to a clean 503."""
    body, status = await graph_routes._run_call_graph(
        FakeGraphClient(raise_unavailable=True),
        org_id=7,
        repo_id=42,
        branch="main",
        symbol="a",
        direction="out",
        depth=3,
    )
    assert status == 503
    assert body["status"] == "error"
    assert "unavailable" in body["error"]


async def test_run_call_graph_maps_unexpected_error_to_503_not_raw_500() -> None:
    """A non-GraphUnavailableError failure (e.g. a bad org id) still maps to 503, never a raise."""
    body, status = await graph_routes._run_call_graph(
        FakeGraphClient(raise_other=True),
        org_id=7,
        repo_id=42,
        branch="main",
        symbol="a",
        direction="out",
        depth=3,
    )
    assert status == 503
    assert body == {"status": "error", "error": "graph temporarily unavailable"}


async def test_run_call_graph_happy_path() -> None:
    """A successful traversal returns the `{status, data, meta}` envelope with serialized paths."""
    paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
    body, status = await graph_routes._run_call_graph(
        FakeGraphClient(paths=paths),
        org_id=7,
        repo_id=42,
        branch="main",
        symbol="a",
        direction="out",
        depth=3,
    )
    assert status == 200
    assert body["status"] == "success"
    assert body["data"]["paths"][0]["edges"] == ["CALLS"]
    assert "timestamp" in body["meta"]


async def test_run_class_hierarchy_maps_unavailable_to_503() -> None:
    """A `GraphUnavailableError` from the client maps to a clean 503."""
    body, status = await graph_routes._run_class_hierarchy(
        FakeGraphClient(raise_unavailable=True),
        org_id=7,
        repo_id=42,
        branch="main",
        symbol="Base",
        direction="out",
    )
    assert status == 503


async def test_run_class_hierarchy_happy_path() -> None:
    """A successful traversal returns the `{status, data, meta}` envelope with serialized paths."""
    paths = [GraphPath(("7:42:main:Base", "7:42:main:Child"), ("EXTENDS",))]
    body, status = await graph_routes._run_class_hierarchy(
        FakeGraphClient(paths=paths),
        org_id=7,
        repo_id=42,
        branch="main",
        symbol="Base",
        direction="out",
    )
    assert status == 200
    assert body["data"]["paths"][0]["edges"] == ["EXTENDS"]


class _Pred:
    """Records an equality/AND predicate the way `_resolve_repo_id` builds it."""

    def __init__(self, conds: dict[str, Any]) -> None:
        """Start from a single-field equality condition."""
        self.conds = conds

    def __and__(self, other: _Pred) -> _Pred:
        """Merge two predicates' conditions, matching PyDAL's `&` query combinator."""
        merged = dict(self.conds)
        merged.update(other.conds)
        return _Pred(merged)


class _Field:
    """A `db.table.field`-style attribute stand-in that records `== value` comparisons."""

    def __init__(self, name: str) -> None:
        """Bind this field stand-in to its column name."""
        self._name = name

    def __eq__(self, other: object) -> _Pred:  # type: ignore[override]
        """Record an equality predicate rather than compare identity/value."""
        return _Pred({self._name: other})


class _Table:
    """A `db.table`-style stand-in whose attributes are `_Field`s."""

    def __getattr__(self, name: str) -> _Field:
        """Return a `_Field` recorder for any column name accessed."""
        return _Field(name)


class _RecordingDB:
    """A minimal fake db that actually filters rows by the built predicate.

    Unlike the shared Quart-harness mock (dummy `_DBQuery` objects that
    accept any comparison), this evaluates the real `(org_id, name)`
    predicate `_resolve_repo_id` builds against a fixed row set -- proving
    the IDOR-safety property at the query-construction level, not just via
    an empty-vs-nonempty mock result.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Seed the fake `code_repos` table with `rows`."""
        self.code_repos = _Table()
        self._rows = rows
        self.last_conds: dict[str, Any] | None = None

    def __call__(self, pred: _Pred) -> _RecordingDB:
        """Record the predicate `_resolve_repo_id` built, mimicking `db(query)`."""
        self.last_conds = pred.conds
        return self

    def select(self) -> MagicMock:
        """Return only the rows matching every recorded condition, mimicking `.select()`."""
        matches = [
            make_dal_row(**row)
            for row in self._rows
            if all(row.get(k) == v for k, v in (self.last_conds or {}).items())
        ]
        return make_select_result(matches)


async def test_resolve_repo_id_scopes_to_callers_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo named identically in two orgs resolves only to the caller's own org's id."""
    rows = [
        {"id": 42, "org_id": 7, "name": "widgets"},
        {"id": 99, "org_id": 8, "name": "widgets"},  # same name, different org
    ]
    fake_db = _RecordingDB(rows)
    monkeypatch.setattr(graph_routes, "db", fake_db)

    repo_id = await graph_routes._resolve_repo_id(7, "widgets")

    assert repo_id == 42
    assert fake_db.last_conds == {"org_id": 7, "name": "widgets"}


async def test_resolve_repo_id_other_org_only_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo that exists only in another org resolves to None (never leaks the other org's id)."""
    rows = [{"id": 99, "org_id": 8, "name": "widgets"}]
    fake_db = _RecordingDB(rows)
    monkeypatch.setattr(graph_routes, "db", fake_db)

    repo_id = await graph_routes._resolve_repo_id(7, "widgets")

    assert repo_id is None


def test_get_license_client_lazily_constructs_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_get_license_client` builds a `product="waddleai"` client once, then reuses it."""
    monkeypatch.setattr(graph_routes, "_license_client", None)
    monkeypatch.setenv("LICENSE_KEY", "test-only-key")  # noqa: S105 -- test fixture, not real

    first = graph_routes._get_license_client()
    second = graph_routes._get_license_client()

    assert first is second
    assert first.product == "waddleai"


def test_get_graph_client_builds_a_tenant_graph_client() -> None:
    """`_get_graph_client` returns a real `TenantGraphClient` bound to this module's `db`."""
    from shared.graph.client import TenantGraphClient

    client = graph_routes._get_graph_client()

    assert isinstance(client, TenantGraphClient)


# ---------------------------------------------------------------------------
# Route-level tests (real HTTP through the Quart test client)
# ---------------------------------------------------------------------------


class TestCallGraphRoute:
    """GET /api/v1/graph/call-graph."""

    async def test_requires_auth(self, client) -> None:
        """No Authorization header at all -> 401."""
        resp = await client.get("/api/v1/graph/call-graph?repo=widgets&symbol=a")
        assert resp.status_code == 401

    async def test_flag_off_returns_404(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag-off path never touches the DB -- 404, not 200/500."""
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")
        resp = await client.get(
            "/api/v1/graph/call-graph?repo=widgets&symbol=a", headers=auth_headers
        )
        assert resp.status_code == 404
        app_mock_db.return_value.select.assert_not_called()

    async def test_flag_on_not_entitled_returns_403(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag on but no Enterprise entitlement -> 403."""
        _gate_open(monkeypatch, entitled=False)
        resp = await client.get(
            "/api/v1/graph/call-graph?repo=widgets&symbol=a", headers=auth_headers
        )
        assert resp.status_code == 403

    async def test_missing_repo_returns_400(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing `repo` query param -> 400, never a downstream KeyError."""
        _gate_open(monkeypatch)
        resp = await client.get("/api/v1/graph/call-graph?symbol=a", headers=auth_headers)
        assert resp.status_code == 400

    async def test_missing_symbol_returns_400(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing `symbol` query param -> 400."""
        _gate_open(monkeypatch)
        resp = await client.get("/api/v1/graph/call-graph?repo=widgets", headers=auth_headers)
        assert resp.status_code == 400

    async def test_invalid_direction_returns_400(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `direction` outside {in, out, both} -> 400."""
        _gate_open(monkeypatch)
        resp = await client.get(
            "/api/v1/graph/call-graph?repo=widgets&symbol=a&direction=sideways",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("depth", ["0", "11", "abc"])
    async def test_invalid_depth_returns_400(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch, depth: str
    ) -> None:
        """Zero, above-max, and non-numeric `depth` values all -> 400."""
        _gate_open(monkeypatch)
        resp = await client.get(
            f"/api/v1/graph/call-graph?repo=widgets&symbol=a&depth={depth}",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_unknown_or_other_org_repo_returns_404(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo name not owned by the caller's org -- 404, identical to nonexistent."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/graph/call-graph?repo=someone-elses-repo&symbol=a", headers=auth_headers
        )

        assert resp.status_code == 404

    async def test_graph_unavailable_returns_503(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `GraphUnavailableError` from the graph client -> 503, not a raw 500."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])
        _patch_client(monkeypatch, FakeGraphClient(raise_unavailable=True))

        resp = await client.get(
            "/api/v1/graph/call-graph?repo=widgets&symbol=a", headers=auth_headers
        )

        assert resp.status_code == 503
        body = await resp.get_json()
        assert body["status"] == "error"

    async def test_happy_path_returns_scoped_dto(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful call-graph request returns the serialized `{status, data, meta}` DTO."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])
        paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
        _patch_client(monkeypatch, FakeGraphClient(paths=paths))

        resp = await client.get(
            "/api/v1/graph/call-graph?repo=widgets&symbol=a&direction=out&depth=2",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        expected = [{"nodes": ["7:42:main:a", "7:42:main:b"], "edges": ["CALLS"]}]
        assert body["data"]["paths"] == expected


class TestClassHierarchyRoute:
    """GET /api/v1/graph/class-hierarchy."""

    async def test_requires_auth(self, client) -> None:
        """No Authorization header at all -> 401."""
        resp = await client.get("/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base")
        assert resp.status_code == 401

    async def test_flag_off_returns_404(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag-off path -> 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")
        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_flag_on_not_entitled_returns_403(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag on but no Enterprise entitlement -> 403."""
        _gate_open(monkeypatch, entitled=False)
        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base", headers=auth_headers
        )
        assert resp.status_code == 403

    async def test_invalid_direction_returns_400(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `direction` outside {in, out, both} -> 400."""
        _gate_open(monkeypatch)
        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base&direction=nope",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_unknown_or_other_org_repo_returns_404(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo name not owned by the caller's org -- 404, identical to nonexistent."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=someone-elses-repo&symbol=Base",
            headers=auth_headers,
        )

        assert resp.status_code == 404

    async def test_graph_unavailable_returns_503(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `GraphUnavailableError` from the graph client -> 503, not a raw 500."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])
        _patch_client(monkeypatch, FakeGraphClient(raise_unavailable=True))

        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base", headers=auth_headers
        )

        assert resp.status_code == 503

    async def test_happy_path_returns_scoped_dto(
        self, client, auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful class-hierarchy request returns the serialized `{status, data}` DTO."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])
        paths = [GraphPath(("7:42:main:Base", "7:42:main:Child"), ("EXTENDS",))]
        _patch_client(monkeypatch, FakeGraphClient(paths=paths))

        resp = await client.get(
            "/api/v1/graph/class-hierarchy?repo=widgets&symbol=Base&direction=in",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["paths"] == [
            {"nodes": ["7:42:main:Base", "7:42:main:Child"], "edges": ["EXTENDS"]}
        ]
