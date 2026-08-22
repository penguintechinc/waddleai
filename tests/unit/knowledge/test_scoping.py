"""Tests for shared.knowledge.scoping: §9.7 scope/trust/isolation core.

Isolation and contradiction-resolution behaviors are security properties --
see the class docstrings below for which tests specifically assert
session-memory isolation between users sharing a repo.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from shared.knowledge.scoping import (
    ConflictResolution,
    ScopedRecord,
    ScopeKey,
    ScopeType,
    TrustTier,
    detect_contradiction,
    filter_visible,
    is_visible,
    needs_provenance_header,
    rank,
    resolve_conflict,
    visible_scopes,
)


def _record(**overrides: object) -> ScopedRecord:
    """Build a ScopedRecord with sane defaults, overridden per test."""
    defaults: dict[str, object] = dict(
        id="rec-1",
        content="the build command is `make build`",
        scope_type=ScopeType.REPO,
        scope_ref="repo-42",
        trust_tier=TrustTier.DERIVED,
        author_user_id="user-1",
        org="org-a",
        repo="repo-42",
    )
    defaults.update(overrides)
    return ScopedRecord(**defaults)  # type: ignore[arg-type]


class TestSessionIsolation:
    """(a) A session read never leaks another user's session/user items -- security."""

    def test_session_scope_visible_to_own_session_only(self) -> None:
        """Alice's session note is visible to Alice, not to Bob."""
        alice = ScopeKey(org="org-a", repo="repo-42", user="alice", session="alice-session-1")
        bob = ScopeKey(org="org-a", repo="repo-42", user="bob", session="bob-session-1")

        alice_note = _record(
            id="note-1",
            scope_type=ScopeType.SESSION,
            scope_ref="alice-session-1",
            trust_tier=TrustTier.UNVERIFIED,
        )

        assert is_visible(alice_note, alice) is True
        assert is_visible(alice_note, bob) is False

    def test_user_scope_visible_to_own_user_only(self) -> None:
        """Alice's user-scope preference is visible to Alice, not to Bob."""
        alice = ScopeKey(org="org-a", repo="repo-42", user="alice")
        bob = ScopeKey(org="org-a", repo="repo-42", user="bob")

        alice_pref = _record(
            id="pref-1",
            scope_type=ScopeType.USER,
            scope_ref="alice",
            trust_tier=TrustTier.CONFIRMED,
        )

        assert is_visible(alice_pref, alice) is True
        assert is_visible(alice_pref, bob) is False

    def test_session_read_still_sees_shared_repo_and_org_items(self) -> None:
        """A session read also returns broader repo/org items -- shared, read-only."""
        alice = ScopeKey(org="org-a", repo="repo-42", user="alice", session="alice-session-1")
        repo_fact = _record(id="fact-1", scope_type=ScopeType.REPO, scope_ref="repo-42")
        org_doc = _record(
            id="doc-1", scope_type=ScopeType.ORG, scope_ref="org-a", trust_tier=TrustTier.VERIFIED
        )

        visible = filter_visible([repo_fact, org_doc], alice)

        assert {r.id for r in visible} == {"fact-1", "doc-1"}

    def test_visible_scopes_excludes_other_users_identity_scopes(self) -> None:
        """visible_scopes() never includes another user's user/session identity."""
        alice = ScopeKey(org="org-a", repo="repo-42", user="alice", session="alice-session-1")
        scopes = visible_scopes(alice)

        assert (ScopeType.USER, "alice") in scopes
        assert (ScopeType.SESSION, "alice-session-1") in scopes
        assert (ScopeType.USER, "bob") not in scopes
        assert (ScopeType.SESSION, "bob-session-1") not in scopes

    def test_org_boundary_is_a_hard_wall(self) -> None:
        """An org-scope record from a different org is never visible."""
        org_a_caller = ScopeKey(org="org-a")
        org_b_record = _record(
            id="org-b-doc", scope_type=ScopeType.ORG, scope_ref="org-b", org="org-b"
        )

        assert is_visible(org_b_record, org_a_caller) is False


