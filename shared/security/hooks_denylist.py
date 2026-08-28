"""Agent-hooks Tier-1 canonical denylist (§18.1 platform spec).

The Tier-1 denylist protects paths where a wrong edit is unrecoverable or a
credential leak: `.env*`, `.git/**`, key/cert files, SSH/AWS credential
directories, and lockfiles. Adapters enforce this list **offline** (zero
network cost per tool call) and **fail closed** -- if they cannot reach
WaddleAI, they keep enforcing their last-synced copy rather than allowing
everything through. This module owns the canonical list server-side
(`BUILTIN_DENYLIST_PATTERNS`) and merges it with admin-added entries so
`GET /api/v1/hooks/policy` and the server's own defense-in-depth check in
`HooksPolicyEngine.evaluate()` (§18.2) both resolve the same set.

Builtin patterns are a hardcoded constant, not database rows -- deliberately.
An admin (global or tenant) can only ever *add* denylist entries via
`hook_denylist_entries`; there is no DB row to delete or update that would
remove a builtin protection, so "an admin rule cannot weaken the Tier-1
denylist" (coordinator directive, §18.4) holds structurally, not by a
runtime special case. See `hooks_engine.HooksPolicyEngine.evaluate` for why
this check also runs unconditionally *before* any admin `hook_rules` are
even loaded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:hookdenylist:"
_CACHE_TTL_SECONDS = 300

# Seed set (coordinator directive): paths where a wrong edit is unrecoverable
# (lockfiles -- hand-editing corrupts reproducible builds/supply-chain
# integrity) or a credential leak (env files, git internals, keys, SSH/AWS
# dirs). Matched with glob "contains" semantics against the flattened
# tool_input text (see `flatten_tool_input_text`), so `.git/**` matches a
# command that touches `.git/hooks/pre-commit` anywhere in its arguments.
BUILTIN_DENYLIST_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    ".git/**",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "~/.ssh/**",
    "~/.aws/**",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "go.sum",
    "Gemfile.lock",
    "*.lock",
)


@dataclass(slots=True, frozen=True)
class DenylistEntry:
    """One denylist pattern -- either the hardcoded builtin set or an admin-added row."""

    pattern: str
    source: str  # "builtin" | "admin"
    scope_type: str | None = None  # None for builtin; "global" | "org" for admin
    scope_ref: str | None = None
    reason: str | None = None
    id: int | None = None  # DB id for admin entries; None for builtin (nothing to delete)


@lru_cache(maxsize=2048)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a shell-glob-like pattern (`*`, `?`, `**`) into a compiled regex.

    There are no path segments in the flattened matchable text this runs
    against (see `flatten_tool_input_text`), so `**` and `*` both translate
    to `.*` -- the recursive-vs-single-segment distinction glob normally
    carries doesn't apply here. Callers use `re.search` (not `fullmatch`),
    so a pattern matches anywhere within a larger string (e.g. a full Bash
    command line containing a protected path as one argument).
    """
    parts: list[str] = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.IGNORECASE)


def glob_search(pattern: str, text: str) -> bool:
    """True if `pattern` (shell-glob-like) matches anywhere within `text`."""
    if not pattern or not text:
        return False
    return _compile_glob(pattern).search(text) is not None


def flatten_tool_input_text(tool_input: dict[str, Any] | None) -> str:
    """Flatten a `tool_input` payload into one matchable string.

    Ecosystems disagree on field names (`file_path` vs `path` vs `command`
    vs `cmd`) -- rather than special-casing every adapter's tool schema,
    this walks every string leaf value in the payload and joins them, so a
    denylist/rule pattern matches whatever text is actually present
    regardless of which key it lives under. A unit-separator join avoids
    two unrelated field values accidentally concatenating into a false
    match at a boundary.
    """
    if not tool_input:
        return ""
    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)
        # numbers/bools/None contribute no matchable text

    _walk(tool_input)
    return "\x1f".join(parts)


def match_denylist(entries: list[DenylistEntry], matchable_text: str) -> DenylistEntry | None:
    """Return the first denylist entry matching `matchable_text`, or None."""
    if not matchable_text:
        return None
    for entry in entries:
        if glob_search(entry.pattern, matchable_text):
            return entry
    return None


