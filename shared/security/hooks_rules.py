"""Agent-hooks admin-authored declarative rules (§18.3/§18.4 platform spec).

An admin (global or tenant) pushes a custom hook as a `hook_rules` row --
never shippable/executable code. The matcher is structural (ecosystem,
event, tool name, a path/command glob against the flattened `tool_input`);
the server owns the only place logic actually runs, so a bad rule is a row
update, not a script already resident on every developer's laptop.

Precedence (coordinator directive, decided and defended in §18.4): matched
rules combine by **max severity** (`deny` > `ask` > `allow`) across BOTH
global and tenant-scoped matches -- not "more specific scope wins". This is
what makes a global `deny` unconditionally outrank a tenant `allow` (2 > 0
regardless of which scope set it) while still letting a tenant `deny`
tighten a global `allow` the other direction (2 > 0 again) -- a tenant can
restrict further than the deployment-wide floor but never loosen below it.
It deliberately mirrors `policy_engine.combine()`'s monotonic composition
(§8.5): an LLM verdict there can only raise severity, never lower it;
here, no single scope's rule can lower what another scope's rule already
set. See `combine_hook_rule_matches` for the implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from shared.security.hooks_denylist import glob_search

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:hookrules:"
_CACHE_TTL_SECONDS = 300

# Severity order for the max-severity combine below -- deliberately the
# same shape as policy_engine._SEVERITY, sized for hook decisions instead
# of content-filter actions.
HOOK_DECISION_SEVERITY: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(slots=True, frozen=True)
class HookRule:
    """One admin-authored declarative hook rule (a `hook_rules` row)."""

    id: int
    scope_type: str  # "global" | "org"
    scope_ref: str | None
    ecosystem: str | None  # None = matches any ecosystem
    event: str | None  # None = matches any event
    tool_name_pattern: str | None  # None = matches any tool; else a glob
    match_pattern: str | None  # None = matches any input; else a glob against flattened text
    decision: str  # "allow" | "deny" | "ask"
    reason: str
    priority: int = 100  # lower = higher priority; tie-break only, never changes the outcome


def matches_rule(
    rule: HookRule, ecosystem: str, event: str, tool_name: str, matchable_text: str
) -> bool:
    """True if every matcher field the rule sets agrees with this hook event.

    Every field on a rule is an AND-narrowing filter; `None`/unset fields
    match anything (a rule with all fields unset matches every event --
    intentionally permitted, e.g. an org-wide `ask` on everything).
    """
    if rule.ecosystem and rule.ecosystem != ecosystem:
        return False
    if rule.event and rule.event != event:
        return False
    if rule.tool_name_pattern and not glob_search(rule.tool_name_pattern, tool_name or ""):
        return False
    if rule.match_pattern and not glob_search(rule.match_pattern, matchable_text):
        return False
    return True


def combine_hook_rule_matches(matched: list[HookRule]) -> HookRule | None:
    """Pick the winning rule across every matched rule: max severity wins.

    Ties (same severity) are broken by lowest `priority` (admin-assigned
    ordering), then lowest `id` (deterministic, insertion order) -- purely
    to pick a single reason/rule_id to surface in the response; it never
    changes which *decision* wins, only which rule gets credited for it.
    """
    if not matched:
        return None

    def _key(r: HookRule) -> tuple[int, int, int]:
        return (HOOK_DECISION_SEVERITY[r.decision], -r.priority, -r.id)

    return max(matched, key=_key)


class HookRulesStore(Protocol):
    """Read seam `HookRulesResolver` depends on -- implemented by penguin-dal."""

    async def fetch_rules(self, scope_type: str, scope_ref: str | None) -> list[HookRule]:
        """Return enabled rules for one (scope_type, scope_ref)."""
        ...


class HookRulesResolver:
    """Resolves global ∪ org-scoped enabled hook_rules for one org, Valkey-cached.

    Tenant isolation is structural, same as `HookDenylistResolver`:
    `resolve(org_id)` only ever fetches "org" rows for *that* `org_id`.
    """

    def __init__(self, store: HookRulesStore, valkey: Any = None) -> None:
        """Wire the rule row source and optional Valkey cache."""
        self.store = store
        self.valkey = valkey

    async def resolve(self, org_id: int | str | None) -> list[HookRule]:
        """Enabled rules visible to one org: global rules + this org's own rules."""
        cache_key = f"{_CACHE_PREFIX}{org_id}"
        if self.valkey is not None:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        rules: list[HookRule] = list(await self.store.fetch_rules("global", None))
        if org_id is not None:
            rules.extend(await self.store.fetch_rules("org", str(org_id)))

        if self.valkey is not None:
            await self._cache_set(cache_key, rules)
        return rules

    async def invalidate(self) -> None:
        """Drop all cached resolutions after a hook_rules write.

        No fine-grained per-org invalidation -- a global-scope write can
        affect every org's resolved set (same `PolicyResolver.invalidate`
        precedent, §8.1).
        """
        if self.valkey is None:
            return
        try:
            keys = [k async for k in self.valkey.scan_iter(match=f"{_CACHE_PREFIX}*")]
            if keys:
                await self.valkey.delete(*keys)
        except Exception as e:
            logger.warning("HookRulesResolver cache invalidation failed: %s", e)

    async def _cache_get(self, key: str) -> list[HookRule] | None:
        try:
            raw = await self.valkey.get(key)
        except Exception as e:
            logger.warning("HookRulesResolver cache read failed: %s", e)
            return None
        if raw is None:
            return None
        try:
            return [HookRule(**row) for row in json.loads(raw)]
        except Exception as e:
            logger.warning("HookRulesResolver cache deserialize failed: %s", e)
            return None

    async def _cache_set(self, key: str, rules: list[HookRule]) -> None:
        from dataclasses import asdict

        try:
            payload = [asdict(r) for r in rules]
            await self.valkey.set(key, json.dumps(payload), ex=_CACHE_TTL_SECONDS)
        except Exception as e:
            logger.warning("HookRulesResolver cache write failed: %s", e)


class PenguinDALHookRulesStore:
    """`HookRulesStore` backed by penguin-dal's `hook_rules` table."""

    def __init__(self, db: Any) -> None:
        """Wrap a penguin-dal/PyDAL connection exposing `db.hook_rules`."""
        self.db = db

    async def fetch_rules(self, scope_type: str, scope_ref: str | None) -> list[HookRule]:
        """Query enabled rules for one scope, offloading the sync DAL call to a thread."""

        def _fetch() -> list[HookRule]:
            table = self.db.hook_rules
            if scope_ref is None:
                query = (
                    (table.scope_type == scope_type)
                    & (table.scope_ref == None)  # noqa: E711
                    & (table.enabled == True)  # noqa: E712
                )
            else:
                query = (
                    (table.scope_type == scope_type)
                    & (table.scope_ref == scope_ref)
                    & (table.enabled == True)  # noqa: E712
                )
            rows = self.db(query).select()
            return [
                HookRule(
                    id=r.id,
                    scope_type=r.scope_type,
                    scope_ref=r.scope_ref,
                    ecosystem=r.ecosystem,
                    event=r.event,
                    tool_name_pattern=r.tool_name_pattern,
                    match_pattern=r.match_pattern,
                    decision=r.decision,
                    reason=r.reason,
                    priority=r.priority,
                )
                for r in rows
            ]

        return await asyncio.to_thread(_fetch)
