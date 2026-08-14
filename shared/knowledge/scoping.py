"""Scope / trust / isolation core (§9.7).

The model that makes memory safe for real teams: composite-scope reads
(narrower scopes override, broader scopes are shared read-only),
trust-weighted ranking, and contradiction -> quarantine -> supersede
conflict resolution. Isolation is a **security** property -- a session read
must never leak another user's session/user-scope items, even within the
same repo.

This module is pure: it computes visibility/ranking/conflict decisions but
never touches a database. Callers (the memory-config API, CodeRAG search,
the knowledge retriever) apply the decisions and perform the actual DB
reads/writes. No auto-promotion path exists in this module -- promotion from
a narrow scope to a broader one is always an explicit caller action.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ScopeType(StrEnum):
    """§9.7 scope hierarchy, narrowest first."""

    SESSION = "session"
    USER = "user"
    REPO = "repo"
    PROJECT = "project"
    ORG = "org"


class TrustTier(StrEnum):
    """§9.7 trust tiers. Retrieval ranks by relevance x trust weight."""

    VERIFIED = "verified"
    CONFIRMED = "confirmed"
    DERIVED = "derived"
    UNVERIFIED = "unverified"


# Retrieval weighting: verified > confirmed > derived > unverified.
_TRUST_WEIGHT: dict[TrustTier, float] = {
    TrustTier.VERIFIED: 1.0,
    TrustTier.CONFIRMED: 0.85,
    TrustTier.DERIVED: 0.7,
    TrustTier.UNVERIFIED: 0.5,
}

# Precedence for contradiction resolution: trust first, then confirmation
# (same trust tier, but one is a correction to an earlier item), then
# recency. Higher number wins.
_TRUST_PRECEDENCE: dict[TrustTier, int] = {
    TrustTier.VERIFIED: 4,
    TrustTier.CONFIRMED: 3,
    TrustTier.DERIVED: 2,
    TrustTier.UNVERIFIED: 1,
}


@dataclass(slots=True, frozen=True)
class ScopeKey:
    """The caller's composite scope for a read -- who is asking, from where.

    Only ``session``/``user`` are ever compared for isolation (§9.7: "two
    users in the same repo are isolated"); ``org``/``project``/``repo`` are
    the broader identifiers a caller is *inside*, used to compute which
    broader-scope items are visible.
    """

    org: str
    project: str | None = None
    repo: str | None = None
    branch: str | None = None
    user: str | None = None
    session: str | None = None


@dataclass(slots=True)
class ScopedRecord:
    """A single stored item (memory, code chunk, doc, upload) with §9.7 metadata."""

    id: str
    content: str
    scope_type: ScopeType
    scope_ref: str
    """Identifier within scope_type: org id, project id, repo id, user id, or session id."""
    trust_tier: TrustTier
    author_user_id: str | None
    org: str
    project: str | None = None
    repo: str | None = None
    branch: str | None = None
    version: int = 1
    superseded_by: str | None = None
    status: str = "active"  # active | quarantined | disputed
    expires_at: datetime | None = None
    created_at: datetime | None = None
    embedding: list[float] | None = None
    relevance: float = 0.0
    """Caller-supplied similarity score (0..1); rank() combines with trust."""


def visible_scopes(caller: ScopeKey) -> set[tuple[ScopeType, str]]:
    """The set of (scope_type, scope_ref) pairs readable by ``caller``.

    Composite-key read: the caller's own session and user scopes, plus every
    broader scope they are inside (repo, project, org) -- narrower scopes
    override on conflict, broader scopes are shared read-only. Another
    user's session/user scope is never included, even within the same repo.
    """
    scopes: set[tuple[ScopeType, str]] = {(ScopeType.ORG, caller.org)}
    if caller.project:
        scopes.add((ScopeType.PROJECT, caller.project))
    if caller.repo:
        scopes.add((ScopeType.REPO, caller.repo))
    if caller.user:
        scopes.add((ScopeType.USER, caller.user))
    if caller.session:
        scopes.add((ScopeType.SESSION, caller.session))
    return scopes


def _is_own_narrow_scope(record: ScopedRecord, caller: ScopeKey) -> bool:
    """Whether a session/user-scoped record belongs to the caller themself."""
    if record.scope_type == ScopeType.SESSION:
        return caller.session is not None and record.scope_ref == caller.session
    if record.scope_type == ScopeType.USER:
        return caller.user is not None and record.scope_ref == caller.user
    return True  # not a narrow per-identity scope


def is_visible(record: ScopedRecord, caller: ScopeKey, *, now: datetime | None = None) -> bool:
    """Whether ``caller`` may read ``record``.

    Isolation rule (security): session/user-scoped records are visible only
    to their own session/user, regardless of shared repo/org membership.
    Broader scopes (repo/project/org) are shared read-only to anyone inside
    them. Quarantined/disputed records and TTL-expired unconfirmed records
    are excluded from retrieval (still held for audit, never hard-deleted
    here -- deletion is a caller decision).
    """
    if record.status != "active":
        return False
    if record.org != caller.org:
        return False
    is_narrow_scope = record.scope_type in (ScopeType.SESSION, ScopeType.USER)
    if is_narrow_scope and not _is_own_narrow_scope(record, caller):
        return False
    is_repo_scope = record.scope_type == ScopeType.REPO
    if is_repo_scope and caller.repo is not None and record.repo != caller.repo:
        return False
    # §9.7 concurrent repo/branch work: CodeRAG chunks key on (repo, branch),
    # so a caller on feature/A must never see feature/B's in-flight chunks --
    # branch isolation is a security property, not just a filter convenience.
    if is_repo_scope and record.branch is not None and caller.branch is not None:
        if record.branch != caller.branch:
            return False
    is_project_scope = record.scope_type == ScopeType.PROJECT
    if is_project_scope and caller.project is not None and record.project != caller.project:
        return False
    if record.trust_tier == TrustTier.UNVERIFIED and record.expires_at is not None:
        check_time = now or datetime.utcnow()
        if record.expires_at <= check_time:
            return False
    return True


def filter_visible(
    records: list[ScopedRecord], caller: ScopeKey, *, now: datetime | None = None
) -> list[ScopedRecord]:
    """Filter ``records`` down to what ``caller`` may read (§9.7 isolation)."""
    return [r for r in records if is_visible(r, caller, now=now)]


def rank(records: list[ScopedRecord]) -> list[ScopedRecord]:
    """Sort ``records`` by relevance x trust weight, descending.

    Uses each record's ``relevance`` (caller-computed similarity, 0..1)
    combined with its trust tier's fixed weight. Ties break by trust tier
    (verified before unverified) to keep ordering deterministic.
    """
    return sorted(
        records,
        key=lambda r: (r.relevance * _TRUST_WEIGHT[r.trust_tier], _TRUST_PRECEDENCE[r.trust_tier]),
        reverse=True,
    )


def needs_provenance_header(record: ScopedRecord) -> bool:
    """Whether ``record`` must carry an explicit provenance header on injection.

    §9.7: unverified memory is always injected with a header naming it as a
    claim, not fact. Non-unverified records may still carry provenance (§9.6
    requires it for everything retrieved), but only unverified is mandatory
    at the scoping layer -- injection_safety.py enforces it for all tiers.
    """
    return record.trust_tier == TrustTier.UNVERIFIED


@dataclass(slots=True)
class ConflictResolution:
    """The outcome of resolving a contradiction between two records."""

    winner_id: str
    loser_id: str
    reason: str  # "trust" | "confirmation" | "recency"


def detect_contradiction(
    new_record: ScopedRecord,
    existing: list[ScopedRecord],
    *,
    similarity_fn: object = None,
    threshold: float = 0.92,
) -> ScopedRecord | None:
    """Find an existing record that semantically conflicts with ``new_record``.

    Conflict detection is cosine similarity on embeddings above ``threshold``
    combined with differing content -- i.e. "about the same fact" but not
    identical, which is the write-time signal that this is a *correction*
    rather than a duplicate. ``similarity_fn(a, b) -> float`` is injectable
    for testing; defaults to cosine similarity over ``embedding``.

    Returns the conflicting existing record, or ``None`` if no conflict is
    found (a genuinely new fact, or content-identical -- not a contradiction).
    """
    fn = similarity_fn or _cosine_similarity
    if new_record.embedding is None:
        return None
    for candidate in existing:
        if candidate.id == new_record.id or candidate.status != "active":
            continue
        if candidate.embedding is None:
            continue
        if candidate.content.strip() == new_record.content.strip():
            continue  # identical content is a duplicate, not a contradiction
        similarity = fn(new_record.embedding, candidate.embedding)
        if similarity >= threshold:
            return candidate
    return None


def resolve_conflict(new_record: ScopedRecord, existing: ScopedRecord) -> ConflictResolution:
    """Decide which of two conflicting records wins: trust -> confirmation -> recency.

    Pure decision function -- the caller applies it by setting the loser's
    ``status='quarantined'`` and ``superseded_by=winner.id`` (held for audit,
    never hard-deleted).
    """
    new_rank = _TRUST_PRECEDENCE[new_record.trust_tier]
    existing_rank = _TRUST_PRECEDENCE[existing.trust_tier]

    if new_rank != existing_rank:
        new_wins = new_rank > existing_rank
        winner, loser = (new_record, existing) if new_wins else (existing, new_record)
        return ConflictResolution(winner_id=winner.id, loser_id=loser.id, reason="trust")

    # Same trust tier: a higher version number is an explicit correction
    # (memory_correct) of the same fact -> confirmation wins.
    if new_record.version != existing.version:
        new_wins = new_record.version > existing.version
        winner, loser = (new_record, existing) if new_wins else (existing, new_record)
        return ConflictResolution(winner_id=winner.id, loser_id=loser.id, reason="confirmation")

    # Tie-break on recency.
    new_time = new_record.created_at or datetime.min
    existing_time = existing.created_at or datetime.min
    winner, loser = (new_record, existing) if new_time >= existing_time else (existing, new_record)
    return ConflictResolution(winner_id=winner.id, loser_id=loser.id, reason="recency")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Default similarity function for detect_contradiction()."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


__all__ = [
    "ScopeType",
    "TrustTier",
    "ScopeKey",
    "ScopedRecord",
    "ConflictResolution",
    "visible_scopes",
    "is_visible",
    "filter_visible",
    "rank",
    "needs_provenance_header",
    "detect_contradiction",
    "resolve_conflict",
]
