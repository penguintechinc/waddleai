"""§4a CodeRAG graph queries: ``/api/v1/graph/call-graph`` and ``/class-hierarchy``.

Read-only, org-scoped surfaces over Task 8's ``TenantGraphClient``: call-graph
(CALLS) and class-hierarchy (EXTENDS/IMPLEMENTS) traversals from a symbol.
``@require_auth`` only (no write scope) -- org comes from the validated JWT
(``g.user["organization_id"]``), never from a query param, mirroring
``code_repos.py``'s ``list_code_repos``. The ``repo`` query param is a repo
*name*; it is resolved to a repo_id filtered on the caller's ``org_id`` before
any graph call is made, so a repo name that exists in a different org 404s
identically to a nonexistent one (IDOR-safe, same pattern as ``code_repos.py``).

Two-layer gate, mirroring ``model_access_policies.py``: the ``waddleai.graph``
PostHog flag (404 if off, fail-safe OFF) and the Enterprise ``waddleai_graph``
license entitlement (403 if unentitled). A ``GraphUnavailableError`` -- or any
other failure surfacing from graph resolution/traversal (e.g. a malformed org
id reaching the resolver) -- maps to a clean 503, never a raw 500/stack trace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from quart import g, jsonify, request

from shared.graph.client import TenantGraphClient
from shared.graph.types import MAX_GRAPH_DEPTH, GraphPath, GraphUnavailableError, TenantScope
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.graph"
_LICENSE_FEATURE = "waddleai_graph"

# Mirrors shared.graph.client's private `_Direction` alias -- kept in sync
# here (rather than imported) since that name is module-private; must match
# exactly, or `_GraphClientProtocol` below becomes structurally incompatible
# with the real `TenantGraphClient` under mypy --strict (a Protocol method
# accepting a wider `str` than the concrete class's `Literal[...]` param is
# an unsound override).
_Direction = Literal["in", "out", "both"]

_VALID_DIRECTIONS = frozenset({"in", "out", "both"})
_DEFAULT_DEPTH = 3
# Shared with the MCP graph adapter (shared/graph/types.py) so the two
# surfaces can't drift apart on the traversal-depth cost bound.
_MAX_DEPTH = MAX_GRAPH_DEPTH

_license_client: Any = None


class _GraphClientProtocol(Protocol):
    """The subset of ``TenantGraphClient`` these routes call -- injectable for tests."""

    async def call_graph(
        self, scope: TenantScope, symbol: str, *, direction: _Direction = "out", depth: int = 3
    ) -> list[GraphPath]:
        """Callers of / callees from ``symbol``, scoped to ``scope``."""
        ...

    async def class_hierarchy(
        self, scope: TenantScope, symbol: str, *, direction: _Direction = "out"
    ) -> list[GraphPath]:
        """Inheritance chain for ``symbol``, scoped to ``scope``."""
        ...


def _get_license_client() -> Any:
    """Lazily construct the shared ``penguin_licensing.LicenseClient``.

    ``product`` must be ``"waddleai"`` -- the SDK's own default is ``"elder"``
    and would silently check entitlements for the wrong product.
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


def _flag_enabled(org_id: int | None) -> bool:
    """Fail-safe-OFF evaluation of the ``waddleai.graph`` flag for this org."""
    return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)


async def _entitled() -> bool:
    """Two-layer gate's license-entitlement half -- fail-closed on any I/O error."""

    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(_LICENSE_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("graph: entitlement check failed: %s", exc)
            return False

    return await asyncio.to_thread(_check)


async def _gate(org_id: int | None) -> tuple[Any, int] | None:
    """Return a ``(jsonify, status)`` tuple if the caller may not use this surface, else None."""
    if not _flag_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    if not await _entitled():
        return (
            jsonify(
                {
                    "status": "error",
                    "error": (
                        "Graph queries require an Enterprise license entitlement (waddleai_graph)"
                    ),
                }
            ),
            403,
        )
    return None


def _serialize_paths(paths: list[GraphPath]) -> list[dict[str, list[str]]]:
    """Explicit response DTO for a list of traversal paths -- never a raw ``GraphPath``."""
    return [{"nodes": list(p.node_keys), "edges": list(p.edge_types)} for p in paths]


async def _resolve_repo_id(org_id: int, repo_name: str) -> int | None:
    """Resolve a repo *name* to its id, filtered on the caller's org (IDOR-safe).

    A repo name that belongs to a different org, or doesn't exist at all,
    both resolve to ``None`` -- the caller maps that to 404 either way, so no
    response ever confirms or denies another org's repo names.
    """

    def _fetch() -> Any:
        query = (db.code_repos.org_id == org_id) & (db.code_repos.name == repo_name)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    return int(row.id) if row is not None else None


def _validate_common_params(repo_name: str, symbol: str, direction: str) -> str | None:
    """Validate the params shared by both routes; returns an error message or None."""
    if not repo_name:
        return "repo is required"
    if not symbol:
        return "symbol is required"
    if direction not in _VALID_DIRECTIONS:
        return f"direction must be one of {sorted(_VALID_DIRECTIONS)}"
    return None


def _parse_depth(raw: str | None) -> int | None:
    """Parse and bound the ``depth`` query param; ``None`` on missing/invalid/out-of-range."""
    if raw is None:
        return _DEFAULT_DEPTH
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return None
    if depth < 1 or depth > _MAX_DEPTH:
        return None
    return depth


def _get_graph_client() -> _GraphClientProtocol:
    """Construct the ``TenantGraphClient`` bound to this module's db handle.

    A thin factory (rather than instantiating inline in each route) so tests
    can monkeypatch construction without patching the class itself.
    """
    return TenantGraphClient(db)


async def _run_call_graph(
    client: _GraphClientProtocol,
    *,
    org_id: int,
    repo_id: int,
    branch: str,
    symbol: str,
    direction: _Direction,
    depth: int,
) -> tuple[dict[str, Any], int]:
    """Execute a scoped call-graph traversal; maps any graph-layer failure to 503.

    Both ``GraphUnavailableError`` (the org's instance isn't ready/reachable)
    and any other exception surfacing from scope construction or the client
    call (e.g. a malformed org id reaching the resolver) are caught here and
    turned into the same clean 503 -- never a raw 500/stack trace to the
    caller (carry-forward from Task 8's review).
    """
    try:
        scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch)
        paths = await client.call_graph(scope, symbol, direction=direction, depth=depth)
    except GraphUnavailableError as exc:
        logger.warning("graph: call-graph unavailable org=%s repo=%s: %s", org_id, repo_id, exc)
        return {"status": "error", "error": "graph temporarily unavailable"}, 503
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        logger.error("graph: call-graph failed org=%s repo=%s: %s", org_id, repo_id, exc)
        return {"status": "error", "error": "graph temporarily unavailable"}, 503
    return (
        {
            "status": "success",
            "data": {"paths": _serialize_paths(paths)},
            "meta": {"timestamp": datetime.now(UTC).isoformat()},
        },
        200,
    )


