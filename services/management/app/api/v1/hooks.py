"""WaddleAI Management API v1 - Agent Hooks (§18): adapter-facing contract.

Implements the fixed wire contract `POST /api/v1/hooks/evaluate`, `POST
/api/v1/hooks/telemetry`, `GET /api/v1/hooks/policy` that the Claude Code/
Cortex, Antigravity/AGY CLI, and VS Code adapters are coded against
verbatim (§18.2). Responses here are the flat shapes the contract
specifies, **not** the `{"status","data","meta"}` envelope used elsewhere
in this API -- deviating would break interop with adapters built against
this exact shape. Admin CRUD for the underlying `hook_rules`/
`hook_denylist_entries`/`hook_configs` tables (which DOES use the house
envelope) lives in `hook_rules.py`, attached to this same blueprint.

Every route is org-scoped from the caller's own JWT/API-key identity
(`g.user["organization_id"]`), never a client-supplied org -- the adapters
stay dumb (send the normalized event, honor the returned decision); all
authored logic and org resolution live server-side.

Feature-flagged (`waddleai.agent_hooks`, default OFF): when off,
`/evaluate` always returns `allow` with no enforcement (flag-off proof --
zero behavior change for an adapter, since Tier 1 is enforced client-side
regardless of server flag state).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from quart import Blueprint, current_app, g, jsonify, request

from shared.security.content_filter import ContentFilter
from shared.security.hooks_config import HookConfigResolver, PenguinDALHookConfigStore
from shared.security.hooks_denylist import HookDenylistResolver, PenguinDALHookDenylistStore
from shared.security.hooks_engine import HooksPolicyEngine
from shared.security.hooks_rules import HookRulesResolver, PenguinDALHookRulesStore
from shared.security.policy_engine import SecurityPolicyEngine
from shared.security.policy_resolver import create_policy_resolver
from shared.utils.feature_flags import is_feature_enabled
from shared.utils.metrics import get_management_metrics

from ... import extensions as _ext
from ...extensions import db
from .auth import require_auth

logger = logging.getLogger(__name__)

hooks_bp = Blueprint("hooks", __name__, url_prefix="/api/v1/hooks")

HOOKS_FEATURE_FLAG = "waddleai.agent_hooks"

HOOK_ECOSYSTEMS = ("claude-code", "cortex", "antigravity", "vscode")
HOOK_EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "notification")


def _get_redis() -> Any:
    return getattr(_ext, "redis_client", None)


def _get_content_filter() -> ContentFilter:
    """Build a `ContentFilter` against the current db (cheap enough to build per call).

    Not `lru_cache`d: unlike the OIDC provider (auth.py), this closes over
    the module-level `db` symbol that route tests patch per-module (see
    `tests/unit/management/conftest.py` `ROUTE_MODULES`) -- caching across
    calls would risk pinning a stale `db` reference from an earlier test.
    """
    return ContentFilter(
        db=db,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        auditor_model=os.getenv("SECURITY_AUDITOR_MODEL", "shieldgemma:2b"),
    )


def _get_engine() -> HooksPolicyEngine:
    """Build a `HooksPolicyEngine` wired to the current db/Valkey connections."""
    valkey = _get_redis()
    return HooksPolicyEngine(
        denylist_resolver=HookDenylistResolver(PenguinDALHookDenylistStore(db), valkey),
        rules_resolver=HookRulesResolver(PenguinDALHookRulesStore(db), valkey),
        config_resolver=HookConfigResolver(PenguinDALHookConfigStore(db), valkey),
        security_policy_resolver=create_policy_resolver(db, valkey),
        security_policy_engine=SecurityPolicyEngine(_get_content_filter()),
        metrics=get_management_metrics(),
    )


def _parse_occurred_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@hooks_bp.route("/evaluate", methods=["POST"])
@require_auth
async def evaluate_hook() -> tuple:
    """POST /api/v1/hooks/evaluate -- the fixed adapter contract (§18.2).

    Request/response shapes are fixed by the platform spec §18.2 -- do not
    add/rename/wrap fields without updating the spec and every adapter.
    """
    org_id = g.user.get("organization_id")

    if not is_feature_enabled(HOOKS_FEATURE_FLAG, distinct_id=str(org_id or "server")):
        return (
            jsonify(
                {"decision": "allow", "reason": "agent hooks disabled", "rule_id": None,
                 "evaluated_in_ms": 0}
            ),
            200,
        )

    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    hook_version = str(data.get("hook_version", ""))
    ecosystem = str(data.get("ecosystem", ""))
    event = str(data.get("event", ""))
    tool_name = str(data.get("tool_name", ""))
    tool_input = data.get("tool_input") or {}

    if hook_version != "1":
        return jsonify({"error": "unsupported hook_version"}), 400
    if ecosystem not in HOOK_ECOSYSTEMS:
        return jsonify({"error": f"ecosystem must be one of {HOOK_ECOSYSTEMS}"}), 400
    if event not in HOOK_EVENTS:
        return jsonify({"error": f"event must be one of {HOOK_EVENTS}"}), 400
    if not tool_name:
        return jsonify({"error": "tool_name is required"}), 400
    if not isinstance(tool_input, dict):
        return jsonify({"error": "tool_input must be an object"}), 400

    metrics = get_management_metrics()
    engine = _get_engine()

    start = time.monotonic()
    result = await engine.evaluate(ecosystem, event, tool_name, tool_input, org_id)
    elapsed_s = time.monotonic() - start
    evaluated_in_ms = int(elapsed_s * 1000)

    metrics.record_hook_invocation(ecosystem, event, result.decision)
    metrics.observe_hook_evaluation_duration(ecosystem, event, elapsed_s)
    metrics.record_hook_tool_call(ecosystem, tool_name, str(org_id))

    return (
        jsonify(
            {
                "decision": result.decision,
                "reason": result.reason,
                "rule_id": result.rule_id,
                "evaluated_in_ms": evaluated_in_ms,
            }
        ),
        200,
    )


async def _persist_telemetry(
    ecosystem: str,
    event: str,
    tool_name: str,
    session_id: str,
    tool_input: dict[str, Any],
    org_id: Any,
    occurred_at_raw: str | None,
) -> None:
    """Background persistence for one telemetry event -- never raises past this point.

    `tool_input_hash` is always computed; `tool_input_raw` is populated only
    when the org's resolved `hook_configs.capture_raw_payloads` is True
    (§18.5 privacy constraint) -- default OFF, so the common case never
    persists a raw command line or file path.
    """
    try:
        config_resolver = HookConfigResolver(PenguinDALHookConfigStore(db), _get_redis())
        config = await config_resolver.resolve(org_id)
        payload_json = json.dumps(tool_input, sort_keys=True, default=str)
        tool_input_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        raw_payload = tool_input if config.capture_raw_payloads else None

        def _insert() -> None:
            db.hook_telemetry_events.insert(
                organization_id=org_id,
                ecosystem=ecosystem,
                event=event,
                tool_name=tool_name,
                session_id=session_id,
                tool_input_hash=tool_input_hash,
                tool_input_raw=raw_payload,
                occurred_at=_parse_occurred_at(occurred_at_raw),
                received_at=datetime.utcnow(),
            )
            db.commit()

        await asyncio.to_thread(_insert)
    except Exception as e:
        logger.warning("hook telemetry persistence failed (%s/%s): %s", ecosystem, event, e)


@hooks_bp.route("/telemetry", methods=["POST"])
@require_auth
async def hook_telemetry() -> tuple:
    """POST /api/v1/hooks/telemetry -- fire-and-forget, must never block the agent.

    Persistence is scheduled as a background task (`current_app.
    add_background_task`) rather than awaited inline, so the response never
    waits on a DB round trip; persistence failures are logged, never
    surfaced to the caller.
    """
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    ecosystem = str(data.get("ecosystem", ""))
    event = str(data.get("event", ""))
    tool_name = str(data.get("tool_name", ""))
    session_id = str(data.get("session_id", ""))
    tool_input = data.get("tool_input") or {}

    if ecosystem not in HOOK_ECOSYSTEMS or event not in HOOK_EVENTS:
        return jsonify({"error": "invalid ecosystem/event"}), 400
    if not isinstance(tool_input, dict):
        return jsonify({"error": "tool_input must be an object"}), 400

    org_id = g.user.get("organization_id")

    current_app.add_background_task(
        _persist_telemetry,
        ecosystem,
        event,
        tool_name,
        session_id,
        tool_input,
        org_id,
        data.get("occurred_at"),
    )

    get_management_metrics().record_hook_invocation(ecosystem, event, "telemetry")

    return jsonify({"accepted": True}), 202


@hooks_bp.route("/policy", methods=["GET"])
@require_auth
async def get_hook_policy() -> tuple:
    """GET /api/v1/hooks/policy -- canonical Tier-1 denylist for adapters to sync.

    Adapters enforce this list **offline** (§18.1) and fail closed if they
    cannot reach WaddleAI -- this endpoint is what they periodically poll to
    refresh their local copy. Intentionally flat/minimal: an adapter's
    enforcement loop just needs the pattern list.
    """
    org_id = g.user.get("organization_id")
    resolver = HookDenylistResolver(PenguinDALHookDenylistStore(db), _get_redis())
    entries = await resolver.resolve(org_id)

    return (
        jsonify(
            {
                "denylist_patterns": [e.pattern for e in entries],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
        ),
        200,
    )
