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
import json
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Router — mounted at /mem0 in main.py
mem0_router = APIRouter(prefix="/mem0", tags=["mem0"])

# The memory manager is set by the proxy startup (injected after initialization)
_memory_manager = None


def set_memory_manager(manager) -> None:
    """Called from proxy startup to inject the initialized memory manager."""
    global _memory_manager
    _memory_manager = manager


def get_memory_manager():
    if _memory_manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    return _memory_manager


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class AddMemoryRequest(BaseModel):
    messages: List[Dict[str, str]] = Field(
        ..., description="List of message dicts with 'role' and 'content'"
    )
    user_id: str = Field(..., description="User identifier")
    agent_id: Optional[str] = Field(None, description="Agent/session identifier")
    run_id: Optional[str] = Field(None, description="Run/session ID (alias for agent_id)")
    metadata: Dict[str, Any] = Field(default_factory=dict)
    organization_id: Optional[int] = Field(None)


class SearchMemoryRequest(BaseModel):
    query: str = Field(..., description="Search query (last user message)")
    user_id: str = Field(..., description="User identifier")
    agent_id: Optional[str] = Field(None)
    run_id: Optional[str] = Field(None)
    limit: int = Field(10, ge=1, le=50)
    threshold: float = Field(0.7, ge=0.0, le=1.0)
    organization_id: Optional[int] = Field(None)


class MemoryResult(BaseModel):
    id: str
    memory: str
    user_id: str
    score: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@mem0_router.post("/memories", response_model=Dict[str, Any])
async def add_memories(request: AddMemoryRequest):
    """Add conversation messages to memory.

    Called by MarchProxy after each LLM turn to persist the conversation.
    """
    manager = get_memory_manager()

    try:
        user_id_int = int(request.user_id)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = request.organization_id or 0
    session_id = request.agent_id or request.run_id or ""

    from shared.utils.memory_integration import MemoryEntry

    stored = 0
    for msg in request.messages:
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
            metadata={**request.metadata, "role": role},
            embedding=None,
            created_at=datetime.utcnow(),
        )
        success = await manager.store.store_memory(entry)
        if success:
            stored += 1

    return {
        "status": "success",
        "stored": stored,
        "user_id": request.user_id,
        "session_id": session_id,
    }


@mem0_router.post("/memories/search", response_model=Dict[str, Any])
async def search_memories(request: SearchMemoryRequest):
    """Search memories by semantic similarity.

    Called by MarchProxy before each LLM turn to retrieve relevant context.
    """
    manager = get_memory_manager()

    try:
        user_id_int = int(request.user_id)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = request.organization_id or 0
    session_id = request.agent_id or request.run_id or None

    entries = await manager.store.search_memories(
        query=request.query,
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        limit=request.limit,
        min_relevance=request.threshold,
    )

    results = [
        MemoryResult(
            id=entry.id,
            memory=entry.content,
            user_id=str(entry.user_id),
            score=entry.relevance_score,
            metadata=entry.metadata,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        ).model_dump()
        for entry in entries
    ]

    return {"results": results, "total": len(results)}


@mem0_router.get("/memories", response_model=Dict[str, Any])
async def list_memories(
    user_id: str = Query(..., description="User ID"),
    agent_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    organization_id: Optional[int] = Query(None),
):
    """List recent memories for a user (chronological order)."""
    manager = get_memory_manager()

    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = organization_id or 0
    session_id = agent_id or run_id or ""

    entries = await manager.store.get_conversation_history(
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        limit=limit,
    )

    results = [
        MemoryResult(
            id=entry.id,
            memory=entry.content,
            user_id=str(entry.user_id),
            score=entry.relevance_score,
            metadata=entry.metadata,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        ).model_dump()
        for entry in entries
    ]

    return {"memories": results, "total": len(results)}


@mem0_router.delete("/memories/{memory_id}", response_model=Dict[str, Any])
async def delete_memory(
    memory_id: str = Path(..., description="Memory ID to delete"),
    user_id: str = Query(...),
    organization_id: Optional[int] = Query(None),
):
    """Delete a specific memory by ID."""
    manager = get_memory_manager()

    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = organization_id or 0

    # Delete via raw SQL (direct write to primary)
    try:
        manager.store.write_db.executesql(
            "DELETE FROM memory_embeddings WHERE id = %s AND user_id = %s AND organization_id = %s",
            (int(memory_id), user_id_int, org_id),
        )
        return {"status": "deleted", "id": memory_id}
    except Exception as exc:
        logger.error("Failed to delete memory %s: %s", memory_id, exc)
        raise HTTPException(status_code=500, detail="Failed to delete memory")


@mem0_router.delete("/memories", response_model=Dict[str, Any])
async def clear_memories(
    user_id: str = Query(...),
    agent_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    organization_id: Optional[int] = Query(None),
):
    """Clear all memories for a user (optionally scoped to a session)."""
    manager = get_memory_manager()

    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        user_id_int = 0

    org_id = organization_id or 0
    session_id = agent_id or run_id or None

    success = await manager.store.clear_memories(
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to clear memories")

    return {"status": "cleared", "user_id": user_id}