async def _run_class_hierarchy(
    client: _GraphClientProtocol,
    *,
    org_id: int,
    repo_id: int,
    branch: str,
    symbol: str,
    direction: _Direction,
) -> tuple[dict[str, Any], int]:
    """Execute a scoped class-hierarchy traversal; maps any graph-layer failure to 503.

    See ``_run_call_graph`` for why both ``GraphUnavailableError`` and any
    other exception from this boundary are collapsed to the same 503.
    """
    try:
        scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch)
        paths = await client.class_hierarchy(scope, symbol, direction=direction)
    except GraphUnavailableError as exc:
        logger.warning(
            "graph: class-hierarchy unavailable org=%s repo=%s: %s", org_id, repo_id, exc
        )
        return {"status": "error", "error": "graph temporarily unavailable"}, 503
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        logger.error("graph: class-hierarchy failed org=%s repo=%s: %s", org_id, repo_id, exc)
        return {"status": "error", "error": "graph temporarily unavailable"}, 503
    return (
        {
            "status": "success",
            "data": {"paths": _serialize_paths(paths)},
            "meta": {"timestamp": datetime.now(UTC).isoformat()},
        },
        200,
    )


@api_v1_bp.route("/graph/call-graph", methods=["GET"])
@require_auth
async def call_graph_route() -> tuple[Any, int]:
    """Call-graph traversal from a symbol, org-scoped from the validated JWT."""
    org_id = g.user.get("organization_id")
    gate = await _gate(org_id)
    if gate is not None:
        return gate

    repo_name = request.args.get("repo", "")
    symbol = request.args.get("symbol", "")
    branch = request.args.get("branch", "main")
    direction = request.args.get("direction", "out")

    error = _validate_common_params(repo_name, symbol, direction)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    depth = _parse_depth(request.args.get("depth"))
    if depth is None:
        return (
            jsonify({"status": "error", "error": f"depth must be an integer in [1, {_MAX_DEPTH}]"}),
            400,
        )

    repo_id = await _resolve_repo_id(org_id, repo_name)
    if repo_id is None:
        return jsonify({"status": "error", "error": "not found"}), 404

    # Safe: `_validate_common_params` already confirmed membership in
    # `_VALID_DIRECTIONS` above; `cast` only narrows the static type.
    validated_direction = cast(_Direction, direction)
    client = _get_graph_client()
    body, status = await _run_call_graph(
        client,
        org_id=org_id,
        repo_id=repo_id,
        branch=branch,
        symbol=symbol,
        direction=validated_direction,
        depth=depth,
    )
    return jsonify(body), status


@api_v1_bp.route("/graph/class-hierarchy", methods=["GET"])
@require_auth
async def class_hierarchy_route() -> tuple[Any, int]:
    """Class-hierarchy (EXTENDS/IMPLEMENTS) traversal from a symbol, org-scoped."""
    org_id = g.user.get("organization_id")
    gate = await _gate(org_id)
    if gate is not None:
        return gate

    repo_name = request.args.get("repo", "")
    symbol = request.args.get("symbol", "")
    branch = request.args.get("branch", "main")
    direction = request.args.get("direction", "out")

    error = _validate_common_params(repo_name, symbol, direction)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    repo_id = await _resolve_repo_id(org_id, repo_name)
    if repo_id is None:
        return jsonify({"status": "error", "error": "not found"}), 404

    # Safe: `_validate_common_params` already confirmed membership in
    # `_VALID_DIRECTIONS` above; `cast` only narrows the static type.
    validated_direction = cast(_Direction, direction)
    client = _get_graph_client()
    body, status = await _run_class_hierarchy(
        client,
        org_id=org_id,
        repo_id=repo_id,
        branch=branch,
        symbol=symbol,
        direction=validated_direction,
    )
    return jsonify(body), status
