"""Tests for BypassResolver: scope gating, shadow/skip, narrowing, expiry, upstream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from shared.security.bypass import BYPASS_SCOPE, BypassGrant, BypassResolver, BypassStore


class StubBypassStore(BypassStore):
    """In-memory `BypassStore` -- one grant slot per (subject_type, subject_ref)."""

    def __init__(self) -> None:
        """Start with no grants configured."""
        self.grants: dict[tuple[str, str], BypassGrant] = {}

    def add(self, subject_type: str, subject_ref: str, grant: BypassGrant) -> None:
        """Register a grant for a subject."""
        self.grants[(subject_type, subject_ref)] = grant

    async def find_active_grant(
        self, subject_type: str, subject_ref: str, now: datetime
    ) -> BypassGrant | None:
        """Return the configured grant if present and not expired."""
        grant = self.grants.get((subject_type, subject_ref))
        if grant is None:
            return None
        if grant.expires_at is not None and grant.expires_at <= now:
            return None
        return grant


@dataclass(slots=True)
class _Ctx:
    """Minimal request-context stand-in BypassResolver.resolve() consumes."""

    token_scopes: tuple[str, ...] = field(default_factory=tuple)
    user_id: int | None = None
    vkey_id: str | None = None
    now: datetime | None = None


class RecordingAuditSink:
    """Captures every bypass audit record for assertion."""

    def __init__(self) -> None:
        """Start with an empty record list."""
        self.records: list[tuple[Any, Any]] = []

    def record_bypass(self, decision: Any, subject: Any) -> None:
        """Append one (decision, subject) audit record."""
        self.records.append((decision, subject))


@pytest.fixture
def store() -> StubBypassStore:
    """A fresh, empty stub bypass store."""
    return StubBypassStore()


@pytest.fixture
def audit() -> RecordingAuditSink:
    """A fresh recording audit sink."""
    return RecordingAuditSink()


@pytest.fixture
def resolver(store: StubBypassStore, audit: RecordingAuditSink) -> BypassResolver:
    """A BypassResolver wired to the stub store and recording audit sink."""
    return BypassResolver(store, audit_sink=audit)


class TestScopeGating:
    """(a): no security:bypass scope -> grant ignored, normal enforcement."""

    @pytest.mark.asyncio
    async def test_grant_present_but_scope_missing_is_ignored(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """A grant exists, but the token lacks security:bypass -- it is never honored."""
        grant = BypassGrant(id=1, subject_type="user", subject_ref="42", mode="skip")
        store.add("user", "42", grant)
        ctx = _Ctx(token_scopes=(), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.active is False


class TestModes:
    """(b)-(c): shadow logs-but-passes, skip audited."""

    @pytest.mark.asyncio
    async def test_shadow_mode_is_active_but_shadow(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """A shadow grant resolves active with mode='shadow' (caller still runs+logs verdicts)."""
        store.add(
            "user", "42", BypassGrant(id=2, subject_type="user", subject_ref="42", mode="shadow")
        )
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.active is True
        assert decision.mode == "shadow"

    @pytest.mark.asyncio
    async def test_skip_mode_is_audited(
        self, resolver: BypassResolver, store: StubBypassStore, audit: RecordingAuditSink
    ) -> None:
        """A skip grant resolves active with mode='skip' and is audit-logged."""
        grant = BypassGrant(id=3, subject_type="vkey", subject_ref="wa-123", mode="skip")
        store.add("vkey", "wa-123", grant)
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), vkey_id="wa-123")

        decision = await resolver.resolve(ctx)

        assert decision.active is True
        assert decision.mode == "skip"
        assert len(audit.records) == 1
        assert audit.records[0][0].grant_id == 3


class TestScopeNarrowing:
    """(d): scope-narrowed grant only bypasses the named scopes."""

    @pytest.mark.asyncio
    async def test_narrowed_grant_bypasses_only_named_scope(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """A grant narrowed to intent_classifier does not bypass PII redaction."""
        store.add(
            "user",
            "42",
            BypassGrant(
                id=4,
                subject_type="user",
                subject_ref="42",
                mode="skip",
                scope_narrow=("intent_classifier",),
            ),
        )
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.bypasses("intent_classifier") is True
        assert decision.bypasses("tier1_pii") is False


class TestExpiry:
    """(e): expired grant is not honored."""

    @pytest.mark.asyncio
    async def test_expired_grant_is_ignored(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """A grant whose expires_at is in the past resolves inactive."""
        past = datetime.utcnow() - timedelta(hours=1)
        store.add(
            "user",
            "42",
            BypassGrant(id=5, subject_type="user", subject_ref="42", mode="skip", expires_at=past),
        )
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.active is False


class TestUpstreamSeparation:
    """(f): include_upstream=false keeps §8.7 upstream redaction active."""

    @pytest.mark.asyncio
    async def test_default_grant_does_not_bypass_upstream(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """include_upstream defaults False -- upstream redaction is untouched."""
        grant = BypassGrant(id=6, subject_type="user", subject_ref="42", mode="skip")
        store.add("user", "42", grant)
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.include_upstream is False

    @pytest.mark.asyncio
    async def test_include_upstream_true_bypasses_upstream_too(
        self, resolver: BypassResolver, store: StubBypassStore
    ) -> None:
        """A grant with include_upstream=True also bypasses upstream redaction."""
        store.add(
            "user",
            "42",
            BypassGrant(
                id=7, subject_type="user", subject_ref="42", mode="skip", include_upstream=True
            ),
        )
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        decision = await resolver.resolve(ctx)

        assert decision.include_upstream is True


class TestAuditFlag:
    """(g): every bypass sets the usage.waddleai bypass flag (via the audit sink)."""

    @pytest.mark.asyncio
    async def test_every_active_bypass_is_recorded(
        self, resolver: BypassResolver, store: StubBypassStore, audit: RecordingAuditSink
    ) -> None:
        """Every active decision is handed to the audit sink for usage.waddleai flagging."""
        grant = BypassGrant(id=8, subject_type="user", subject_ref="42", mode="shadow")
        store.add("user", "42", grant)
        ctx = _Ctx(token_scopes=(BYPASS_SCOPE,), user_id=42)

        await resolver.resolve(ctx)

        assert len(audit.records) == 1