class TestTrustOrderingAndRanking:
    """(b) TrustTier order verified > confirmed > derived > unverified; rank() applies it."""

    def test_rank_orders_by_relevance_times_trust(self) -> None:
        """A verified item with slightly lower relevance still outranks unverified."""
        caller_records = [
            _record(id="low-trust-high-relevance", trust_tier=TrustTier.UNVERIFIED, relevance=0.95),
            _record(
                id="high-trust-similar-relevance", trust_tier=TrustTier.VERIFIED, relevance=0.9
            ),
        ]

        ordered = rank(caller_records)

        assert ordered[0].id == "high-trust-similar-relevance"

    def test_rank_is_deterministic_on_ties(self) -> None:
        """Equal relevance*trust ties break toward higher trust tier, deterministically."""
        a = _record(id="a", trust_tier=TrustTier.VERIFIED, relevance=0.5)
        b = _record(id="b", trust_tier=TrustTier.UNVERIFIED, relevance=0.5)

        ordered = rank([b, a])

        assert [r.id for r in ordered] == ["a", "b"]


class TestUnverifiedProvenanceFlag:
    """(c) An unverified item is always flagged for provenance-header injection."""

    def test_unverified_needs_provenance_header(self) -> None:
        """Unverified records are always flagged for a provenance header."""
        record = _record(trust_tier=TrustTier.UNVERIFIED)
        assert needs_provenance_header(record) is True

    def test_verified_does_not_require_it_at_scoping_layer(self) -> None:
        """Verified isn't mandatory here (injection_safety.py headers all tiers on inject)."""
        record = _record(trust_tier=TrustTier.VERIFIED)
        assert needs_provenance_header(record) is False


class TestContradictionQuarantineSupersede:
    """(d) detect_contradiction + resolve() supersede by trust -> confirmation -> recency."""

    def test_detects_semantic_conflict_above_threshold(self) -> None:
        """Two near-identical embeddings with differing content are flagged as a conflict."""
        existing = _record(id="old", content="the API port is 8000", embedding=[1.0, 0.0, 0.0])
        new = _record(id="new", content="the API port is 9000", embedding=[0.99, 0.05, 0.0])

        conflict = detect_contradiction(new, [existing])

        assert conflict is not None
        assert conflict.id == "old"

    def test_identical_content_is_not_a_contradiction(self) -> None:
        """Byte-identical content is a duplicate, not a contradiction."""
        existing = _record(id="old", content="same fact", embedding=[1.0, 0.0])
        new = _record(id="new", content="same fact", embedding=[1.0, 0.0])

        assert detect_contradiction(new, [existing]) is None

    def test_dissimilar_content_is_not_a_contradiction(self) -> None:
        """Unrelated facts (low embedding similarity) never conflict."""
        existing = _record(id="old", content="the build command is make", embedding=[1.0, 0.0])
        new = _record(id="new", content="the test command is pytest", embedding=[0.0, 1.0])

        assert detect_contradiction(new, [existing]) is None

    def test_higher_trust_correction_supersedes_lower_trust_original(self) -> None:
        """A verified correction supersedes an unverified original by trust."""
        existing = _record(id="old", trust_tier=TrustTier.UNVERIFIED)
        new = _record(id="new", trust_tier=TrustTier.VERIFIED)

        resolution = resolve_conflict(new, existing)

        assert resolution == ConflictResolution(winner_id="new", loser_id="old", reason="trust")

    def test_same_trust_higher_version_wins_as_confirmation(self) -> None:
        """Same trust tier, higher version number wins as an explicit confirmation."""
        existing = _record(id="old", trust_tier=TrustTier.CONFIRMED, version=1)
        new = _record(id="new", trust_tier=TrustTier.CONFIRMED, version=2)

        resolution = resolve_conflict(new, existing)

        assert resolution.winner_id == "new"
        assert resolution.reason == "confirmation"

    def test_recency_tiebreak_when_trust_and_version_equal(self) -> None:
        """Same trust and version -- the more recent record wins."""
        now = datetime.utcnow()
        existing = _record(
            id="old", trust_tier=TrustTier.DERIVED, version=1, created_at=now - timedelta(hours=1)
        )
        new = _record(id="new", trust_tier=TrustTier.DERIVED, version=1, created_at=now)

        resolution = resolve_conflict(new, existing)

        assert resolution.winner_id == "new"
        assert resolution.reason == "recency"

    def test_quarantine_is_a_caller_action_not_a_deletion(self) -> None:
        """resolve_conflict() is a pure decision -- callers hold the loser, never delete it here."""
        existing = _record(id="old", trust_tier=TrustTier.UNVERIFIED)
        new = _record(id="new", trust_tier=TrustTier.VERIFIED)

        resolution = resolve_conflict(new, existing)

        assert not hasattr(resolution, "delete")
        assert resolution.loser_id == "old"


