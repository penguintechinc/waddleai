"""
mem0-compatible REST API for WaddleAI's pgvector memory backend.

MarchProxy's AILB is configured with a mem0 endpoint pointing here:
    mem0_endpoint = "http://waddleai-proxy:8080/mem0"

WaddleAI handles embedding generation, pgvector storage, and retrieval.
MarchProxy uses the standard mem0 client interface and is unaware of the
underlying implementation.

Endpoints (mem0 REST API subset):
    POST   /mem0/memories          — Add a memory
    POST   /mem0/memories/search   — Search memories (called before each LLM turn)
    GET    /mem0/memories          — List memories for a user
    DELETE /mem0/memories/{id}     — Delete a specific memory
    DELETE /mem0/memories          — Clear all memories for a user
"""

import logging
from datetime import datetime

from quart import Blueprint, abort, jsonify, request

logger = logging.getLogger(__name__)

# Blueprint — mounted at /mem0 in main.py
mem0_bp = Blueprint("mem0", __name__, url_prefix="/mem0")

# The memory manager is set by the proxy startup (injected after initialization)
_memory_manager = None


def set_memory_manager(manager) -> None:
    """Called from proxy startup to inject the initialized memory manager."""
    global _memory_manager
    _memory_manager = manager


def get_memory_manager():
    if _memory_manager is None:
        abort(503, description="Memory manager not initialized")
    return _memory_manager


# Note: every handler below reaches the underlying MemoryStore via
# `manager.memory_store` (WaddleAIMemoryManager's actual attribute name --
# see shared/utils/memory_integration.py). Previously read `manager.store`,
# which does not exist on WaddleAIMemoryManager and raised AttributeError
# unconditionally on every call in every environment.


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@mem0_bp.route("/memories", methods=["POST"])
async def add_memories():
    """Add conversation messages to memory.

    Called by MarchProxy after each LLM turn to persist the conversation.
    """
    manager = get_memory_manager()
    body = await request.get_json()

    if not body:
        abort(400, description="Request body required")

    messages = body.get("messages", [])
    user_id_raw = body.get("user_id", "0")
    agent_id = body.get("agent_id")
    run_id = body.get("run_id")
    metadata = body.get("metadata", {})
    organization_id = body.get("organization_id")

    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = organization_id or 0
    session_id = agent_id or run_id or ""

    from shared.utils.memory_integration import MemoryEntry

    stored = 0
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "").strip()
        if not content:
            continue

        entry = MemoryEntry(
            id="",
            user_id=user_id_int,
            organization_id=org_id,
            session_id=session_id,
            content=content,
            metadata={**metadata, "role": role},
            embedding=None,
            created_at=datetime.utcnow(),
        )
        success = await manager.memory_store.store_memory(entry)
        if success:
            stored += 1

    return jsonify(
        {
            "status": "success",
            "stored": stored,
            "user_id": str(user_id_raw),
            "session_id": session_id,
        }
    )


@mem0_bp.route("/memories/search", methods=["POST"])
async def search_memories():
    """Search memories by semantic similarity.

    Called by MarchProxy before each LLM turn to retrieve relevant context.
    """
    manager = get_memory_manager()
    body = await request.get_json()

    if not body:
        abort(400, description="Request body required")

    query = body.get("query", "")
    user_id_raw = body.get("user_id", "0")
    agent_id = body.get("agent_id")
    run_id = body.get("run_id")
    limit = body.get("limit", 10)
    threshold = body.get("threshold", 0.7)
    organization_id = body.get("organization_id")

    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = organization_id or 0
    session_id = agent_id or run_id or None

    entries = await manager.memory_store.search_memories(
        query=query,
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        limit=limit,
        min_relevance=threshold,
    )

    results = [
        {
            "id": entry.id,
            "memory": entry.content,
            "user_id": str(entry.user_id),
            "score": entry.relevance_score,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry in entries
    ]

    return jsonify({"results": results, "total": len(results)})


@mem0_bp.route("/memories", methods=["GET"])
async def list_memories():
    """List recent memories for a user (chronological order)."""
    manager = get_memory_manager()

    user_id_raw = request.args.get("user_id", "0")
    agent_id = request.args.get("agent_id")
    run_id = request.args.get("run_id")
    limit = int(request.args.get("limit", "20"))
    organization_id_raw = request.args.get("organization_id")

    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = int(organization_id_raw) if organization_id_raw else 0
    session_id = agent_id or run_id or ""

    entries = await manager.memory_store.get_conversation_history(
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        limit=limit,
    )

    results = [
        {
            "id": entry.id,
            "memory": entry.content,
            "user_id": str(entry.user_id),
            "score": entry.relevance_score,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry in entries
    ]

    return jsonify({"memories": results, "total": len(results)})


@mem0_bp.route("/memories/<memory_id>", methods=["DELETE"])
async def delete_memory(memory_id: str):
    """Delete a specific memory by ID."""
    manager = get_memory_manager()

    user_id_raw = request.args.get("user_id", "0")
    organization_id_raw = request.args.get("organization_id")

    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = int(organization_id_raw) if organization_id_raw else 0

    # Delete via raw SQL (direct write to primary)
    try:
        manager.memory_store.write_db.executesql(
            "DELETE FROM memory_embeddings WHERE id = %s AND user_id = %s AND organization_id = %s",
            (int(memory_id), user_id_int, org_id),
        )
        return jsonify({"status": "deleted", "id": memory_id})
    except Exception as exc:
        logger.error("Failed to delete memory %s: %s", memory_id, exc)
        abort(500, description="Failed to delete memory")


@mem0_bp.route("/memories", methods=["DELETE"])
async def clear_memories():
    """Clear all memories for a user (optionally scoped to a session)."""
    manager = get_memory_manager()

    user_id_raw = request.args.get("user_id", "0")
    agent_id = request.args.get("agent_id")
    run_id = request.args.get("run_id")
    organization_id_raw = request.args.get("organization_id")

    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = int(organization_id_raw) if organization_id_raw else 0
    session_id = agent_id or run_id or None

    success = await manager.memory_store.clear_memories(
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
    )

    if not success:
        abort(500, description="Failed to clear memories")

    return jsonify({"status": "cleared", "user_id": str(user_id_raw)})
