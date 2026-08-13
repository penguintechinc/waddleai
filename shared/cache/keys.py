"""Determinism-eligibility matrix and SHA-256 exact-key derivation (spec §6.1).

The exact cache only ever replays byte-identical responses for genuinely
deterministic requests: ``temperature == 0`` and no message in the
conversation already carries a tool-call result. A ``tools`` *schema* on the
request is fine -- and part of the key -- because it does not by itself make
a request non-reproducible; a previously-executed tool *result* already in
the message history does, because a re-run could get a different result.

The cache sits before routing (spec §3.2), so ``model_class`` is the
client-requested ``model`` string as received -- deterministic per request.
When routing (§7) lands, its resolved route can be threaded through the same
``ExactKeyParts.model_class`` field without changing this module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import orjson

# Roles/blocks that indicate a tool call has already been *executed* and its
# result is part of the conversation -- not just that a `tools` schema was
# offered to the model.
_TOOL_RESULT_ROLES = {"tool"}
_TOOL_RESULT_BLOCK_TYPES = {"tool_use", "tool_result"}


@dataclass(slots=True)
class ExactKeyParts:
    """Canonical inputs to the exact-cache key (spec §6.1).

    org_id is included in the hash *and* the Valkey key namespace
    (shared.cache.exact.ExactCache) as defense in depth against
    cross-org cache poisoning/leakage.
    """

    org_id: int
    model_class: str
    messages: list
    tools: list | None = None
    temperature: float = 0.0
    top_p: float | None = None
    max_tokens: int | None = None


def _message_has_tool_result(message: dict) -> bool:
    """True if a single message carries an already-executed tool-call result."""
    if not isinstance(message, dict):
        return False
    if message.get("role") in _TOOL_RESULT_ROLES:
        return True
    if message.get("tool_calls"):
        return True
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in _TOOL_RESULT_BLOCK_TYPES:
                return True
    return False


def is_exact_eligible(body: dict) -> bool:
    """Determinism-eligibility matrix for the exact cache (spec §6.1/§6.5).

    Eligible iff ``temperature`` is explicitly ``0``/``0.0`` and no message
    in ``body["messages"]`` carries an already-executed tool-call result. A
    ``tools`` schema with no results yet, and the ``stream`` flag, never
    affect eligibility.
    """
    temperature = body.get("temperature")
    if temperature is None:
        return False
    try:
        temperature_value = float(temperature)
    except (TypeError, ValueError):
        return False
    if temperature_value != 0.0:
        return False

    for message in body.get("messages") or []:
        if _message_has_tool_result(message):
            return False

    return True


def _canonical_payload(parts: ExactKeyParts) -> dict[str, Any]:
    return {
        "org_id": parts.org_id,
        "model_class": parts.model_class,
        "messages": parts.messages,
        "tools": parts.tools,
        "temperature": parts.temperature,
        "top_p": parts.top_p,
        "max_tokens": parts.max_tokens,
    }


def derive_exact_key(parts: ExactKeyParts) -> str:
    """SHA-256 hex digest over the canonical request shape (spec §6.1).

    Canonicalized via ``orjson`` with ``OPT_SORT_KEYS`` so dict key order
    (and whitespace, which orjson never emits) never affects the digest;
    list/array order (message order) is preserved as semantically
    meaningful and is *not* sorted.
    """
    canonical_bytes = orjson.dumps(_canonical_payload(parts), option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical_bytes).hexdigest()
