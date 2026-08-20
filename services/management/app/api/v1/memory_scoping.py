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
from datetime import datetime
from typing import Any

from quart import g, jsonify, request

from shared.auth.rbac import Permission
from shared.knowledge.injection_safety import filter_for_store
from shared.knowledge.scoping import ScopedRecord, ScopeType, TrustTier, resolve_conflict
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_DEFAULT_RELEVANCE_CUTOFF = 0.7
_DEFAULT_TOP_K = 3
_PROMOTABLE_SCOPES = ("repo", "project", "org")


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


@api_v1_bp.route("/memory-scoping", methods=["GET"])
@require_auth
async def get_memory_config_v2():
    """§9.4 seeded conversation-memory defaults (0.7 relevance cutoff, top-3 injection)."""
    org_id = request.args.get("organization_id", type=int) or g.user.get("organization_id")

    def _fetch() -> Any:
        return db(db.conversation_memory_configs.organization_id == org_id).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return (
            jsonify(
                {
                    "organization_id": org_id,
                    "enabled": True,
                    "relevance_cutoff": _DEFAULT_RELEVANCE_CUTOFF,
                    "top_k": _DEFAULT_TOP_K,
                    "configured": False,
                }
            ),
            200,
        )
    return (
        jsonify(
            {
                "organization_id": org_id,
                "enabled": row.enabled,
                "relevance_cutoff": float(row.similarity_threshold),
                "top_k": _DEFAULT_TOP_K,
                "configured": True,
            }
        ),
        200,
    )


@api_v1_bp.route("/memory-scoping", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_SCOPING_ADMIN)
async def set_memory_config_v2():
    """Create/update the §9.4 conversation-memory config for an org."""
    data = (await request.get_json(force=True)) or {}
    org_id = data.get("organization_id")
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    cutoff = data.get("relevance_cutoff", _DEFAULT_RELEVANCE_CUTOFF)

    def _upsert() -> str:
        existing = db(db.conversation_memory_configs.organization_id == org_id).select().first()
        if existing:
            # regression: penguin_dal's Row has no update_record() (classic
            # PyDAL API); the correct penguin_dal update is
            # db(condition).update(**kwargs) -- see shared/auth/rbac.py for
            # the identical fix. Unlike the /memory-config sibling routes in
            # memory_config.py, this route has no surrounding try/except, so
            # the old call's AttributeError propagated uncaught out of the
            # route entirely on every update -- only the very first §9.4
            # config write for an org ever succeeded.
            db(db.conversation_memory_configs.id == existing.id).update(
                enabled=data.get("enabled", existing.enabled), similarity_threshold=cutoff
            )
            return "updated"
        db.conversation_memory_configs.insert(
            organization_id=org_id,
            enabled=data.get("enabled", True),
            max_messages=20,
            similarity_threshold=cutoff,
        )
        return "created"

    status = await asyncio.to_thread(_upsert)
    code = 200 if status == "updated" else 201
    return jsonify({"status": status, "organization_id": org_id, "top_k": _DEFAULT_TOP_K}), code


@api_v1_bp.route("/memory/<int:item_id>/promote", methods=["POST"])
@require_auth
async def memory_promote(item_id: int):
    """Explicitly promote a session/user-scope memory to repo/project/org (§9.7).

    Never automatic -- this endpoint is the only code path that changes
    ``scope_type``. Only the memory's owner or an admin may promote it.
    """
    data = (await request.get_json(force=True)) or {}
    target_scope = data.get("target_scope")
    if target_scope not in _PROMOTABLE_SCOPES:
        return jsonify({"error": f"target_scope must be one of {_PROMOTABLE_SCOPES}"}), 400

    user_id = g.user.get("user_id")
    role = g.user.get("role")
    org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        mem = db.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404

    if row.author_user_id != user_id and role != "admin":
        return jsonify({"error": "only the owner or an admin may promote this memory"}), 403

    scope_ref = str(org_id) if target_scope == "org" else data.get("scope_ref")
    if target_scope != "org" and not scope_ref:
        return jsonify({"error": "scope_ref required for repo/project promotion"}), 400

    def _promote() -> None:
        db(db.memory_embeddings.id == item_id).update(
            scope_type=target_scope, scope_ref=scope_ref, trust_tier="confirmed"
        )
        db.commit()

    await asyncio.to_thread(_promote)
    return (
        jsonify(
            {
                "status": "promoted",
                "id": item_id,
                "scope_type": target_scope,
                "scope_ref": scope_ref,
            }
        ),
        200,
    )


@api_v1_bp.route("/memory/<int:item_id>/correct", methods=["POST"])
@require_auth
async def memory_correct(item_id: int):
    """Version a memory with a correction; resolve by trust before superseding (§9.7).

    The correction only supersedes the original when it wins by trust ->
    confirmation -> recency (:func:`shared.knowledge.scoping.resolve_conflict`);
    a lower-trust "correction" of a verified fact is itself quarantined
    instead, never silently overwriting higher-trust knowledge.
    """
    data = (await request.get_json(force=True)) or {}
    new_content = data.get("content")
    if not new_content:
        return jsonify({"error": "content required"}), 400

    user_id = g.user.get("user_id")
    role = g.user.get("role")
    org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        mem = db.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404

    if row.author_user_id != user_id and role != "admin":
        return jsonify({"error": "only the owner or an admin may correct this memory"}), 403

    scanner = PromptSecurityScanner(db)
    content_filter = ContentFilter(db=db)
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
        new_status = "active" if new_wins else "quarantined"
        old_status = "quarantined" if new_wins else "active"
        new_id = db.memory_embeddings.insert(
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
        db(db.memory_embeddings.id == item_id).update(
            status=old_status, superseded_by=new_id if new_wins else None
        )
        db.commit()
        return new_id

    new_id = await asyncio.to_thread(_apply_correction)
    return (
        jsonify(
            {
                "status": "corrected" if new_wins else "correction_quarantined",
                "old_id": item_id,
                "new_id": new_id,
                "version": new_record.version,
                "resolution_reason": resolution.reason,
            }
        ),
        200,
    )


@api_v1_bp.route("/memory/<int:item_id>/dispute", methods=["POST"])
@require_auth
async def memory_dispute(item_id: int):
    """Flag a shared memory as disputed -- quarantined pending review (§9.7).

    Any repo/project member may dispute a shared memory (not just the
    owner); the acting user is recorded in ``provenance`` for attribution.
    """
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _dispute() -> bool:
        mem = db.memory_embeddings
        query = (mem.id == item_id) & (mem.organization_id == org_id)
        existing = db(query).select().first()
        if existing is None:
            return False
        provenance = dict(getattr(existing, "provenance", None) or {})
        provenance["disputed_by"] = user_id
        provenance["disputed_at"] = datetime.utcnow().isoformat()
        db(query).update(status="quarantined", provenance=provenance)
        db.commit()
        return True

    disputed = await asyncio.to_thread(_dispute)
    if not disputed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "quarantined", "id": item_id, "disputed_by": user_id}), 200
