"""Memory Injection Configuration Routes

Manages per-organization configuration for:
- Conversation memory injection (mem0 via pgvector)
- RAG document retrieval injection
- Embedding backend settings (ollama/openai/anthropic)

Re-homed from the deleted MarchProxy AILB coupling (formerly
``api/v1/ailb_memory.py`` under the ``/ailb/*`` prefix) -- this
functionality is native to WaddleAI's own memory subsystem and was never
actually AILB-specific, so it survives the MarchProxy deletion under its
own top-level path.
"""

import asyncio
import logging

from quart import jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory config (conversation history injection)
# ---------------------------------------------------------------------------


@api_v1_bp.route("/memory-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def get_memory_config():
    """Get memory injection config for an organization."""
    org_id = request.args.get("organization_id", type=int)
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    try:

        def _fetch():
            rows = db(db.conversation_memory_configs.organization_id == org_id).select()
            return rows.first() if rows else None

        config = await asyncio.to_thread(_fetch)
        if not config:
            return (
                jsonify(
                    {
                        "organization_id": org_id,
                        "enabled": False,
                        "max_messages": 20,
                        "similarity_threshold": 0.7,
                        "configured": False,
                    }
                ),
                200,
            )

        return (
            jsonify(
                {
                    "organization_id": org_id,
                    "enabled": config.enabled,
                    "max_messages": config.max_messages,
                    "similarity_threshold": float(config.similarity_threshold),
                    "configured": True,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.error("get_memory_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@api_v1_bp.route("/memory-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def set_memory_config():
    """Create or update memory injection config for an organization."""
    data = (await request.get_json(force=True)) or {}
    org_id = data.get("organization_id")
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    try:

        def _upsert():
            existing = db(db.conversation_memory_configs.organization_id == org_id).select().first()
            if existing:
                existing.update_record(
                    enabled=data.get("enabled", existing.enabled),
                    max_messages=data.get("max_messages", existing.max_messages),
                    similarity_threshold=data.get("similarity_threshold", existing.similarity_threshold),
                )
                return "updated"
            else:
                db.conversation_memory_configs.insert(
                    organization_id=org_id,
                    enabled=data.get("enabled", True),
                    max_messages=data.get("max_messages", 20),
                    similarity_threshold=data.get("similarity_threshold", 0.7),
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return jsonify({"status": "updated", "organization_id": org_id}), 200
        return jsonify({"status": "created", "organization_id": org_id}), 201
    except Exception as exc:
        logger.error("set_memory_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# RAG config
# ---------------------------------------------------------------------------


@api_v1_bp.route("/rag-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def get_rag_config():
    """Get RAG injection config for an organization."""
    org_id = request.args.get("organization_id", type=int)
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    try:

        def _fetch():
            rows = db(db.rag_configs.organization_id == org_id).select()
            return rows.first() if rows else None

        config = await asyncio.to_thread(_fetch)
        if not config:
            return (
                jsonify(
                    {
                        "organization_id": org_id,
                        "enabled": False,
                        "collection": "default",
                        "top_k": 5,
                        "similarity_threshold": 0.7,
                        "configured": False,
                    }
                ),
                200,
            )

        return (
            jsonify(
                {
                    "organization_id": org_id,
                    "enabled": config.enabled,
                    "collection": config.collection,
                    "top_k": config.top_k,
                    "similarity_threshold": float(config.similarity_threshold),
                    "configured": True,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.error("get_rag_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@api_v1_bp.route("/rag-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def set_rag_config():
    """Create or update RAG injection config for an organization."""
    data = (await request.get_json(force=True)) or {}
    org_id = data.get("organization_id")
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    try:

        def _upsert():
            existing = db(db.rag_configs.organization_id == org_id).select().first()
            if existing:
                existing.update_record(
                    enabled=data.get("enabled", existing.enabled),
                    collection=data.get("collection", existing.collection),
                    top_k=data.get("top_k", existing.top_k),
                    similarity_threshold=data.get("similarity_threshold", existing.similarity_threshold),
                )
                return "updated"
            else:
                db.rag_configs.insert(
                    organization_id=org_id,
                    enabled=data.get("enabled", False),
                    collection=data.get("collection", "default"),
                    top_k=data.get("top_k", 5),
                    similarity_threshold=data.get("similarity_threshold", 0.7),
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return jsonify({"status": "updated", "organization_id": org_id}), 200
        return jsonify({"status": "created", "organization_id": org_id}), 201
    except Exception as exc:
        logger.error("set_rag_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Embedding config
# ---------------------------------------------------------------------------


@api_v1_bp.route("/embedding-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def get_embedding_config():
    """Get embedding backend config (global or per-org)."""
    org_id = request.args.get("organization_id", type=int)  # optional; None = global

    try:

        def _fetch():
            if org_id:
                rows = db(db.embedding_settings.organization_id == org_id).select()
            else:
                rows = db(db.embedding_settings.organization_id == None).select()  # noqa: E711
            return rows.first() if rows else None

        config = await asyncio.to_thread(_fetch)
        if not config:
            return (
                jsonify(
                    {
                        "organization_id": org_id,
                        "backend": "ollama",
                        "model": "nomic-embed-text",
                        "ollama_host": "http://localhost:11434",
                        "dimensions": 768,
                        "configured": False,
                    }
                ),
                200,
            )

        return (
            jsonify(
                {
                    "organization_id": config.organization_id,
                    "backend": config.backend,
                    "model": config.model,
                    "ollama_host": config.ollama_host,
                    "dimensions": config.dimensions,
                    "configured": True,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.error("get_embedding_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@api_v1_bp.route("/embedding-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
async def set_embedding_config():
    """Create or update embedding backend config."""
    data = (await request.get_json(force=True)) or {}

    backend = data.get("backend", "ollama")
    if backend not in ("ollama", "openai", "anthropic"):
        return jsonify({"error": "backend must be one of: ollama, openai, anthropic"}), 400

    org_id = data.get("organization_id")  # None = global default

    try:

        def _upsert():
            if org_id:
                existing = db(db.embedding_settings.organization_id == org_id).select().first()
            else:
                existing = db(db.embedding_settings.organization_id == None).select().first()  # noqa: E711

            if existing:
                existing.update_record(
                    backend=backend,
                    model=data.get("model", existing.model),
                    ollama_host=data.get("ollama_host", existing.ollama_host),
                    dimensions=data.get("dimensions", existing.dimensions),
                )
                return "updated"
            else:
                db.embedding_settings.insert(
                    organization_id=org_id,
                    backend=backend,
                    model=data.get("model", "nomic-embed-text"),
                    ollama_host=data.get("ollama_host", "http://localhost:11434"),
                    dimensions=data.get("dimensions", 768),
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return jsonify({"status": "updated", "backend": backend}), 200
        return jsonify({"status": "created", "backend": backend}), 201
    except Exception as exc:
        logger.error("set_embedding_config error: %s", exc)
        return jsonify({"error": str(exc)}), 500
