"""cache_configs resolution at key > org > global precedence (spec §6.4).

Each scope tuple (``('global', None)``, ``('org', str(org_id))``,
``('key', str(vkey_id))``) is cached independently in Valkey under
``waddleai:cache:cfg:{scope_type}:{scope_ref}`` with a short TTL, including
a negative cache entry (``null``) when no row exists for that scope --
so a resolve() call that misses every scope on a cold cache does exactly
one combined DB read (one ``select()`` covering all applicable scopes) and
every subsequent resolve() for the same (org_id, vkey_id) is Valkey-only
until the TTL lapses or ``invalidate()`` is called for a specific scope.

Field-level merge: a row missing a field (stored as SQL NULL) falls through
to the next-broader scope rather than resetting it to the hard default, so
a key-scoped row can override just e.g. ``semantic_enabled`` without having
to restate every other field.

penguin-dal only (see backend-database.md) -- no runtime SQLAlchemy queries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import orjson

logger = logging.getLogger(__name__)

_DEFAULT_SCOPE_TTL_SECONDS = 30

_FIELDS: tuple[str, ...] = (
    "exact_enabled",
    "semantic_enabled",
    "semantic_threshold",
    "ttl_seconds",
    "max_entry_kb",
    "anthropic_cache_control",
)


@dataclass(slots=True)
class ResolvedCacheConfig:
    """The effective cache configuration for a given (org, virtual key) pair."""

    exact_enabled: bool = True
    semantic_enabled: bool = False
    semantic_threshold: float = 0.95
    ttl_seconds: int = 86400
    max_entry_kb: int = 256
    anthropic_cache_control: bool = True


_HARD_DEFAULTS = ResolvedCacheConfig()


def scope_cache_key(scope_type: str, scope_ref: str | None) -> str:
    """Valkey key for one (scope_type, scope_ref) cache_configs row or its negative-cache entry."""
    return f"waddleai:cache:cfg:{scope_type}:{scope_ref if scope_ref is not None else '-'}"


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "exact_enabled": row.exact_enabled,
        "semantic_enabled": row.semantic_enabled,
        "semantic_threshold": row.semantic_threshold,
        "ttl_seconds": row.ttl_seconds,
        "max_entry_kb": row.max_entry_kb,
        "anthropic_cache_control": row.anthropic_cache_control,
    }


class CacheConfigResolver:
    """Resolves ``cache_configs`` at key > org > global precedence via a Valkey hot path."""

    def __init__(
        self, db: Any, valkey: Any, scope_ttl_seconds: int = _DEFAULT_SCOPE_TTL_SECONDS
    ) -> None:
        """Initialize with a penguin-dal ``db`` handle and an async Valkey client."""
        self.db = db
        self.valkey = valkey
        self.scope_ttl_seconds = scope_ttl_seconds

    async def resolve(self, org_id: int, vkey_id: int | None = None) -> ResolvedCacheConfig:
        """Resolve the effective config for ``org_id`` (and optionally ``vkey_id``)."""
        scopes: list[tuple[str, str | None]] = [("global", None), ("org", str(org_id))]
        if vkey_id is not None:
            scopes.append(("key", str(vkey_id)))

        rows_by_scope: dict[tuple[str, str | None], dict | None] = {}
        missing: list[tuple[str, str | None]] = []

        for scope_type, scope_ref in scopes:
            cached = await self.valkey.get(scope_cache_key(scope_type, scope_ref))
            if cached is None:
                missing.append((scope_type, scope_ref))
            else:
                rows_by_scope[(scope_type, scope_ref)] = orjson.loads(cached)

        if missing:
            fetched_rows = await asyncio.to_thread(self._fetch_rows, org_id, vkey_id)
            fetched_by_scope = {(row["scope_type"], row["scope_ref"]): row for row in fetched_rows}
            for scope_type, scope_ref in missing:
                row = fetched_by_scope.get((scope_type, scope_ref))
                rows_by_scope[(scope_type, scope_ref)] = row
                await self.valkey.set(
                    scope_cache_key(scope_type, scope_ref),
                    orjson.dumps(row),
                    ex=self.scope_ttl_seconds,
                )

        return self._merge([rows_by_scope.get(s) for s in scopes])

    def _fetch_rows(self, org_id: int, vkey_id: int | None) -> list[dict[str, Any]]:
        """Single combined query covering every scope this resolution needs."""
        table = self.db.cache_configs
        query = table.scope_type == "global"
        query |= (table.scope_type == "org") & (table.scope_ref == str(org_id))
        if vkey_id is not None:
            query |= (table.scope_type == "key") & (table.scope_ref == str(vkey_id))
        rows = self.db(query).select()
        return [_row_to_dict(r) for r in rows]

    @staticmethod
    def _merge(rows: list[dict | None]) -> ResolvedCacheConfig:
        """Field-level merge: broader scopes first, narrower scopes override per-field."""
        merged = {field_name: getattr(_HARD_DEFAULTS, field_name) for field_name in _FIELDS}
        for row in rows:
            if row is None:
                continue
            for field_name in _FIELDS:
                value = row.get(field_name)
                if value is not None:
                    merged[field_name] = value
        return ResolvedCacheConfig(**merged)

    async def invalidate(self, scope_type: str, scope_ref: str | None) -> None:
        """Bust the Valkey-cached entry for one scope; next resolve() re-reads the DB."""
        await self.valkey.delete(scope_cache_key(scope_type, scope_ref))
