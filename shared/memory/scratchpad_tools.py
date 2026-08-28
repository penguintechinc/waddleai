"""Transport-agnostic scratchpad_put/get/list MCP tool handlers (§6A.1).

Handlers are dict-in/dict-out and take identity from the caller's
authenticated context (``UserContext`` + ``session_id``) -- never from tool
arguments -- so a tool call can never reach across sessions or users by
passing a different ``session_id``/``user_id`` key in ``arguments``; those
keys are simply never read. The §11 MCP v2 transport work re-exposes these
same handlers over its new transports without changing this contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from shared.memory.config import ProxyMemoryConfig
from shared.memory.scratchpad import (
    ScratchpadKeyLimitExceededError,
    ScratchpadStore,
    ScratchpadValueTooLargeError,
)

ToolHandler = Callable[[ScratchpadStore, ProxyMemoryConfig, Any, str, dict], Awaitable[dict]]


def _org_and_user(user_context: Any) -> tuple[int | None, int | None]:
    """Extract (org_id, user_id) from an authenticated UserContext.

    Supports both the generic ``tenant_id``/``id`` attribute names and the
    WaddleAI ``organization_id``/``user_id`` names, matching the pattern
    already used by the pipeline stages (proxy/apps/proxy_server/pipeline/stages.py).
    """
    org_id = getattr(user_context, "organization_id", None) or getattr(
        user_context, "tenant_id", None
    )
    user_id = getattr(user_context, "user_id", None) or getattr(user_context, "id", None)
    return org_id, user_id


async def scratchpad_put(
    store: ScratchpadStore,
    config: ProxyMemoryConfig,
    user_context: Any,
    session_id: str,
    arguments: dict,
) -> dict:
    """Store a value in the caller's session scratchpad."""
    if not config.scratchpad_enabled:
        return {
            "error": {"type": "feature_disabled", "message": "scratchpad is disabled for this key"}
        }

    key = arguments.get("key")
    value = arguments.get("value")
    if not key or value is None:
        return {"error": {"type": "invalid_arguments", "message": "key and value are required"}}

    org_id, user_id = _org_and_user(user_context)
    try:
        result = await store.put(org_id, session_id, user_id, key, value)
    except (ScratchpadValueTooLargeError, ScratchpadKeyLimitExceededError) as exc:
        return {"error": {"type": "limit_exceeded", "message": str(exc)}}

    if result.quarantined:
        return {
            "quarantined": True,
            "message": "value was quarantined by security filtering and not stored",
        }
    return {"ok": True, "key": key}


async def scratchpad_get(
    store: ScratchpadStore,
    config: ProxyMemoryConfig,
    user_context: Any,
    session_id: str,
    arguments: dict,
) -> dict:
    """Retrieve a value previously stored in the caller's session scratchpad."""
    if not config.scratchpad_enabled:
        return {
            "error": {"type": "feature_disabled", "message": "scratchpad is disabled for this key"}
        }

    key = arguments.get("key")
    if not key:
        return {"error": {"type": "invalid_arguments", "message": "key is required"}}

    org_id, user_id = _org_and_user(user_context)
    value = await store.get(org_id, session_id, user_id, key)
    if value is None:
        return {"error": {"type": "not_found", "message": f"no scratchpad value for key {key!r}"}}
    return {"key": key, "value": value}


async def scratchpad_list(
    store: ScratchpadStore,
    config: ProxyMemoryConfig,
    user_context: Any,
    session_id: str,
    arguments: dict,
) -> dict:
    """List scratchpad key metadata (never values) for the caller's session."""
    if not config.scratchpad_enabled:
        return {
            "error": {"type": "feature_disabled", "message": "scratchpad is disabled for this key"}
        }

    org_id, user_id = _org_and_user(user_context)
    infos = await store.list(org_id, session_id, user_id)
    return {
        "keys": [
            {
                "key": info.key,
                "size_bytes": info.size_bytes,
                "updated_at": info.updated_at.isoformat(),
            }
            for info in infos
        ]
    }


SCRATCHPAD_TOOL_SCHEMAS: dict[str, dict] = {
    "scratchpad_put": {
        "description": "Store a value in the session scratchpad, keyed by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    "scratchpad_get": {
        "description": "Retrieve a value previously stored in the session scratchpad.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    "scratchpad_list": {
        "description": "List scratchpad keys for the current session (metadata only, no values).",
        "inputSchema": {"type": "object", "properties": {}},
    },
}

SCRATCHPAD_TOOLS: dict[str, ToolHandler] = {
    "scratchpad_put": scratchpad_put,
    "scratchpad_get": scratchpad_get,
    "scratchpad_list": scratchpad_list,
}
