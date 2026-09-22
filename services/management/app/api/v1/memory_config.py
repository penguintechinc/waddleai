"""Memory Injection Configuration Routes.

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
import uuid
from dataclasses import dataclass
from typing import Any

from quart import jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

# Generous bounds: these reject nonsense (a string, a negative number, an
# absurd magnitude) that previously flowed from the request JSON straight
# into the DB with no range check, not express product policy.
_MAX_MESSAGES = 10_000
_MAX_TOP_K = 1_000
_MIN_DIMENSIONS = 1
_MAX_DIMENSIONS = 100_000
_VALID_BACKENDS = ("ollama", "openai", "anthropic")


# ---------------------------------------------------------------------------
# Request/response schemas (quart-schema). Every field on a request body is
# an optional partial update -- ``None`` means "not supplied", matching the
# ``data.get(key, default)`` presence semantics the handlers used before
# they were given a typed schema.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryConfigRequest:
    """Request body for POST /api/v1/memory-config."""

    organization_id: int | None = None
    enabled: bool | None = None
    max_messages: int | None = None
    similarity_threshold: float | None = None


@dataclass(slots=True)
class RagConfigRequest:
    """Request body for POST /api/v1/rag-config."""

    organization_id: int | None = None
    enabled: bool | None = None
    collection: str | None = None
    top_k: int | None = None
    similarity_threshold: float | None = None


@dataclass(slots=True)
class EmbeddingConfigRequest:
    """Request body for POST /api/v1/embedding-config."""

    organization_id: int | None = None
    backend: str | None = None
    model: str | None = None
    ollama_host: str | None = None
    dimensions: int | None = None


@dataclass(slots=True)
class MemoryConfigResponse:
    """Response body for GET /api/v1/memory-config."""

    organization_id: int | None
    enabled: bool
    max_messages: int
    similarity_threshold: float
    configured: bool


@dataclass(slots=True)
class RagConfigResponse:
    """Response body for GET /api/v1/rag-config."""

    organization_id: int | None
    enabled: bool
    collection: str
    top_k: int
    similarity_threshold: float
    configured: bool


@dataclass(slots=True)
class EmbeddingConfigResponse:
    """Response body for GET /api/v1/embedding-config."""

    organization_id: int | None
    backend: str
    model: str
    ollama_host: str
    dimensions: int
    configured: bool


@dataclass(slots=True)
class OrgWriteResponse:
    """Response body for a memory/RAG config write, keyed by organization."""

    status: str
    organization_id: int


@dataclass(slots=True)
class BackendWriteResponse:
    """Response body for an embedding-backend config write."""

    status: str
    backend: str


def _internal_error(operation: str, exc: Exception) -> tuple[Any, int]:
    """Log an unexpected handler failure server-side and return a safe 500 body.

    regression: audit-2026-09-14 -- every handler in this module used to
    return the raw exception text straight to the caller, leaking SQL
    fragments, table names and filesystem paths. The exception (with its
    traceback) now goes to the log at ERROR and the client gets a fixed
    message plus an ``error_id`` correlating the two, so support can still
    trace a reported failure without the response carrying internal detail.
    """
    error_id = uuid.uuid4().hex
    logger.error("%s failed [error_id=%s]", operation, error_id, exc_info=exc)
    return jsonify({"error": "Internal server error", "error_id": error_id}), 500


def _threshold_error(value: float | None) -> str | None:
    """Return an error message when a similarity threshold is out of [0, 1], else None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "similarity_threshold must be a number between 0 and 1"
    if not (0.0 <= float(value) <= 1.0):
        return "similarity_threshold must be between 0 and 1"
    return None


