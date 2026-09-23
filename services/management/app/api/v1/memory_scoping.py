"""§9.4 conversation-memory config + §9.7 memory promote/correct/dispute API.

The correction/promotion surface the MCP tools (``memory_promote``,
``memory_correct``, ``memory_dispute``, built in mcp-v2) call against, plus
the §9.4 seeded conversation-memory defaults (0.7 relevance cutoff, top-3
injection).

TODO(rebase): the legacy conversation-memory config CRUD lives at
``/ailb/memory-config`` (``ailb_memory.py``, backed by
``conversation_memory_configs``). ``feature/aiproxy-migration`` re-homes it
to ``/api/v1/memory-scoping`` but hasn't landed in this worktree yet. This
module adds that path now (reusing the same table -- no schema duplication)
plus the new §9.7 promote/correct/dispute routes; reconcile into one
canonical ``/api/v1/memory-scoping`` module at merge rather than keeping two.

Promotion is always an explicit call -- no code path here or elsewhere
auto-promotes a session/user-scope memory to a broader scope (§9.7).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.knowledge.injection_safety import filter_for_store
from shared.knowledge.scoping import ScopedRecord, ScopeType, TrustTier, resolve_conflict
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner

from ...extensions import db
from ...services.content_filter_deps import (
    get_content_filter_features,
    get_content_filter_license_client,
)
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_DEFAULT_RELEVANCE_CUTOFF = 0.7
_DEFAULT_TOP_K = 3
_PROMOTABLE_SCOPES = ("repo", "project", "org")
_MEMORY_SCOPING_ADMIN = Permission.MEMORY_SCOPING_ADMIN.value


# ---------------------------------------------------------------------------
# Request/response schemas (quart-schema). Request-body fields are optional
# partial updates -- ``None`` means "not supplied".
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryScopingConfigRequest:
    """Request body for POST /api/v1/memory-scoping."""

    organization_id: int | None = None
    relevance_cutoff: float | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class MemoryPromoteRequest:
    """Request body for POST /api/v1/memory/<id>/promote."""

    target_scope: str | None = None
    scope_ref: str | None = None


@dataclass(slots=True)
class MemoryCorrectRequest:
    """Request body for POST /api/v1/memory/<id>/correct."""

    content: str | None = None


@dataclass(slots=True)
class MemoryScopingConfigResponse:
    """Response body for GET /api/v1/memory-scoping."""

    organization_id: int | None
    enabled: bool
    relevance_cutoff: float
    top_k: int
    configured: bool


@dataclass(slots=True)
class MemoryScopingWriteResponse:
    """Response body for POST /api/v1/memory-scoping."""

    status: str
    organization_id: int
    top_k: int


@dataclass(slots=True)
class MemoryPromoteResponse:
    """Response body for a successful memory promotion."""

    status: str
    id: int
    scope_type: str
    scope_ref: str | None


@dataclass(slots=True)
class MemoryCorrectResponse:
    """Response body for a successful memory correction."""

    status: str
    old_id: int
    new_id: int
    version: int
    resolution_reason: str


@dataclass(slots=True)
class MemoryDisputeResponse:
    """Response body for a successful memory dispute."""

    status: str
    id: int
    disputed_by: int | None


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset
    before ``init_db()`` runs at startup; every route below only executes
    after that point, so this narrows the type for mypy without adding any
    reachable failure mode.
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _has_scoping_admin() -> bool:
    """True when the caller's token carries the ``memory_scoping:admin`` scope.

    Authorization is scope-only per house policy (``security.md``): the
    ``roles`` claim / ``g.user["role"]`` is audit/display, never an access
    decision. This is the scope the ``/api/v1/memory-scoping`` POST route
    already gates on (:class:`Permission.MEMORY_SCOPING_ADMIN`), reused here
    so the promote/correct owner-or-admin gate keys on the same scope rather
    than the ``role == "admin"`` string it used before.
    """
    return _MEMORY_SCOPING_ADMIN in set(g.user.get("scope") or ())


def _row_to_scoped_record(row: Any) -> ScopedRecord:
    """Adapt a memory_embeddings row into the pure scoping.py model."""
    return ScopedRecord(
        id=str(row.id),
        content=row.content,
        scope_type=ScopeType(row.scope_type),
        scope_ref=getattr(row, "scope_ref", None) or "",
        trust_tier=TrustTier(getattr(row, "trust_tier", None) or "unverified"),
        author_user_id=str(row.author_user_id),
        org=str(row.organization_id),
        version=getattr(row, "version", None) or 1,
        status=getattr(row, "status", None) or "active",
    )


def _resolve_readable_org(requested_org_id: int | None) -> tuple[int | None, tuple | None]:
    """Resolve which org a memory-scoping read may target, or refuse the read.

    Returns ``(org_id, None)`` when the read is allowed, or
    ``(None, error_response)`` when it is not.

    regression: audit-2026-09-14 -- the caller-supplied ``organization_id``
    query parameter used to win unconditionally, so any authenticated user
    could read another org's memory-injection settings with
    ``?organization_id=<other>``. A tenant id is never taken from a request
    parameter: the caller's own org (from the validated token) is
    authoritative, and only a caller holding ``memory_scoping:admin`` --
    the same scope the sibling write route requires -- may name a different
    one. An explicit mismatch is refused with 403 rather than silently
    substituted, so the parameter cannot be used to probe other orgs.
    """
    caller_org_id = g.user.get("organization_id")
    if requested_org_id is None or requested_org_id == caller_org_id:
        return caller_org_id, None
    if _has_scoping_admin():
        return requested_org_id, None
    return None, (
        jsonify({"error": "Cannot read another organization's memory-scoping config"}),
        403,
    )


def _belongs_to_org(row: Any, org_id: Any) -> bool:
    """Re-check in Python that a fetched memory row really is this tenant's.

    Deliberately redundant with the ``organization_id`` term already in each
    route's select: that query is the primary control, this is the guard
    that keeps a cross-tenant row from being acted on if the query is ever
    widened or reordered. Same defence-in-depth rationale as
    cache_configs._row_visible_to (audit-2026-09-14).
    """
    return org_id is not None and getattr(row, "organization_id", None) == org_id


@api_v1_bp.route("/memory-scoping", methods=["GET"])
@require_auth
@validate_response(MemoryScopingConfigResponse, 200)
async def get_memory_config_v2():
    """§9.4 seeded conversation-memory defaults (0.7 relevance cutoff, top-3 injection)."""
    org_id, denied = _resolve_readable_org(request.args.get("organization_id", type=int))
    if denied is not None:
        return denied

    def _fetch() -> Any:
        conn = _db()
        return conn(conn.conversation_memory_configs.organization_id == org_id).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return MemoryScopingConfigResponse(
            organization_id=org_id,
            enabled=True,
            relevance_cutoff=_DEFAULT_RELEVANCE_CUTOFF,
            top_k=_DEFAULT_TOP_K,
            configured=False,
        )
    return MemoryScopingConfigResponse(
        organization_id=org_id,
        enabled=row.enabled,
        relevance_cutoff=float(row.similarity_threshold),
        top_k=_DEFAULT_TOP_K,
        configured=True,
    )


@api_v1_bp.route("/memory-scoping", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_SCOPING_ADMIN)
@validate_response(MemoryScopingWriteResponse, 200)
@validate_response(MemoryScopingWriteResponse, 201)
@validate_request(MemoryScopingConfigRequest)
async def set_memory_config_v2(data: MemoryScopingConfigRequest):
    """Create/update the §9.4 conversation-memory config for an org."""
    org_id = data.organization_id
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    cutoff = (
        data.relevance_cutoff if data.relevance_cutoff is not None else _DEFAULT_RELEVANCE_CUTOFF
    )
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
        return jsonify({"error": "relevance_cutoff must be a number between 0 and 1"}), 400
    if not (0.0 <= float(cutoff) <= 1.0):
        return jsonify({"error": "relevance_cutoff must be between 0 and 1"}), 400

    def _upsert() -> str:
        conn = _db()
        existing = conn(conn.conversation_memory_configs.organization_id == org_id).select().first()
        if existing:
            # regression: penguin_dal's Row has no update_record() (classic
            # PyDAL API); the correct penguin_dal update is
            # db(condition).update(**kwargs) -- see shared/auth/rbac.py for
            # the identical fix. Unlike the /memory-config sibling routes in
            # memory_config.py, this route has no surrounding try/except, so
            # the old call's AttributeError propagated uncaught out of the
            # route entirely on every update -- only the very first §9.4
            # config write for an org ever succeeded.
            conn(conn.conversation_memory_configs.id == existing.id).update(
                enabled=data.enabled if data.enabled is not None else existing.enabled,
                similarity_threshold=cutoff,
            )
            return "updated"
        conn.conversation_memory_configs.insert(
            organization_id=org_id,
            enabled=data.enabled if data.enabled is not None else True,
            max_messages=20,
            similarity_threshold=cutoff,
        )
        return "created"

    status = await asyncio.to_thread(_upsert)
    code = 200 if status == "updated" else 201
    return (
        MemoryScopingWriteResponse(status=status, organization_id=org_id, top_k=_DEFAULT_TOP_K),
        code,
    )


@api_v1_bp.route("/memory/<int:item_id>/promote", methods=["POST"])
@require_auth
@validate_response(MemoryPromoteResponse, 200)
@validate_request(MemoryPromoteRequest)
async def memory_promote(item_id: int, data: MemoryPromoteRequest):
    """Explicitly promote a session/user-scope memory to repo/project/org (§9.7).

    Never automatic -- this endpoint is the only code path that changes
    ``scope_type``. Only the memory's owner or a ``memory_scoping:admin``
    holder may promote it.
    """
    target_scope = data.target_scope
    if target_scope not in _PROMOTABLE_SCOPES:
        return jsonify({"error": f"target_scope must be one of {_PROMOTABLE_SCOPES}"}), 400

    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        conn = _db()
        mem = conn.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        return conn(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None or not _belongs_to_org(row, org_id):
        return jsonify({"error": "not found"}), 404

    # Owner-or-admin gate. Authorization keys on the memory_scoping:admin
    # scope, never the role-name string (house policy: security.md).
    if row.author_user_id != user_id and not _has_scoping_admin():
        return jsonify({"error": "only the owner or an admin may promote this memory"}), 403

    scope_ref = str(org_id) if target_scope == "org" else data.scope_ref
    if target_scope != "org" and not scope_ref:
        return jsonify({"error": "scope_ref required for repo/project promotion"}), 400

    def _promote() -> None:
        conn = _db()
        conn(conn.memory_embeddings.id == item_id).update(
            scope_type=target_scope, scope_ref=scope_ref, trust_tier="confirmed"
        )
        conn.commit()

    await asyncio.to_thread(_promote)
    return MemoryPromoteResponse(
        status="promoted",
        id=item_id,
        scope_type=target_scope,
        scope_ref=scope_ref,
    )


@api_v1_bp.route("/memory/<int:item_id>/correct", methods=["POST"])
@require_auth
@validate_response(MemoryCorrectResponse, 200)
@validate_request(MemoryCorrectRequest)
async def memory_correct(item_id: int, data: MemoryCorrectRequest):
    """Version a memory with a correction; resolve by trust before superseding (§9.7).

    The correction only supersedes the original when it wins by trust ->
    confirmation -> recency (:func:`shared.knowledge.scoping.resolve_conflict`);
    a lower-trust "correction" of a verified fact is itself quarantined
    instead, never silently overwriting higher-trust knowledge.
    """
    new_content = data.content
    if not new_content:
        return jsonify({"error": "content required"}), 400

    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        conn = _db()
        mem = conn.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        return conn(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None or not _belongs_to_org(row, org_id):
        return jsonify({"error": "not found"}), 404

    # Owner-or-admin gate. Scope, not role name (house policy: security.md).
    if row.author_user_id != user_id and not _has_scoping_admin():
        return jsonify({"error": "only the owner or an admin may correct this memory"}), 403

    scanner = PromptSecurityScanner(db)
    content_filter = ContentFilter(
        db=db,
        license_client=get_content_filter_license_client(),
        features=get_content_filter_features(),
    )
    filter_result = await filter_for_store(
        new_content, scanner, content_filter, org_id=org_id, user_id=user_id
    )
    if filter_result.quarantined:
        return (
            jsonify(
                {
                    "error": "correction rejected by content-safety filter",
                    "reason": filter_result.reason,
                }
            ),
            400,
        )

    existing_record = _row_to_scoped_record(row)
    new_record = ScopedRecord(
        id="pending",
        content=filter_result.content,
        scope_type=existing_record.scope_type,
        scope_ref=existing_record.scope_ref,
        trust_tier=TrustTier.CONFIRMED,  # an explicit correction is user-confirmed
        author_user_id=str(user_id),
        org=str(org_id),
        version=(existing_record.version or 1) + 1,
        created_at=datetime.utcnow(),
    )
    resolution = resolve_conflict(new_record, existing_record)
    new_wins = resolution.winner_id != existing_record.id

    def _apply_correction() -> int:
        conn = _db()
        new_status = "active" if new_wins else "quarantined"
        old_status = "quarantined" if new_wins else "active"
        new_id = conn.memory_embeddings.insert(
            user_id=row.user_id,
            organization_id=row.organization_id,
            session_id=row.session_id,
            content=filter_result.content,
            role=row.role,
            scope_type=row.scope_type,
            author_user_id=user_id,
            scope_ref=getattr(row, "scope_ref", None),
            trust_tier="confirmed",
            version=new_record.version,
            status=new_status,
        )
        conn(conn.memory_embeddings.id == item_id).update(
            status=old_status, superseded_by=new_id if new_wins else None
        )
        conn.commit()
        return new_id

    new_id = await asyncio.to_thread(_apply_correction)
    return MemoryCorrectResponse(
        status="corrected" if new_wins else "correction_quarantined",
        old_id=item_id,
        new_id=new_id,
        version=new_record.version,
        resolution_reason=resolution.reason,
    )


@api_v1_bp.route("/memory/<int:item_id>/dispute", methods=["POST"])
@require_auth
@validate_response(MemoryDisputeResponse, 200)
async def memory_dispute(item_id: int):
    """Flag a shared memory as disputed -- quarantined pending review (§9.7).

    Any repo/project member may dispute a shared memory (not just the
    owner); the acting user is recorded in ``provenance`` for attribution.
    """
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _dispute() -> bool:
        conn = _db()
        mem = conn.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        existing = conn(query).select().first()
        if existing is None or not _belongs_to_org(existing, org_id):
            return False
        provenance = dict(getattr(existing, "provenance", None) or {})
        provenance["disputed_by"] = user_id
        provenance["disputed_at"] = datetime.utcnow().isoformat()
        conn(query).update(status="quarantined", provenance=provenance)
        conn.commit()
        return True

    disputed = await asyncio.to_thread(_dispute)
    if not disputed:
        return jsonify({"error": "not found"}), 404
    return MemoryDisputeResponse(status="quarantined", id=item_id, disputed_by=user_id)
