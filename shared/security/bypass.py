"""Scope-based authorized bypass for researchers/red teams (§8.6).

Bypass is scope-based (`security:bypass` OIDC scope per house auth rules --
never a role-name check), grantable per user or virtual key and optionally
narrowed to specific policy scopes. Mode per grant: `shadow` (default -- all
tiers still run and log verdicts, nothing blocks/redacts) or `skip` (tiers
don't run). Every bypassed request is audit-logged with the grant identity;
bypass never disables §8.7 upstream redaction unless the grant explicitly
includes it -- threat-blocking and data-protection are separate concerns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BYPASS_SCOPE = "security:bypass"


@dataclass(slots=True, frozen=True)
class BypassGrant:
    """One `security_bypass_grants` row, as read from the store."""

    id: int
    subject_type: str  # "user" | "vkey"
    subject_ref: str
    mode: str  # "shadow" | "skip"
    scope_narrow: tuple[str, ...] = ()
    include_upstream: bool = False
    expires_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class BypassDecision:
    """Resolved bypass decision for one request."""

    active: bool = False
    mode: str = "shadow"
    scope_narrow: tuple[str, ...] = ()
    include_upstream: bool = False
    grant_id: int | None = None

    def bypasses(self, scope: str) -> bool:
        """True if this decision bypasses enforcement for the given policy scope.

        An empty `scope_narrow` means "bypass everything the grant's mode
        implies"; a non-empty list narrows the bypass to exactly those scopes
        (e.g. bypass "intent_classifier" but keep PII redaction active).
        """
        if not self.active:
            return False
        if not self.scope_narrow:
            return True
        return scope in self.scope_narrow


_INACTIVE = BypassDecision(active=False)


class BypassStore:
    """Read seam for grant lookups -- implemented by penguin-dal in production."""

    async def find_active_grant(
        self, subject_type: str, subject_ref: str, now: datetime
    ) -> BypassGrant | None:
        """Return the active (non-expired) grant for a subject, if any."""
        raise NotImplementedError


class BypassResolver:
    """Resolves whether a request is authorized to bypass enforcement."""

    def __init__(self, store: BypassStore, audit_sink: Any = None) -> None:
        """Wire the grant store and an optional audit sink (defaults to logging)."""
        self.store = store
        self.audit_sink = audit_sink

    async def resolve(self, ctx: Any) -> BypassDecision:
        """Resolve the bypass decision for one request context.

        `ctx` must expose `token_scopes: Iterable[str]` and either
        `vkey_id`/`user_id` for grant lookup. No `security:bypass` scope on
        the caller's token means the grant (if any) is ignored entirely --
        a grant existing in the DB never bypasses the scope check.
        """
        token_scopes = set(getattr(ctx, "token_scopes", None) or ())
        if BYPASS_SCOPE not in token_scopes:
            return _INACTIVE

        now = getattr(ctx, "now", None) or datetime.utcnow()
        vkey_id = getattr(ctx, "vkey_id", None)
        user_id = getattr(ctx, "user_id", None)

        grant: BypassGrant | None = None
        if vkey_id is not None:
            grant = await self.store.find_active_grant("vkey", str(vkey_id), now)
        if grant is None and user_id is not None:
            grant = await self.store.find_active_grant("user", str(user_id), now)

        if grant is None:
            return _INACTIVE
        if grant.expires_at is not None and grant.expires_at <= now:
            return _INACTIVE

        decision = BypassDecision(
            active=True,
            mode=grant.mode,
            scope_narrow=grant.scope_narrow,
            include_upstream=grant.include_upstream,
            grant_id=grant.id,
        )
        self._audit(ctx, decision)
        return decision

    def _audit(self, ctx: Any, decision: BypassDecision) -> None:
        """Log every bypassed request with the grant identity (audit trail + usage flag)."""
        subject = getattr(ctx, "vkey_id", None) or getattr(ctx, "user_id", None)
        logger.warning(
            "BypassResolver: request bypassed (grant_id=%s, mode=%s, subject=%s, "
            "scope_narrow=%s, include_upstream=%s)",
            decision.grant_id,
            decision.mode,
            subject,
            decision.scope_narrow,
            decision.include_upstream,
        )
        if self.audit_sink is not None:
            self.audit_sink.record_bypass(decision, subject)


@dataclass(slots=True)
class PenguinDALBypassStore(BypassStore):
    """`BypassStore` backed by penguin-dal's `security_bypass_grants` table."""

    db: Any = field(default=None)

    async def find_active_grant(
        self, subject_type: str, subject_ref: str, now: datetime
    ) -> BypassGrant | None:
        """Query the most recent matching grant, offloading the sync DAL call to a thread."""
        import asyncio

        def _fetch() -> BypassGrant | None:
            table = self.db.security_bypass_grants
            query = (table.subject_type == subject_type) & (table.subject_ref == subject_ref)
            rows = self.db(query).select(orderby=~table.created_at)
            for row in rows:
                if row.expires_at is not None and row.expires_at <= now:
                    continue
                return BypassGrant(
                    id=row.id,
                    subject_type=row.subject_type,
                    subject_ref=row.subject_ref,
                    mode=row.mode,
                    scope_narrow=tuple(row.scope_narrow or ()),
                    include_upstream=bool(row.include_upstream),
                    expires_at=row.expires_at,
                )
            return None

        return await asyncio.to_thread(_fetch)


def create_bypass_resolver(db: Any, audit_sink: Any = None) -> BypassResolver:
    """Factory: `BypassResolver` wired to a penguin-dal connection."""
    return BypassResolver(PenguinDALBypassStore(db=db), audit_sink)