def _positive_int_error(name: str, value: int | None, maximum: int) -> str | None:
    """Return an error message when *value* is not a positive int within *maximum*, else None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} must be an integer"
    if value < 1 or value > maximum:
        return f"{name} must be between 1 and {maximum}"
    return None


# ---------------------------------------------------------------------------
# Memory config (conversation history injection)
# ---------------------------------------------------------------------------


@api_v1_bp.route("/memory-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(MemoryConfigResponse, 200)
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
            return MemoryConfigResponse(
                organization_id=org_id,
                enabled=False,
                max_messages=20,
                similarity_threshold=0.7,
                configured=False,
            )

        return MemoryConfigResponse(
            organization_id=org_id,
            enabled=config.enabled,
            max_messages=config.max_messages,
            similarity_threshold=float(config.similarity_threshold),
            configured=True,
        )
    except Exception as exc:
        return _internal_error("get_memory_config", exc)


@api_v1_bp.route("/memory-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(OrgWriteResponse, 200)
@validate_response(OrgWriteResponse, 201)
@validate_request(MemoryConfigRequest)
async def set_memory_config(data: MemoryConfigRequest):
    """Create or update memory injection config for an organization."""
    org_id = data.organization_id
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    bounds_error = _threshold_error(data.similarity_threshold) or _positive_int_error(
        "max_messages", data.max_messages, _MAX_MESSAGES
    )
    if bounds_error:
        return jsonify({"error": bounds_error}), 400

    try:

        def _upsert():
            existing = db(db.conversation_memory_configs.organization_id == org_id).select().first()
            if existing:
                # regression: penguin_dal's Row has no update_record() (that's
                # classic PyDAL API); the correct penguin_dal update is
                # db(condition).update(**kwargs) -- see shared/auth/rbac.py for
                # the identical fix. The old call raised AttributeError, caught
                # by this route's own `except Exception` below and turned into
                # a generic 500 -- so an org could create its memory-injection
                # config once but every subsequent update permanently 500'd.
                db(db.conversation_memory_configs.id == existing.id).update(
                    enabled=data.enabled if data.enabled is not None else existing.enabled,
                    max_messages=(
                        data.max_messages
                        if data.max_messages is not None
                        else existing.max_messages
                    ),
                    similarity_threshold=(
                        data.similarity_threshold
                        if data.similarity_threshold is not None
                        else existing.similarity_threshold
                    ),
                )
                return "updated"
            else:
                db.conversation_memory_configs.insert(
                    organization_id=org_id,
                    enabled=data.enabled if data.enabled is not None else True,
                    max_messages=data.max_messages if data.max_messages is not None else 20,
                    similarity_threshold=(
                        data.similarity_threshold if data.similarity_threshold is not None else 0.7
                    ),
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return OrgWriteResponse(status="updated", organization_id=org_id), 200
        return OrgWriteResponse(status="created", organization_id=org_id), 201
    except Exception as exc:
        return _internal_error("set_memory_config", exc)


# ---------------------------------------------------------------------------
# RAG config
# ---------------------------------------------------------------------------


@api_v1_bp.route("/rag-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(RagConfigResponse, 200)
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
            return RagConfigResponse(
                organization_id=org_id,
                enabled=False,
                collection="default",
                top_k=5,
                similarity_threshold=0.7,
                configured=False,
            )

        return RagConfigResponse(
            organization_id=org_id,
            enabled=config.enabled,
            collection=config.collection,
            top_k=config.top_k,
            similarity_threshold=float(config.similarity_threshold),
            configured=True,
        )
    except Exception as exc:
        return _internal_error("get_rag_config", exc)


@api_v1_bp.route("/rag-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(OrgWriteResponse, 200)
@validate_response(OrgWriteResponse, 201)
@validate_request(RagConfigRequest)
async def set_rag_config(data: RagConfigRequest):
    """Create or update RAG injection config for an organization."""
    org_id = data.organization_id
    if not org_id:
        return jsonify({"error": "organization_id required"}), 400

    bounds_error = _threshold_error(data.similarity_threshold) or _positive_int_error(
        "top_k", data.top_k, _MAX_TOP_K
    )
    if bounds_error:
        return jsonify({"error": bounds_error}), 400

    try:

        def _upsert():
            existing = db(db.rag_configs.organization_id == org_id).select().first()
            if existing:
                # regression: see get_memory_config's set_memory_config sibling
                # above -- penguin_dal Row has no update_record(); the old call
                # here 500'd every update to an org's RAG config after the
                # first creation.
                db(db.rag_configs.id == existing.id).update(
                    enabled=data.enabled if data.enabled is not None else existing.enabled,
                    collection=(
                        data.collection if data.collection is not None else existing.collection
                    ),
                    top_k=data.top_k if data.top_k is not None else existing.top_k,
                    similarity_threshold=(
                        data.similarity_threshold
                        if data.similarity_threshold is not None
                        else existing.similarity_threshold
                    ),
                )
                return "updated"
            else:
                db.rag_configs.insert(
                    organization_id=org_id,
                    enabled=data.enabled if data.enabled is not None else False,
                    collection=data.collection if data.collection is not None else "default",
                    top_k=data.top_k if data.top_k is not None else 5,
                    similarity_threshold=(
                        data.similarity_threshold if data.similarity_threshold is not None else 0.7
                    ),
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return OrgWriteResponse(status="updated", organization_id=org_id), 200
        return OrgWriteResponse(status="created", organization_id=org_id), 201
    except Exception as exc:
        return _internal_error("set_rag_config", exc)


# ---------------------------------------------------------------------------
# Embedding config
# ---------------------------------------------------------------------------


@api_v1_bp.route("/embedding-config", methods=["GET"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(EmbeddingConfigResponse, 200)
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
            return EmbeddingConfigResponse(
                organization_id=org_id,
                backend="ollama",
                model="nomic-embed-text",
                ollama_host="http://localhost:11434",
                dimensions=768,
                configured=False,
            )

        return EmbeddingConfigResponse(
            organization_id=config.organization_id,
            backend=config.backend,
            model=config.model,
            ollama_host=config.ollama_host,
            dimensions=config.dimensions,
            configured=True,
        )
    except Exception as exc:
        return _internal_error("get_embedding_config", exc)


@api_v1_bp.route("/embedding-config", methods=["POST"])
@require_auth
@require_scope(Permission.MEMORY_CONFIG_ADMIN)
@validate_response(BackendWriteResponse, 200)
@validate_response(BackendWriteResponse, 201)
@validate_request(EmbeddingConfigRequest)
async def set_embedding_config(data: EmbeddingConfigRequest):
    """Create or update embedding backend config."""
    backend = data.backend if data.backend is not None else "ollama"
    if backend not in _VALID_BACKENDS:
        return jsonify({"error": "backend must be one of: ollama, openai, anthropic"}), 400

    dimensions_error = _positive_int_error("dimensions", data.dimensions, _MAX_DIMENSIONS)
    if dimensions_error:
        return jsonify({"error": dimensions_error}), 400

    org_id = data.organization_id  # None = global default

    try:

        def _upsert():
            if org_id:
                existing = db(db.embedding_settings.organization_id == org_id).select().first()
            else:
                existing = db(db.embedding_settings.organization_id == None).select().first()  # noqa: E711

            if existing:
                # regression: see set_memory_config above -- penguin_dal Row
                # has no update_record(); the old call here 500'd every
                # update to the embedding backend config after the first
                # creation (global default or per-org).
                db(db.embedding_settings.id == existing.id).update(
                    backend=backend,
                    model=data.model if data.model is not None else existing.model,
                    ollama_host=(
                        data.ollama_host if data.ollama_host is not None else existing.ollama_host
                    ),
                    dimensions=(
                        data.dimensions if data.dimensions is not None else existing.dimensions
                    ),
                )
                return "updated"
            else:
                db.embedding_settings.insert(
                    organization_id=org_id,
                    backend=backend,
                    model=data.model if data.model is not None else "nomic-embed-text",
                    ollama_host=(
                        data.ollama_host
                        if data.ollama_host is not None
                        else "http://localhost:11434"
                    ),
                    dimensions=data.dimensions if data.dimensions is not None else 768,
                )
                return "created"

        status = await asyncio.to_thread(_upsert)
        if status == "updated":
            return BackendWriteResponse(status="updated", backend=backend), 200
        return BackendWriteResponse(status="created", backend=backend), 201
    except Exception as exc:
        return _internal_error("set_embedding_config", exc)
