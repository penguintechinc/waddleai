"""Adapts `TenantGraphClient` to the `KnowledgeService` graph methods for MCP (spec Section 4a).

Resolves the caller's repo *name* to an org-scoped repo_id, builds a
`TenantScope` from the caller's org (never from client input -- see
`shared/mcp/tools.py::WaddleAITools`, whose graph methods are subject-free
and always pass `ctx.org_id`), and serializes `GraphPath` results into the
plain-dict shape MCP tool results carry.

MCP tool semantics differ from the REST surface (`services/management/app/
api/v1/graph.py`) only in *how* a denial is communicated, never in *what*
gates access: a REST caller sees an explicit 503/404/403, but an MCP tool
result silently degrades to an empty list on the `waddleai.graph` flag being
off, the Enterprise `waddleai_graph` license entitlement being absent, an
unresolvable repo name (IDOR-safe: an unknown-or-other-org repo name
degrades exactly like an empty index, never distinguishing the two), a
`GraphUnavailableError` from the graph backend, or *any other* unexpected
failure -- never a hang, never a raised exception reaching the MCP
transport. The two-layer gate (flag + entitlement) is mirrored here exactly
as REST enforces it (same `waddleai_graph` feature key, same
`penguin_licensing` product/client construction) -- a Professional-tier org
must not get the Enterprise graph feature just because it reaches this
adapter instead of REST.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal, Protocol, cast, runtime_checkable

from shared.graph.client import TenantGraphClient
from shared.graph.types import MAX_GRAPH_DEPTH, GraphPath, GraphUnavailableError, TenantScope
from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.graph"
_LICENSE_FEATURE = "waddleai_graph"
_DEFAULT_BRANCH = "main"

_license_client: Any = None


def _get_license_client() -> Any:
    """Lazily construct the shared ``penguin_licensing.LicenseClient``.

    Mirrors ``services/management/app/api/v1/graph.py``'s
    ``_get_license_client`` exactly -- same feature key, same
    ``product="waddleai"`` (the SDK's own default is ``"elder"`` and would
    silently check entitlements for the wrong product).
    """
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


async def _entitled() -> bool:
    """Enterprise ``waddleai_graph`` entitlement check -- fail-closed on any I/O error.

    Mirrors REST's ``_entitled()`` in ``services/management/app/api/v1/
    graph.py``. MCP's own degrade-to-empty contract applies on top: an
    unentitled org (or a license-server I/O failure, treated the same as
    unentitled) gets ``[]``, never an exception.
    """

    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(_LICENSE_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("mcp graph: entitlement check failed: %s", exc)
            return False

    return await asyncio.to_thread(_check)


# Mirrors `shared.graph.client`'s private `_Direction` alias -- kept in sync
# here (rather than imported) since that name is module-private. A `str`
# parameter for `direction` (looser than the concrete client's
# `Literal["in", "out", "both"]`) would be structurally unsound under
# mypy --strict against `TenantGraphClient`'s own signature, so this
# adapter's public methods narrow to the same `Literal` and accept whatever
# `WaddleAITools` passes as a plain `str` at the boundary where it's used.
_Direction = Literal["in", "out", "both"]
_VALID_DIRECTIONS = frozenset({"in", "out", "both"})


def _coerce_direction(direction: str) -> _Direction:
    """Narrow an MCP-tool-supplied `direction` string to the client's `Literal`.

    An invalid value (never expected from `WaddleAITools`' own default, but
    a caller could still pass a bad string through the tool call) falls
    back to `"out"` rather than raising -- MCP tool semantics degrade
    gracefully rather than erroring on a cosmetic input mistake. `cast` is
    safe here only because membership in `_VALID_DIRECTIONS` was just
    checked -- mirrors `services/management/app/api/v1/graph.py`'s
    validate-then-cast pattern for the same `Literal`.
    """
    if direction in _VALID_DIRECTIONS:
        return cast(_Direction, direction)
    return "out"


_MIN_GRAPH_DEPTH = 1


def _clamp_depth(depth: int) -> int:
    """Bound an MCP-tool-supplied `depth` to `[1, MAX_GRAPH_DEPTH]`.

    `TenantGraphClient.call_graph` forwards `depth` straight into the
    driver's `[:REL*1..{depth}]` variable-length Cypher pattern -- an
    unbounded value there is an expensive-traversal / availability risk,
    sharper still in Phase-1 dev-mode where every org resolves to one
    shared Neo4j instance (one org's oversized query can degrade every
    other tenant). Mirrors the REST route's `_MAX_DEPTH` bound
    (`services/management/app/api/v1/graph.py`), sourced from the same
    `shared.graph.types.MAX_GRAPH_DEPTH` constant so the two surfaces
    can't drift apart. Unlike the REST route, this silently clamps rather
    than erroring -- MCP tool semantics degrade gracefully on a cosmetic
    input mistake rather than failing the call.
    """
    return max(_MIN_GRAPH_DEPTH, min(depth, MAX_GRAPH_DEPTH))


@runtime_checkable
class _SqlDB(Protocol):
    """Structural db handle: raw-SQL execution only -- mirrors `shared.graph.resolver._SqlDB`.

    Redeclared locally (rather than imported) since that name is
    module-private there; typing `db: object` and calling
    `self._db.executesql(...)` directly would fail mypy --strict's
    `attr-defined` check the same way `shared.knowledge.coderag_backend`
    would without this same fix.
    """

    def executesql(self, sql: str, placeholders: list[Any] | None = ...) -> list[Any]:
        """Run parameterized raw SQL and return the result rows."""
        ...


def _serialize(paths: list[GraphPath]) -> list[dict[str, Any]]:
    """Explicit response shape for a list of traversal paths -- never a raw `GraphPath`."""
    return [{"nodes": list(p.node_keys), "edges": list(p.edge_types)} for p in paths]


class _GraphClientProtocol(Protocol):
    """The subset of `TenantGraphClient` this adapter calls -- injectable for tests.

    Mirrors `services/management/app/api/v1/graph.py`'s `_GraphClientProtocol`:
    typing the injected `client` param as the concrete `TenantGraphClient`
    class would reject any structurally-compatible test fake under
    mypy --strict (a fake need not literally subclass the real client).
    """

    async def call_graph(
        self, scope: TenantScope, symbol: str, *, direction: _Direction = "out", depth: int = 3
    ) -> list[GraphPath]:
        """Callers of / callees from `symbol`, scoped to `scope`."""
        ...

    async def class_hierarchy(
        self, scope: TenantScope, symbol: str, *, direction: _Direction = "out"
    ) -> list[GraphPath]:
        """Inheritance chain for `symbol`, scoped to `scope`."""
        ...


class GraphKnowledgeService:
    """`KnowledgeService` graph methods backed by Task 8's `TenantGraphClient`.

    Every method is org-scoped from its `org_id` keyword-only argument,
    which `WaddleAITools` always sources from `ctx.org_id` -- this class
    never accepts or trusts any other source of tenant identity.
    """

    def __init__(self, db: object, client: _GraphClientProtocol | None = None) -> None:
        """Bind to a penguin-dal-style db handle; `client` is injectable for tests."""
        self._db = db
        self._client: _GraphClientProtocol = client or TenantGraphClient(db)

    async def _repo_id(self, org_id: int, repo: str) -> int | None:
        """Resolve a repo *name* to its id, filtered on `org_id` (IDOR-safe).

        A repo name belonging to a different org, or that doesn't exist at
        all, both resolve to `None` -- callers map either case to an empty
        result, so no response ever confirms or denies another org's repo
        names.
        """

        def _fetch() -> Any:
            rows = cast(_SqlDB, self._db).executesql(
                "SELECT id FROM code_repos WHERE org_id = %s AND name = %s LIMIT 1",  # nosec B608 -- fixed literal, values bound via executesql params  # noqa: S608, E501
                [org_id, repo],
            )
            return int(rows[0][0]) if rows else None

        return await asyncio.to_thread(_fetch)

    async def get_call_graph(
        self,
        *,
        org_id: int,
        repo: str,
        branch: str | None,
        symbol: str,
        direction: str,
        depth: int,
    ) -> list[dict[str, Any]]:
        """Call-graph traversal (CALLS) from `symbol`, scoped to `org_id`.

        Degrades to `[]` -- never a raise, never a hang -- when the
        `waddleai.graph` flag is off for this org, the org lacks the
        Enterprise `waddleai_graph` entitlement, `repo` doesn't resolve
        within `org_id`, the graph backend raises `GraphUnavailableError`,
        or any other unexpected failure occurs (matches REST/the worker's
        broad catch -- see module docstring). `depth` is silently clamped
        to `[1, MAX_GRAPH_DEPTH]` -- see `_clamp_depth`.
        """
        if not is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False):
            return []
        if not await _entitled():
            return []
        repo_id = await self._repo_id(org_id, repo)
        if repo_id is None:
            return []
        scope = TenantScope(
            org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch or _DEFAULT_BRANCH
        )
        try:
            paths = await self._client.call_graph(
                scope,
                symbol,
                direction=_coerce_direction(direction),
                depth=_clamp_depth(depth),
            )
        except GraphUnavailableError:
            return []
        except Exception as exc:
            logger.warning(
                "mcp graph: call-graph failed org=%s repo=%s symbol=%s: %s",
                org_id,
                repo,
                symbol,
                exc,
            )
            return []
        return _serialize(paths)

    async def get_class_hierarchy(
        self, *, org_id: int, repo: str, branch: str | None, symbol: str, direction: str
    ) -> list[dict[str, Any]]:
        """Class-hierarchy traversal (EXTENDS/IMPLEMENTS) from `symbol`, scoped to `org_id`.

        Same degrade-to-empty contract as `get_call_graph` -- see its
        docstring.
        """
        if not is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False):
            return []
        if not await _entitled():
            return []
        repo_id = await self._repo_id(org_id, repo)
        if repo_id is None:
            return []
        scope = TenantScope(
            org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch or _DEFAULT_BRANCH
        )
        try:
            paths = await self._client.class_hierarchy(
                scope, symbol, direction=_coerce_direction(direction)
            )
        except GraphUnavailableError:
            return []
        except Exception as exc:
            logger.warning(
                "mcp graph: class-hierarchy failed org=%s repo=%s symbol=%s: %s",
                org_id,
                repo,
                symbol,
                exc,
            )
            return []
        return _serialize(paths)


__all__ = ["GraphKnowledgeService"]
