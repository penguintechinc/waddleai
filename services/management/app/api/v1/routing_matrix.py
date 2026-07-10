"""
WaddleAI Management API v1 - Routing Matrix Endpoints

CRUD operations for the AI model routing matrix. The routing matrix maps
(tool_type, complexity, region) tuples to recommended models with capability
scores, VRAM requirements, and parameter counts.

Also hosts the routing-LLM "instructions" surface (freeform natural-language
guidance + selected routing model, Redis-backed) that was previously served
by the legacy FastAPI management plane's `/routing-config` page. This is a
distinct feature from the routing matrix above: the matrix is a deterministic
(tool_type, complexity, region) -> model table, while `instructions`/`test`
configure and exercise the natural-language routing LLM consumed by
`shared.utils.request_router.LLMRequestRouter` via the same `routing:*`
Redis keys.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from quart import Blueprint, jsonify, request

from ... import extensions as _ext
from ...extensions import db
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)

routing_matrix_bp = Blueprint("routing_matrix", __name__, url_prefix="/api/v1/routing-matrix")

# Defaults mirror the legacy management plane's fallback text exactly, so
# behavior is unchanged for callers that never configured routing instructions.
_DEFAULT_ROUTING_INSTRUCTIONS = "No routing instructions configured"
_DEFAULT_ROUTING_LLM = "gemma4:2b"

# Default routing matrix spec used by the /seed endpoint
DEFAULT_ROUTING_MATRIX: List[Dict[str, Any]] = [
    {
        "tool_type": "chat",
        "complexity": "low",
        "region": "us",
        "model_name": "gpt-4o-mini",
        "model_params": "8B",
        "vram_gb": 8,
        "capability_score": 0.7,
    },
    {
        "tool_type": "chat",
        "complexity": "medium",
        "region": "us",
        "model_name": "gpt-4o",
        "model_params": "200B",
        "vram_gb": 40,
        "capability_score": 0.9,
    },
    {
        "tool_type": "chat",
        "complexity": "high",
        "region": "us",
        "model_name": "claude-3-5-sonnet-latest",
        "model_params": "175B",
        "vram_gb": 40,
        "capability_score": 0.95,
    },
    {
        "tool_type": "code",
        "complexity": "low",
        "region": "us",
        "model_name": "codellama",
        "model_params": "7B",
        "vram_gb": 8,
        "capability_score": 0.65,
    },
    {
        "tool_type": "code",
        "complexity": "medium",
        "region": "us",
        "model_name": "gpt-4o",
        "model_params": "200B",
        "vram_gb": 40,
        "capability_score": 0.88,
    },
    {
        "tool_type": "code",
        "complexity": "high",
        "region": "us",
        "model_name": "claude-3-5-sonnet-latest",
        "model_params": "175B",
        "vram_gb": 40,
        "capability_score": 0.96,
    },
    {
        "tool_type": "embed",
        "complexity": "low",
        "region": "us",
        "model_name": "nomic-embed-text",
        "model_params": "137M",
        "vram_gb": 2,
        "capability_score": 0.8,
    },
    {
        "tool_type": "embed",
        "complexity": "medium",
        "region": "us",
        "model_name": "text-embedding-3-small",
        "model_params": "1.5B",
        "vram_gb": 4,
        "capability_score": 0.85,
    },
    {
        "tool_type": "embed",
        "complexity": "high",
        "region": "us",
        "model_name": "text-embedding-3-large",
        "model_params": "3B",
        "vram_gb": 8,
        "capability_score": 0.92,
    },
    {
        "tool_type": "chat",
        "complexity": "low",
        "region": "eu",
        "model_name": "mistral",
        "model_params": "7B",
        "vram_gb": 8,
        "capability_score": 0.7,
    },
    {
        "tool_type": "chat",
        "complexity": "medium",
        "region": "eu",
        "model_name": "mixtral",
        "model_params": "46.7B",
        "vram_gb": 24,
        "capability_score": 0.82,
    },
    {
        "tool_type": "chat",
        "complexity": "high",
        "region": "eu",
        "model_name": "claude-3-5-sonnet-latest",
        "model_params": "175B",
        "vram_gb": 40,
        "capability_score": 0.95,
    },
]


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Convert a penguin-dal row to a serializable dictionary."""
    return {
        "id": row.id,
        "tool_type": row.tool_type,
        "complexity": row.complexity,
        "region": row.region,
        "model_name": row.model_name,
        "model_params": row.model_params,
        "vram_gb": row.vram_gb,
        "capability_score": row.capability_score,
        "enabled": row.enabled,
        "credential_label": row.credential_label if hasattr(row, "credential_label") else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@routing_matrix_bp.route("/", methods=["GET"])
@require_auth
async def list_entries() -> tuple:
    """List all routing matrix entries with optional filters.

    Query params: tool_type, complexity, region, enabled
    """
    tool_type: Optional[str] = request.args.get("tool_type")
    complexity: Optional[str] = request.args.get("complexity")
    region: Optional[str] = request.args.get("region")
    enabled_param: Optional[str] = request.args.get("enabled")

    def _fetch():
        query = db.routing_matrix.id > 0

        if tool_type:
            query &= db.routing_matrix.tool_type == tool_type

        if complexity:
            query &= db.routing_matrix.complexity == complexity

        if region:
            query &= db.routing_matrix.region == region

        if enabled_param is not None:
            enabled_val: bool = enabled_param.lower() in ("true", "1", "yes")
            query &= db.routing_matrix.enabled == enabled_val

        return db(query).select(orderby=db.routing_matrix.id)

    rows = await asyncio.to_thread(_fetch)
    entries: List[Dict[str, Any]] = [_row_to_dict(r) for r in rows]

    return (
        jsonify(
            {
                "status": "success",
                "data": entries,
                "meta": {
                    "total": len(entries),
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200,
    )


@routing_matrix_bp.route("/<int:entry_id>", methods=["GET"])
@require_auth
async def get_entry(entry_id: int) -> tuple:
    """Get a single routing matrix entry by ID."""
    row = await asyncio.to_thread(lambda: db(db.routing_matrix.id == entry_id).select().first())
    if not row:
        return jsonify({"status": "error", "error": "Routing matrix entry not found"}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_matrix_bp.route("/", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def create_or_upsert_entry() -> tuple:
    """Create or upsert a routing matrix entry.

    Upserts by (tool_type, complexity, region) composite key.
    """
    data: Optional[Dict[str, Any]] = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    required_fields = ["tool_type", "complexity", "region", "model_name"]
    for field in required_fields:
        if field not in data:
            return jsonify({"status": "error", "error": f"{field} is required"}), 400

    tool_type: str = data["tool_type"]
    complexity: str = data["complexity"]
    region: str = data["region"]

    # Input validation
    if len(tool_type) > 50:
        return jsonify({"status": "error", "error": "tool_type must be <= 50 characters"}), 400
    if len(complexity) > 10:
        return jsonify({"status": "error", "error": "complexity must be <= 10 characters"}), 400
    if len(region) > 5:
        return jsonify({"status": "error", "error": "region must be <= 5 characters"}), 400

    update_fields: Dict[str, Any] = {
        "model_name": data["model_name"],
        "model_params": data.get("model_params"),
        "vram_gb": data.get("vram_gb"),
        "capability_score": data.get("capability_score"),
        "enabled": data.get("enabled", True),
        "credential_label": data.get("credential_label"),
    }

    def _upsert():
        # Check for existing entry (upsert logic)
        existing = (
            db(
                (db.routing_matrix.tool_type == tool_type)
                & (db.routing_matrix.complexity == complexity)
                & (db.routing_matrix.region == region)
            )
            .select()
            .first()
        )

        if existing:
            db(db.routing_matrix.id == existing.id).update(**update_fields)
            db.commit()
            updated_row = db(db.routing_matrix.id == existing.id).select().first()
            return "updated", updated_row

        new_entry_id: int = db.routing_matrix.insert(
            tool_type=tool_type,
            complexity=complexity,
            region=region,
            **update_fields,
            created_at=datetime.utcnow(),
        )
        db.commit()

        new_row = db(db.routing_matrix.id == new_entry_id).select().first()
        return "created", new_row

    action, row = await asyncio.to_thread(_upsert)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {
                    "action": action,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200 if action == "updated" else 201,
    )


@routing_matrix_bp.route("/<int:entry_id>", methods=["PUT"])
@require_auth
@require_role("admin", "resource_manager")
async def update_entry(entry_id: int) -> tuple:
    """Update an existing routing matrix entry by ID."""
    data: Optional[Dict[str, Any]] = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    allowed_fields = [
        "tool_type",
        "complexity",
        "region",
        "model_name",
        "model_params",
        "vram_gb",
        "capability_score",
        "enabled",
        "credential_label",
    ]
    update_fields: Dict[str, Any] = {}
    for field in allowed_fields:
        if field in data:
            update_fields[field] = data[field]

    def _update():
        row = db(db.routing_matrix.id == entry_id).select().first()
        if not row:
            return "not_found", None

        if not update_fields:
            return "no_fields", None

        # Validate unique constraint if changing composite key fields
        key_fields_changed = any(f in update_fields for f in ("tool_type", "complexity", "region"))
        if key_fields_changed:
            new_tool_type: str = update_fields.get("tool_type", row.tool_type)
            new_complexity: str = update_fields.get("complexity", row.complexity)
            new_region: str = update_fields.get("region", row.region)
            conflict = (
                db(
                    (db.routing_matrix.tool_type == new_tool_type)
                    & (db.routing_matrix.complexity == new_complexity)
                    & (db.routing_matrix.region == new_region)
                    & (db.routing_matrix.id != entry_id)
                )
                .select()
                .first()
            )
            if conflict:
                return "conflict", None

        db(db.routing_matrix.id == entry_id).update(**update_fields)
        db.commit()

        updated_row = db(db.routing_matrix.id == entry_id).select().first()
        return "ok", updated_row

    result, row = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Routing matrix entry not found"}), 404
    if result == "no_fields":
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400
    if result == "conflict":
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Another entry with this tool_type/complexity/region combination already exists",
                }
            ),
            409,
        )

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_matrix_bp.route("/<int:entry_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "resource_manager")
async def delete_entry(entry_id: int) -> tuple:
    """Delete a routing matrix entry by ID."""

    def _delete():
        row = db(db.routing_matrix.id == entry_id).select().first()
        if not row:
            return "not_found"

        db(db.routing_matrix.id == entry_id).delete()
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_delete)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Routing matrix entry not found"}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": {"id": entry_id},
                "meta": {
                    "action": "deleted",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200,
    )


@routing_matrix_bp.route("/seed", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def seed_routing_matrix() -> tuple:
    """Populate routing matrix from default spec.

    Upserts all entries from DEFAULT_ROUTING_MATRIX. Existing entries
    matching (tool_type, complexity, region) are updated; new entries are
    created.
    """

    def _seed():
        created: int = 0
        updated: int = 0

        for entry in DEFAULT_ROUTING_MATRIX:
            existing = (
                db(
                    (db.routing_matrix.tool_type == entry["tool_type"])
                    & (db.routing_matrix.complexity == entry["complexity"])
                    & (db.routing_matrix.region == entry["region"])
                )
                .select()
                .first()
            )

            fields: Dict[str, Any] = {
                "model_name": entry["model_name"],
                "model_params": entry.get("model_params"),
                "vram_gb": entry.get("vram_gb"),
                "capability_score": entry.get("capability_score"),
                "enabled": entry.get("enabled", True),
                "credential_label": entry.get("credential_label"),
            }

            if existing:
                db(db.routing_matrix.id == existing.id).update(**fields)
                updated += 1
            else:
                db.routing_matrix.insert(
                    tool_type=entry["tool_type"],
                    complexity=entry["complexity"],
                    region=entry["region"],
                    **fields,
                    created_at=datetime.utcnow(),
                )
                created += 1

        db.commit()

        return created, updated

    created, updated = await asyncio.to_thread(_seed)

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "created": created,
                    "updated": updated,
                    "total": created + updated,
                },
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Routing LLM instructions (ported from legacy /routing-config admin page)
# ---------------------------------------------------------------------------


@routing_matrix_bp.route("/instructions", methods=["GET"])
@require_auth
async def get_routing_instructions() -> tuple:
    """Get the current routing-LLM instructions + selected model from Redis.

    Any authenticated user may read this (matches legacy: the HTML page's
    backing API required only login, not admin, for GET).
    """

    def _fetch():
        if not _ext.redis_client:
            return None, None
        try:
            return (
                _ext.redis_client.get("routing:instructions"),
                _ext.redis_client.get("routing:llm_model"),
            )
        except Exception as exc:  # pragma: no cover - defensive, Redis I/O failure
            logger.error("Failed to read routing instructions from Redis: %s", exc)
            return None, None

    instructions, routing_llm = await asyncio.to_thread(_fetch)

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "instructions": instructions or _DEFAULT_ROUTING_INSTRUCTIONS,
                    "routing_llm": routing_llm or _DEFAULT_ROUTING_LLM,
                },
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_matrix_bp.route("/instructions", methods=["POST"])
@require_auth
@require_role("admin")
async def set_routing_instructions() -> tuple:
    """Set routing-LLM instructions + model in Redis (admin only).

    Admin-only mirrors legacy's Permission.SYSTEM_CONFIG check, which only
    the admin role holds (resource_manager/reporter/user do not) -- so this
    intentionally uses @require_role("admin") rather than the
    ("admin", "resource_manager") pair used by the routing-matrix CRUD routes
    above.
    """
    data: Optional[Dict[str, Any]] = await request.get_json()
    if not data or not data.get("instructions"):
        return jsonify({"status": "error", "error": "instructions field required"}), 400

    instructions: str = data["instructions"]
    routing_llm: str = data.get("routing_llm", _DEFAULT_ROUTING_LLM)

    def _persist() -> bool:
        if not _ext.redis_client:
            return False
        try:
            _ext.redis_client.set("routing:instructions", instructions)
            _ext.redis_client.set("routing:llm_model", routing_llm)
            return True
        except Exception as exc:  # pragma: no cover - defensive, Redis I/O failure
            logger.error("Failed to persist routing instructions to Redis: %s", exc)
            return False

    persisted = await asyncio.to_thread(_persist)

    if not persisted:
        return jsonify({"status": "error", "error": "Routing instructions store (Redis) unavailable"}), 503

    logger.info("Updated routing instructions (length=%d, llm=%s)", len(instructions), routing_llm)

    return (
        jsonify(
            {
                "status": "success",
                "data": {"instructions_length": len(instructions), "routing_llm": routing_llm},
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_matrix_bp.route("/test", methods=["POST"])
@require_auth
@require_role("admin")
async def test_routing_decision() -> tuple:
    """Test a routing decision for a sample prompt (admin only).

    Ported as-is from the legacy management plane: legacy's own
    implementation was already a static illustrative response ("For now,
    return a mock response") rather than a live call into the routing LLM,
    so this preserves identical (mocked) fidelity for WebUI parity. Wiring
    this to a real routing-LLM call is a separate follow-up, not part of
    this admin-surface parity task.
    """
    data: Optional[Dict[str, Any]] = await request.get_json()
    prompt: str = (data or {}).get("prompt", "")
    if not prompt:
        return jsonify({"status": "error", "error": "prompt field required"}), 400

    result = {
        "prompt": prompt,
        "routing_decision": "claude-3-sonnet",
        "routing_reasoning": "Programming task detected - routing to Claude Sonnet for code generation",
        "request_type": "programming",
        "confidence": 0.85,
        "alternative_models": ["gpt-4", "llama-70b"],
    }

    return (
        jsonify(
            {
                "status": "success",
                "data": result,
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
