"""Scoped security-policy resolution chain (§8.1).

Resolves the global -> org -> model -> tool/function chain into a single
merged `ResolvedPolicy`. Precedence is per-field, not whole-row: a field left
NULL at a more-specific scope inherits the next-more-general scope's value
(see migration 011's docstring for the schema rationale). Results are
Valkey-cached and invalidated on policy write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:secpol:"
_CACHE_TTL_SECONDS = 300

# Hardcoded floor used only when literally no policy rows exist yet (e.g.
# fresh install before migration 011's seeded 'global' row has landed, or a
# disconnected policy store). Mirrors migration 011's `_GLOBAL_DEFAULTS`.
_HARDCODED_FLOOR: dict[str, Any] = {
    "tier1_enabled": True,
    "tier2_enabled": True,
    "tier3_enabled": True,
    "tier4_enabled": True,
    "tier4_model": None,
    "intent_classifier_enabled": False,
    "intent_categories": (),
    "block_action": "redact",
    "fail_mode": "degrade",
    "on_unclassifiable": "reject",
    "auditor_timeout_ms": 5000,
    "latency_budget_ms": None,
    "sample_rate": 100,
    "upstream_filters": None,
}

# Fields resolved per-field (everything except structural scope/direction
# columns, which are not part of a ResolvedPolicy).
_MERGE_FIELDS = tuple(_HARDCODED_FLOOR.keys())


@dataclass(slots=True, frozen=True)
class ResolvedPolicy:
    """Fully-merged policy for one (org, model, tool, direction) resolution."""

    tier1_enabled: bool = True
    tier2_enabled: bool = True
    tier3_enabled: bool = True
    tier4_enabled: bool = True
    tier4_model: str | None = None
    intent_classifier_enabled: bool = False
    intent_categories: tuple[str, ...] = ()
    block_action: str = "redact"
    fail_mode: str = "degrade"
    on_unclassifiable: str = "reject"
    auditor_timeout_ms: int = 5000
    latency_budget_ms: int | None = None
    sample_rate: int = 100
    upstream_filters: dict[str, Any] | None = None


@dataclass(slots=True)
class _CandidateRow:
    """One security_policies row as read from the policy store."""

    scope_type: str
    scope_ref: str | None
    direction: str
    fields: dict[str, Any] = field(default_factory=dict)


class PolicyStore(Protocol):
    """Read/write seam PolicyResolver depends on -- implemented by penguin-dal."""

    async def fetch_scope_rows(self, scope_type: str, scope_ref: str | None) -> list[_CandidateRow]:
        """Return the (usually 0 or 1 per direction) rows for one scope."""
        ...


class PolicyResolver:
    """Resolves the global->org->model->tool chain into a `ResolvedPolicy`.

    `store` is any object exposing `fetch_scope_rows` (see `PolicyStore`);
    `PenguinDALPolicyStore` below is the production implementation.
    `valkey` is a `redis.asyncio`-compatible client (get/set/delete) used to
    cache resolutions; both are optional so unit tests can run store-only.
    """

    def __init__(self, store: PolicyStore, valkey: Any = None) -> None:
        """Wire the policy row source and optional Valkey cache."""
        self.store = store
        self.valkey = valkey

    async def resolve(
        self,
        org_id: int | str | None,
        model: str | None,
        tool_name: str | None,
        direction: str = "both",
    ) -> ResolvedPolicy:
        """Resolve the merged policy for (org_id, model, tool_name, direction).

        Tool scope keys on the literal `tool_name` first, falling back to a
        namespaced-MCP prefix match (`elder.search` -> `elder.*`) when no
        exact row exists.
        """
        cache_key = self._cache_key(org_id, model, tool_name, direction)
        if self.valkey is not None:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        merged: dict[str, Any] = dict(_HARDCODED_FLOOR)

        for scope_type, scope_ref in self._scope_chain(org_id, model, tool_name):
            row = await self._best_row(scope_type, scope_ref, direction)
            if row is None:
                continue
            for f in _MERGE_FIELDS:
                value = row.fields.get(f)
                if value is not None:
                    merged[f] = value

        resolved = ResolvedPolicy(
            tier1_enabled=merged["tier1_enabled"],
            tier2_enabled=merged["tier2_enabled"],
            tier3_enabled=merged["tier3_enabled"],
            tier4_enabled=merged["tier4_enabled"],
            tier4_model=merged["tier4_model"],
            intent_classifier_enabled=merged["intent_classifier_enabled"],
            intent_categories=tuple(merged["intent_categories"] or ()),
            block_action=merged["block_action"],
            fail_mode=merged["fail_mode"],
            on_unclassifiable=merged["on_unclassifiable"],
            auditor_timeout_ms=merged["auditor_timeout_ms"],
            latency_budget_ms=merged["latency_budget_ms"],
            sample_rate=merged["sample_rate"],
            upstream_filters=merged["upstream_filters"],
        )

        if self.valkey is not None:
            await self._cache_set(cache_key, resolved)

        return resolved

    async def invalidate(
        self,
        scope_type: str | None = None,
        scope_ref: str | None = None,
    ) -> None:
        """Drop cached resolutions after a policy write.

        No fine-grained per-key invalidation (a write at any scope can
        change resolutions for many (org, model, tool) tuples down the
        chain) -- callers pass the written scope for logging only; the
        cache prefix is cleared wholesale.
        """
        if self.valkey is None:
            return
        try:
            keys = []
            async for k in self.valkey.scan_iter(match=f"{_CACHE_PREFIX}*"):
                keys.append(k)
            if keys:
                await self.valkey.delete(*keys)
            logger.debug(
                "PolicyResolver cache invalidated (%d keys) after write to %s/%s",
                len(keys),
                scope_type,
                scope_ref,
            )
        except Exception as e:
            logger.warning("PolicyResolver cache invalidation failed: %s", e)

    async def _best_row(
        self, scope_type: str, scope_ref: str | None, direction: str
    ) -> _CandidateRow | None:
        """Most-specific-direction row for one scope (exact direction beats 'both')."""
        rows = await self.store.fetch_scope_rows(scope_type, scope_ref)
        exact = next((r for r in rows if r.direction == direction), None)
        if exact is not None:
            return exact
        return next((r for r in rows if r.direction == "both"), None)

    def _scope_chain(
        self, org_id: int | str | None, model: str | None, tool_name: str | None
    ) -> list[tuple[str, str | None]]:
        """Ordered (most-general -> most-specific) list of (scope_type, scope_ref)."""
        chain: list[tuple[str, str | None]] = [("global", None)]
        if org_id is not None:
            chain.append(("org", str(org_id)))
        if model:
            chain.append(("model", model))
        if tool_name:
            chain.append(("tool", tool_name))
            mcp_prefix = self._mcp_namespace(tool_name)
            if mcp_prefix:
                # Namespaced MCP fallback (elder.search -> elder.*) is
                # slightly less specific than an exact tool-name row, so it
                # is inserted just before the exact-name entry, not after.
                chain.insert(-1, ("tool", mcp_prefix))
        return chain

    @staticmethod
    def _mcp_namespace(tool_name: str) -> str | None:
        """`elder.search` -> `elder.*`; plain tool names have no namespace."""
        if "." not in tool_name:
            return None
        prefix = tool_name.split(".", 1)[0]
        return f"{prefix}.*"

    @staticmethod
    def _cache_key(
        org_id: int | str | None, model: str | None, tool_name: str | None, direction: str
    ) -> str:
        return f"{_CACHE_PREFIX}{org_id}:{model}:{tool_name}:{direction}"

    async def _cache_get(self, key: str) -> ResolvedPolicy | None:
        try:
            raw = await self.valkey.get(key)
        except Exception as e:
            logger.warning("PolicyResolver cache read failed: %s", e)
            return None
        if raw is None:
            return None
        import json

        try:
            data = json.loads(raw)
            data["intent_categories"] = tuple(data.get("intent_categories") or ())
            return ResolvedPolicy(**data)
        except Exception as e:
            logger.warning("PolicyResolver cache deserialize failed: %s", e)
            return None

    async def _cache_set(self, key: str, resolved: ResolvedPolicy) -> None:
        import json
        from dataclasses import asdict

        try:
            payload = asdict(resolved)
            payload["intent_categories"] = list(payload["intent_categories"])
            await self.valkey.set(key, json.dumps(payload), ex=_CACHE_TTL_SECONDS)
        except Exception as e:
            logger.warning("PolicyResolver cache write failed: %s", e)


class PenguinDALPolicyStore:
    """`PolicyStore` backed by penguin-dal's `security_policies` table."""

    def __init__(self, db: Any) -> None:
        """Wrap a penguin-dal/PyDAL connection exposing `db.security_policies`."""
        self.db = db

    async def fetch_scope_rows(self, scope_type: str, scope_ref: str | None) -> list[_CandidateRow]:
        """Query rows for one scope, offloading the sync DAL call to a thread."""
        import asyncio

        def _fetch() -> list[_CandidateRow]:
            table = self.db.security_policies
            if scope_ref is None:
                # PyDAL Field.__eq__ is operator-overloaded to build a query
                # object, not real Python equality -- `is None` would not
                # build an IS NULL clause here.
                query = (table.scope_type == scope_type) & (
                    table.scope_ref == None  # noqa: E711
                )
            else:
                query = (table.scope_type == scope_type) & (table.scope_ref == scope_ref)
            rows = self.db(query).select()
            return [
                _CandidateRow(
                    scope_type=r.scope_type,
                    scope_ref=r.scope_ref,
                    direction=r.direction,
                    fields={f: getattr(r, f, None) for f in _MERGE_FIELDS},
                )
                for r in rows
            ]

        return await asyncio.to_thread(_fetch)


def create_policy_resolver(db: Any, valkey: Any = None) -> PolicyResolver:
    """Factory: `PolicyResolver` wired to a penguin-dal connection + Valkey cache."""
    return PolicyResolver(PenguinDALPolicyStore(db), valkey)
