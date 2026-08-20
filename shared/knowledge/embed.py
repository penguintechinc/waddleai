"""Cached embedding access layer over ``embedding_cache`` (§6A.3).

Every embedder in the knowledge layer (CodeRAG chunks, docs cache pages,
uploaded knowledge, memory writes) goes through :func:`embed_cached` so that
identical content is never embedded twice: it hashes the content, checks
``embedding_cache`` for ``(model, content_hash)``, and only calls the
backend embedder on a miss.

Dimension note (do not "fix" this by pointing at the other embedder): this
module resolves the §7.1 ``embeddings`` assignment, default
``nomic-embed-text`` at **768 dimensions** (``penguin_dal``/Ollama path via
``shared.utils.embedding_manager``). ``rag_integration.py``'s in-process
``SentenceTransformer`` (``all-MiniLM-L6-v2``, 384-dim) is a separate,
unrelated embedding path for a different table (``rag_configs``' existing
retrieval) -- mixing the two would corrupt vector search with a dimension
mismatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from shared.utils.embedding_manager import EmbeddingManager, create_embedding_manager

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_EMBEDDING_DIMENSIONS = 768


def resolve_embedding_model(db: object | None) -> str:
    """Resolve the embedding model from the §7.1 ``embeddings`` assignment.

    Reads ``model_assignments`` where ``tool_type='embeddings'`` (populated
    by migration 010, smart-routing -- not present in every worktree/DB yet).
    Falls back to the hardcoded ``nomic-embed-text`` (768-dim) default when
    the assignment table or row is absent, so the knowledge layer works
    standalone before smart-routing lands. Never raises.
    """
    if db is None:
        return DEFAULT_EMBEDDING_MODEL
    try:
        table = getattr(db, "model_assignments", None)
        if table is None:
            return DEFAULT_EMBEDDING_MODEL
        row = db(table.tool_type == "embeddings").select().first()
        model_name = getattr(row, "model_name", None) if row else None
        if model_name:
            return str(model_name)
    except Exception as exc:  # pragma: no cover - defensive, DAL-shape dependent
        logger.warning("resolve_embedding_model: falling back to default (%s)", exc)
    return DEFAULT_EMBEDDING_MODEL


def _content_hash(content: str) -> str:
    """Stable dedup key for embedding_cache lookups."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _lookup_cache(db: object | None, model: str, content_hash: str) -> list[float] | None:
    """Synchronous embedding_cache read; ``None`` on miss or if unavailable."""
    if db is None or not hasattr(db, "embedding_cache"):
        return None
    try:
        table = db.embedding_cache
        row = db((table.model == model) & (table.content_hash == content_hash)).select().first()
        if row is None:
            return None
        embedding = getattr(row, "embedding", None)
        if embedding is not None:
            return list(embedding)
        embedding_json = getattr(row, "embedding_json", None)
        if embedding_json:
            return list(json.loads(embedding_json))
    except Exception as exc:  # pragma: no cover - defensive, DAL-shape dependent
        logger.warning("embedding_cache lookup failed, treating as miss: %s", exc)
    return None


def _store_cache(db: object | None, model: str, content_hash: str, vector: list[float]) -> None:
    """Best-effort embedding_cache write; failures never break the caller."""
    if db is None or not hasattr(db, "embedding_cache"):
        return
    try:
        db.embedding_cache.insert(model=model, content_hash=content_hash, embedding=vector)
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive, DAL-shape dependent
        logger.warning("embedding_cache write failed (non-fatal): %s", exc)


async def embed_cached(
    content: str,
    db: object | None = None,
    model: str | None = None,
    embedding_manager: EmbeddingManager | None = None,
) -> list[float]:
    """Embed ``content``, deduplicating compute through ``embedding_cache``.

    Looks up ``(model, content_hash)`` before calling the backend embedder;
    writes the result back on a miss. The CPU-bound backend call runs in a
    thread so the event loop stays responsive.

    Args:
        content: Text to embed.
        db: penguin-dal handle exposing ``embedding_cache`` (and optionally
            ``model_assignments``); ``None`` disables caching/resolution but
            still returns an embedding.
        model: Explicit model override; resolved via
            :func:`resolve_embedding_model` when omitted.
        embedding_manager: Explicit backend override (tests); a default
            manager for the resolved model is constructed otherwise.

    Returns:
        The embedding vector (768-dim for the default nomic-embed-text path).

    """
    resolved_model = model or resolve_embedding_model(db)
    content_hash = _content_hash(content)

    cached = await asyncio.to_thread(_lookup_cache, db, resolved_model, content_hash)
    if cached is not None:
        return cached

    manager = embedding_manager or create_embedding_manager(model=resolved_model)
    vector = await asyncio.to_thread(manager.embed, content)

    await asyncio.to_thread(_store_cache, db, resolved_model, content_hash, vector)
    return vector
