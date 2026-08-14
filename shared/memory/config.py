"""Per-key §6A.5 ``proxy_memory`` config resolution, AND-gated with the feature flag.

Resolution is fail-safe OFF at every step: flag missing/off, config missing,
or the features client raising all collapse to :data:`ALL_DISABLED`. No
layer may read a per-key config value without going through
:func:`resolve_proxy_memory_config` first.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PROXY_MEMORY_FLAG = "waddleai.proxy_memory"

DEFAULT_THRESHOLD_TOKENS = 8000
DEFAULT_KEEP_RECENT = 4
DEFAULT_RATIO = 0.3


@dataclass(slots=True)
class ProxyMemoryConfig:
    """Resolved, flag-gated proxy-memory configuration for one request."""

    scratchpad_enabled: bool
    scratchpad_substitution: bool
    summarization_enabled: bool
    threshold_tokens: int
    keep_recent: int
    ratio: float
    embedding_cache: bool
    schema_dedup: bool


ALL_DISABLED = ProxyMemoryConfig(
    scratchpad_enabled=False,
    scratchpad_substitution=False,
    summarization_enabled=False,
    threshold_tokens=DEFAULT_THRESHOLD_TOKENS,
    keep_recent=DEFAULT_KEEP_RECENT,
    ratio=DEFAULT_RATIO,
    embedding_cache=False,
    schema_dedup=False,
)


def _parse_block(block: dict | None) -> ProxyMemoryConfig:
    """Parse the ``api_keys.proxy_memory`` JSON block into a ProxyMemoryConfig.

    Documented defaults (applied whenever the block, or a given key within
    it, is absent): scratchpad tools on, plain-client substitution off
    (opt-in — it is an ambient, header-driven behavior with a bigger blast
    radius than an explicit MCP tool call), summarization off (opt-in),
    embedding cache and schema dedup on. All defaults apply only once the
    whole-feature flag is already on -- see resolve_proxy_memory_config.
    """
    block = block or {}
    summarization = block.get("summarization") or {}

    return ProxyMemoryConfig(
        scratchpad_enabled=bool(block.get("scratchpad", True)),
        scratchpad_substitution=bool(block.get("scratchpad_substitution", False)),
        summarization_enabled=bool(summarization.get("enabled", False)),
        threshold_tokens=int(summarization.get("threshold_tokens", DEFAULT_THRESHOLD_TOKENS)),
        keep_recent=int(summarization.get("keep_recent", DEFAULT_KEEP_RECENT)),
        ratio=float(summarization.get("ratio", DEFAULT_RATIO)),
        embedding_cache=bool(block.get("embedding_cache", True)),
        schema_dedup=bool(block.get("schema_dedup", True)),
    )


async def _load_proxy_memory_block(db: Any, api_key_id: int | None) -> dict | None:
    """Fetch the raw ``proxy_memory`` JSON block for an API key. None on any lookup failure."""
    if db is None or api_key_id is None:
        return None
    try:
        row = await db.get_api_key_proxy_memory(api_key_id)
    except AttributeError:
        # Minimal/stubbed db in tests -- treat as "no config" rather than erroring.
        return None
    except Exception as exc:  # fail-safe: config lookup failure never crashes the request
        logger.warning("proxy_memory config lookup failed for api_key_id=%s: %s", api_key_id, exc)
        return None
    return row


async def resolve_proxy_memory_config(
    db: Any,
    features: Any,
    api_key_id: int | None,
    org_id: int | None,
) -> ProxyMemoryConfig:
    """Resolve the effective proxy-memory config for a request.

    AND-gated with ``features.is_feature_enabled("waddleai.proxy_memory", ...)``
    -- the whole-feature flag is the outer gate; per-key config only narrows
    from there. Fail-safe OFF: any exception from the features client, or
    the flag being off/missing, returns ALL_DISABLED regardless of what the
    per-key config says.
    """
    try:
        flag_on = features.is_feature_enabled(
            PROXY_MEMORY_FLAG, distinct_id=str(org_id) if org_id else "server"
        )
    except Exception as exc:
        logger.warning("proxy_memory feature flag evaluation failed, defaulting OFF: %s", exc)
        return ALL_DISABLED

    if not flag_on:
        return ALL_DISABLED

    block = await _load_proxy_memory_block(db, api_key_id)
    return _parse_block(block)


def build_config_resolver(db: Any, features: Any) -> Callable[[Any], Awaitable[ProxyMemoryConfig]]:
    """Bind (db, features) once and return a `user_context -> ProxyMemoryConfig` resolver.

    Shared by the MCP scratchpad tools (shared/utils/mcp_interface.py) and
    the memory pipeline stages (proxy/apps/proxy_server/pipeline/memory_stages.py)
    so both call sites resolve the exact same config the exact same way.
    """

    async def _resolve(user_context: Any) -> ProxyMemoryConfig:
        org_id = getattr(user_context, "organization_id", None) or getattr(
            user_context, "tenant_id", None
        )
        api_key_id = getattr(user_context, "api_key_id", None)
        return await resolve_proxy_memory_config(db, features, api_key_id, org_id)

    return _resolve
