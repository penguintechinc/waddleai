"""Agent-hooks per-scope config: Tier-2 opt-in + telemetry privacy (§18.2/§18.5).

Resolves two independent admin choices through the same global->org
per-field-merge chain `PolicyResolver` uses for §8 (nullable columns =
"inherit from the next-more-general scope", non-NULL overrides):

- Tier-2 remote policy evaluation is **opt-in** (`remote_eval_enabled`,
  default False) with its own tight timeout (`remote_eval_timeout_ms`,
  default 200ms -- independent of the resolved *security policy's* own
  `auditor_timeout_ms`, which is tuned for the proxy's chat-completion
  latency budget, not the interactive tool-call path hooks sit on) and its
  own explicit fail-mode choice (`remote_eval_fail_mode`, default "open" --
  see `hooks_engine` module docstring for the full defense of this default).
- Telemetry payload capture is **opt-in** (`capture_raw_payloads`, default
  False): `tool_input` is command lines and absolute file paths -- sensitive,
  often PII-adjacent -- so it is hashed by default and never persisted raw
  unless an org has explicitly turned this on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:hookconfig:"
_CACHE_TTL_SECONDS = 300

# Resolution floor -- used when no config row exists at any scope yet.
_HARDCODED_FLOOR: dict[str, Any] = {
    "remote_eval_enabled": False,
    "remote_eval_timeout_ms": 200,
    # "open" (fail available) vs "closed" (fail safe) -- see hooks_engine
    # module docstring for the full rationale. This is a per-org override
    # point precisely so a regulated-environment org can flip it to
    # "closed" without a code change.
    "remote_eval_fail_mode": "open",
    "capture_raw_payloads": False,
}
_MERGE_FIELDS = tuple(_HARDCODED_FLOOR.keys())


@dataclass(slots=True, frozen=True)
class HookConfig:
    """Fully-merged hook config for one org (or the global floor when org_id is None)."""

    remote_eval_enabled: bool = False
    remote_eval_timeout_ms: int = 200
    remote_eval_fail_mode: str = "open"
    capture_raw_payloads: bool = False


class HookConfigStore(Protocol):
    """Read seam `HookConfigResolver` depends on -- implemented by penguin-dal."""

    async def fetch_config(self, scope_type: str, scope_ref: str | None) -> dict[str, Any] | None:
        """Return the configurable-field dict for one scope, or None if no row exists."""
        ...


class HookConfigResolver:
    """Resolves global->org merged `HookConfig`, Valkey-cached."""

    def __init__(self, store: HookConfigStore, valkey: Any = None) -> None:
        """Wire the config row source and optional Valkey cache."""
        self.store = store
        self.valkey = valkey

    async def resolve(self, org_id: int | str | None) -> HookConfig:
        """Merge the global row, then this org's row (per-field, non-NULL overrides)."""
        cache_key = f"{_CACHE_PREFIX}{org_id}"
        if self.valkey is not None:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        merged: dict[str, Any] = dict(_HARDCODED_FLOOR)
        for scope_type, scope_ref in self._scope_chain(org_id):
            fields = await self.store.fetch_config(scope_type, scope_ref)
            if not fields:
                continue
            for f in _MERGE_FIELDS:
                value = fields.get(f)
                if value is not None:
                    merged[f] = value

        resolved = HookConfig(**merged)
        if self.valkey is not None:
            await self._cache_set(cache_key, resolved)
        return resolved

    async def invalidate(self) -> None:
        """Drop all cached resolutions after a hook_configs write."""
        if self.valkey is None:
            return
        try:
            keys = [k async for k in self.valkey.scan_iter(match=f"{_CACHE_PREFIX}*")]
            if keys:
                await self.valkey.delete(*keys)
        except Exception as e:
            logger.warning("HookConfigResolver cache invalidation failed: %s", e)

    @staticmethod
    def _scope_chain(org_id: int | str | None) -> list[tuple[str, str | None]]:
        chain: list[tuple[str, str | None]] = [("global", None)]
        if org_id is not None:
            chain.append(("org", str(org_id)))
        return chain

    async def _cache_get(self, key: str) -> HookConfig | None:
        try:
            raw = await self.valkey.get(key)
        except Exception as e:
            logger.warning("HookConfigResolver cache read failed: %s", e)
            return None
        if raw is None:
            return None
        try:
            return HookConfig(**json.loads(raw))
        except Exception as e:
            logger.warning("HookConfigResolver cache deserialize failed: %s", e)
            return None

    async def _cache_set(self, key: str, resolved: HookConfig) -> None:
        from dataclasses import asdict

        try:
            await self.valkey.set(key, json.dumps(asdict(resolved)), ex=_CACHE_TTL_SECONDS)
        except Exception as e:
            logger.warning("HookConfigResolver cache write failed: %s", e)


class PenguinDALHookConfigStore:
    """`HookConfigStore` backed by penguin-dal's `hook_configs` table."""

    def __init__(self, db: Any) -> None:
        """Wrap a penguin-dal/PyDAL connection exposing `db.hook_configs`."""
        self.db = db

    async def fetch_config(self, scope_type: str, scope_ref: str | None) -> dict[str, Any] | None:
        """Query the one config row for a scope, offloading the sync DAL call to a thread."""

        def _fetch() -> dict[str, Any] | None:
            table = self.db.hook_configs
            if scope_ref is None:
                query = (table.scope_type == scope_type) & (table.scope_ref == None)  # noqa: E711
            else:
                query = (table.scope_type == scope_type) & (table.scope_ref == scope_ref)
            row = self.db(query).select().first()
            if row is None:
                return None
            return {f: getattr(row, f, None) for f in _MERGE_FIELDS}

        return await asyncio.to_thread(_fetch)
