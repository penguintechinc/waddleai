"""Model-assignments resolver -- the admin's steering wheel (spec §7.1.1).

Resolves a tool type to a default model plus an optional escalation model,
honoring global -> org scope precedence, including the pre-declared
internal-function rows (security-audit, routing-classifier, embeddings,
docs-fetch, summarize). Valkey-cached, invalidated on Management writes.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:route:assign"
_DEFAULT_CACHE_TTL = 300


@dataclass(slots=True)
class Assignment:
    """A resolved tool-type -> model assignment."""

    tool_type: str
    default_model: str
    escalation_model: str | None = None
    fallback_models: list[str] = field(default_factory=list)


def _cache_key(org_id: int | None, tool_type: str) -> str:
    """Build the Valkey cache key for a (org, tool_type) assignment lookup."""
    return f"{_CACHE_PREFIX}:{org_id if org_id is not None else 'global'}:{tool_type}"


def _to_json(assignment: Assignment) -> str:
    """Serialize an Assignment for Valkey storage."""
    return json.dumps(
        {
            "tool_type": assignment.tool_type,
            "default_model": assignment.default_model,
            "escalation_model": assignment.escalation_model,
            "fallback_models": assignment.fallback_models,
        }
    )


def _from_json(raw: str) -> Assignment:
    """Deserialize an Assignment from its Valkey-cached JSON form."""
    data = json.loads(raw)
    return Assignment(
        tool_type=data["tool_type"],
        default_model=data["default_model"],
        escalation_model=data.get("escalation_model"),
        fallback_models=data.get("fallback_models") or [],
    )


class AssignmentResolver:
    """Resolves model_assignments rows with global->org precedence and caching."""

    def __init__(self, db: Any, valkey: Any = None, cache_ttl: int = _DEFAULT_CACHE_TTL) -> None:
        """Initialize the resolver.

        Args:
            db: penguin-dal DB instance exposing a ``model_assignments`` table.
            valkey: Optional redis.asyncio-compatible client for caching.
            cache_ttl: Cache entry TTL in seconds.

        """
        self.db = db
        self.valkey = valkey
        self.cache_ttl = cache_ttl

    async def resolve(self, tool_type: str, org_id: int | None = None) -> Assignment | None:
        """Resolve a tool type to its Assignment, org row overriding global.

        Args:
            tool_type: The tool type to resolve (e.g. "code-gen", "security-audit").
            org_id: The requesting organization's id, or None for global-only lookup.

        Returns:
            The resolved Assignment, or None when no assignment row exists
            (capability matching alone decides in that case, per §7.1).

        """
        cache_key = _cache_key(org_id, tool_type)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        assignment = await asyncio.to_thread(self._fetch, tool_type, org_id)

        await self._cache_set(cache_key, assignment)
        return assignment

    def _fetch(self, tool_type: str, org_id: int | None) -> Assignment | None:
        """Synchronous penguin-dal lookup: org row first, else global row."""
        table = self.db.model_assignments

        if org_id is not None:
            org_row = (
                self.db(
                    (table.tool_type == tool_type)
                    & (table.scope == "org")
                    & (table.scope_ref == org_id)
                    & (table.enabled == True)  # noqa: E712 -- PyDAL query operator, not a truthy check
                )
                .select()
                .first()
            )
            if org_row is not None:
                return self._row_to_assignment(org_row)

        global_row = (
            self.db(
                (table.tool_type == tool_type) & (table.scope == "global") & (table.enabled == True)  # noqa: E712
            )
            .select()
            .first()
        )
        if global_row is None:
            return None
        return self._row_to_assignment(global_row)

    @staticmethod
    def _row_to_assignment(row: Any) -> Assignment:
        """Convert a penguin-dal model_assignments row into an Assignment."""
        return Assignment(
            tool_type=row.tool_type,
            default_model=row.model_name,
            escalation_model=getattr(row, "escalation_model", None),
            fallback_models=list(getattr(row, "fallback_models", None) or []),
        )

    async def invalidate(self, org_id: int | None, tool_type: str | None = None) -> None:
        """Invalidate cached assignment(s) for an org, called on Management writes.

        Args:
            org_id: The organization whose cache entries should be cleared.
            tool_type: When given, only that tool type's entry is cleared;
                otherwise both the org-scoped and global-scoped keys for it
                are left to natural TTL expiry (a targeted tool_type should
                normally be supplied by callers that know what changed).

        """
        if self.valkey is None:
            return
        keys = []
        if tool_type is not None:
            keys.append(_cache_key(org_id, tool_type))
            keys.append(_cache_key(None, tool_type))
        if not keys:
            return
        try:
            await self.valkey.delete(*keys)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("AssignmentResolver: cache invalidation failed: %s", exc)

    async def _cache_get(self, key: str) -> Assignment | None:
        """Read-through cache lookup; any Valkey failure is treated as a miss."""
        if self.valkey is None:
            return None
        try:
            raw = await self.valkey.get(key)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("AssignmentResolver: cache read failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return _from_json(raw)
        except (ValueError, KeyError):
            return None

    async def _cache_set(self, key: str, assignment: Assignment | None) -> None:
        """Write-through cache store; None results are not cached (avoid masking new rows)."""
        if self.valkey is None or assignment is None:
            return
        try:
            await self.valkey.set(key, _to_json(assignment), ex=self.cache_ttl)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("AssignmentResolver: cache write failed: %s", exc)