class TestNoAutoPromotion:
    """(e) Low-trust session memory can never be auto-elevated without explicit promotion."""

    def test_scoping_module_exposes_no_promotion_function(self) -> None:
        """There is no promote()/auto_promote() in this module -- promotion is a caller action."""
        import shared.knowledge.scoping as scoping_module

        assert not hasattr(scoping_module, "promote")
        assert not hasattr(scoping_module, "auto_promote")

    def test_session_item_stays_invisible_to_other_users_regardless_of_trust(self) -> None:
        """Even a VERIFIED session-scope item stays invisible to other users."""
        bob = ScopeKey(org="org-a", repo="repo-42", user="bob", session="bob-session-1")
        alice_verified_session_note = _record(
            id="note",
            scope_type=ScopeType.SESSION,
            scope_ref="alice-session-1",
            trust_tier=TrustTier.VERIFIED,
        )

        # Even a VERIFIED-trust item stays session-scoped until an explicit
        # promotion action (outside this module) changes its scope_type.
        assert is_visible(alice_verified_session_note, bob) is False


class TestExpiryDecay:
    """(f) Low-trust unconfirmed items past TTL are excluded from retrieval."""

    def test_expired_unverified_item_excluded(self) -> None:
        """An unverified item past its expires_at is excluded from retrieval."""
        caller = ScopeKey(org="org-a", repo="repo-42", user="alice", session="s1")
        expired = _record(
            id="expired",
            scope_type=ScopeType.SESSION,
            scope_ref="s1",
            trust_tier=TrustTier.UNVERIFIED,
            expires_at=datetime.utcnow() - timedelta(minutes=1),
        )

        assert is_visible(expired, caller) is False

    def test_not_yet_expired_unverified_item_included(self) -> None:
        """An unverified item still within its TTL remains visible."""
        caller = ScopeKey(org="org-a", repo="repo-42", user="alice", session="s1")
        fresh = _record(
            id="fresh",
            scope_type=ScopeType.SESSION,
            scope_ref="s1",
            trust_tier=TrustTier.UNVERIFIED,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        assert is_visible(fresh, caller) is True

    def test_verified_items_are_immune_to_ttl_expiry(self) -> None:
        """A long-past expires_at on a verified item is ignored -- TTL decay is unverified-only."""
        caller = ScopeKey(org="org-a", repo="repo-42")
        verified_but_old = _record(
            id="verified-old",
            trust_tier=TrustTier.VERIFIED,
            expires_at=datetime.utcnow() - timedelta(days=365),
        )

        assert is_visible(verified_but_old, caller) is True


class TestBranchIsolation:
    """§9.7 concurrent repo/branch work: feature/A never sees feature/B's repo-scope records."""

    def test_other_branch_repo_scope_record_excluded(self) -> None:
        """A repo-scope record on a different branch is invisible even within the same repo."""
        caller = ScopeKey(org="org-a", repo="repo-1", branch="feature/A")
        other_branch_record = _record(
            id="c", scope_type=ScopeType.REPO, scope_ref="repo-1", repo="repo-1", branch="feature/B"
        )

        assert is_visible(other_branch_record, caller) is False

    def test_same_branch_repo_scope_record_visible(self) -> None:
        """A repo-scope record on the caller's own branch remains visible."""
        caller = ScopeKey(org="org-a", repo="repo-1", branch="feature/A")
        own_branch_record = _record(
            id="c", scope_type=ScopeType.REPO, scope_ref="repo-1", repo="repo-1", branch="feature/A"
        )

        assert is_visible(own_branch_record, caller) is True

    def test_branchless_repo_record_visible_to_any_branch(self) -> None:
        """A repo-scope record with no branch set (repo-wide fact) is visible from any branch."""
        caller = ScopeKey(org="org-a", repo="repo-1", branch="feature/A")
        repo_wide_record = _record(
            id="c", scope_type=ScopeType.REPO, scope_ref="repo-1", repo="repo-1"
        )

        assert is_visible(repo_wide_record, caller) is True


class TestQuarantinedStatusExcluded:
    """Quarantined/disputed records are held for audit but excluded from retrieval."""

    def test_quarantined_record_never_visible(self) -> None:
        """A quarantined record is excluded from is_visible() regardless of scope."""
        caller = ScopeKey(org="org-a", repo="repo-42")
        quarantined = _record(id="q", status="quarantined")

        assert is_visible(quarantined, caller) is False