class HookDenylistStore(Protocol):
    """Read seam `HookDenylistResolver` depends on -- implemented by penguin-dal."""

    async def fetch_entries(self, scope_type: str, scope_ref: str | None) -> list[DenylistEntry]:
        """Return enabled admin-added denylist rows for one (scope_type, scope_ref)."""
        ...


class HookDenylistResolver:
    """Resolves builtin ∪ global-admin ∪ org-admin denylist entries, Valkey-cached.

    Tenant isolation is structural: `resolve(org_id)` only ever fetches
    "org" rows scoped to *that* `org_id` -- another org's admin-added
    entries are never in the merged set, the same way `PolicyResolver`
    (§8.1) never crosses org boundaries.
    """

    def __init__(self, store: HookDenylistStore, valkey: Any = None) -> None:
        """Wire the admin-entry row source and optional Valkey cache."""
        self.store = store
        self.valkey = valkey

    async def resolve(self, org_id: int | str | None) -> list[DenylistEntry]:
        """Merged denylist for one org: builtin + global admin entries + this org's entries."""
        cache_key = f"{_CACHE_PREFIX}{org_id}"
        if self.valkey is not None:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        entries: list[DenylistEntry] = [
            DenylistEntry(pattern=p, source="builtin") for p in BUILTIN_DENYLIST_PATTERNS
        ]
        entries.extend(await self.store.fetch_entries("global", None))
        if org_id is not None:
            entries.extend(await self.store.fetch_entries("org", str(org_id)))

        if self.valkey is not None:
            await self._cache_set(cache_key, entries)
        return entries

    async def invalidate(self) -> None:
        """Drop all cached resolutions after a denylist write.

        No fine-grained per-org invalidation -- a global-scope write can
        affect every org's merged set, matching `PolicyResolver.invalidate`'s
        same "clear the whole prefix" precedent (§8.1).
        """
        if self.valkey is None:
            return
        try:
            keys = [k async for k in self.valkey.scan_iter(match=f"{_CACHE_PREFIX}*")]
            if keys:
                await self.valkey.delete(*keys)
        except Exception as e:
            logger.warning("HookDenylistResolver cache invalidation failed: %s", e)

    async def _cache_get(self, key: str) -> list[DenylistEntry] | None:
        try:
            raw = await self.valkey.get(key)
        except Exception as e:
            logger.warning("HookDenylistResolver cache read failed: %s", e)
            return None
        if raw is None:
            return None
        try:
            return [DenylistEntry(**row) for row in json.loads(raw)]
        except Exception as e:
            logger.warning("HookDenylistResolver cache deserialize failed: %s", e)
            return None

    async def _cache_set(self, key: str, entries: list[DenylistEntry]) -> None:
        from dataclasses import asdict

        try:
            payload = [asdict(e) for e in entries]
            await self.valkey.set(key, json.dumps(payload), ex=_CACHE_TTL_SECONDS)
        except Exception as e:
            logger.warning("HookDenylistResolver cache write failed: %s", e)


class PenguinDALHookDenylistStore:
    """`HookDenylistStore` backed by penguin-dal's `hook_denylist_entries` table."""

    def __init__(self, db: Any) -> None:
        """Wrap a penguin-dal/PyDAL connection exposing `db.hook_denylist_entries`."""
        self.db = db

    async def fetch_entries(self, scope_type: str, scope_ref: str | None) -> list[DenylistEntry]:
        """Query enabled entries for one scope, offloading the sync DAL call to a thread."""

        def _fetch() -> list[DenylistEntry]:
            table = self.db.hook_denylist_entries
            if scope_ref is None:
                # PyDAL Field.__eq__ builds a query object, not real Python
                # equality -- `is None` would not build an IS NULL clause.
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
                DenylistEntry(
                    id=r.id,
                    pattern=r.pattern,
                    source="admin",
                    scope_type=r.scope_type,
                    scope_ref=r.scope_ref,
                    reason=r.reason,
                )
                for r in rows
            ]

        return await asyncio.to_thread(_fetch)
