"""Tests for ``penguincode_cli.auth.scope`` -- ScopeContext + claims mapping.

TDD: written before ``penguincode_cli/auth/scope.py`` exists; must fail with
an ImportError/ModuleNotFoundError until the module is implemented.
"""

import pytest

from penguincode_cli.auth.scope import ScopeContext, ScopeValidationError, scope_from_claims


class TestScopeContextShape:
    """ScopeContext must match Shared Contracts exactly: frozen, slotted."""

    def test_is_frozen(self) -> None:
        ctx = ScopeContext(
            tenant_id="t1", org_id=None, team_ids=(), user_id="u1", scopes=()
        )
        with pytest.raises(AttributeError):
            ctx.tenant_id = "other"  # type: ignore[misc]

    def test_has_slots_no_dict(self) -> None:
        ctx = ScopeContext(
            tenant_id="t1", org_id=None, team_ids=(), user_id="u1", scopes=()
        )
        assert not hasattr(ctx, "__dict__")

    def test_field_names_and_defaults(self) -> None:
        ctx = ScopeContext(
            tenant_id="tenant-a",
            org_id="org-b",
            team_ids=("team-1", "team-2"),
            user_id="user-c",
            scopes=("widgets:read", "widgets:write"),
        )
        assert ctx.tenant_id == "tenant-a"
        assert ctx.org_id == "org-b"
        assert ctx.team_ids == ("team-1", "team-2")
        assert ctx.user_id == "user-c"
        assert ctx.scopes == ("widgets:read", "widgets:write")


class TestScopeFromClaims:
    """scope_from_claims maps validated JWT claims -> ScopeContext."""

    def test_valid_claims_full(self) -> None:
        claims = {
            "sub": "user-123",
            "tenant": "tenant-abc",
            "org": "org-xyz",
            "teams": ["team-1", "team-2"],
            "scope": ["widgets:read", "widgets:write"],
        }
        ctx = scope_from_claims(claims)
        assert ctx == ScopeContext(
            tenant_id="tenant-abc",
            org_id="org-xyz",
            team_ids=("team-1", "team-2"),
            user_id="user-123",
            scopes=("widgets:read", "widgets:write"),
        )

    def test_teams_and_scope_are_tuples(self) -> None:
        claims = {
            "sub": "user-123",
            "tenant": "tenant-abc",
            "teams": ["team-1"],
            "scope": ["widgets:read"],
        }
        ctx = scope_from_claims(claims)
        assert isinstance(ctx.team_ids, tuple)
        assert isinstance(ctx.scopes, tuple)

    def test_teams_as_space_delimited_string(self) -> None:
        """A space-delimited teams claim (non-standard, but tolerated) is also accepted."""
        claims = {
            "sub": "user-123",
            "tenant": "tenant-abc",
            "teams": "team-1 team-2",
            "scope": [],
        }
        ctx = scope_from_claims(claims)
        assert ctx.team_ids == ("team-1", "team-2")

    def test_scope_as_space_delimited_string(self) -> None:
        """OAuth2-standard space-delimited scope string is also accepted."""
        claims = {
            "sub": "user-123",
            "tenant": "tenant-abc",
            "teams": [],
            "scope": "widgets:read widgets:write",
        }
        ctx = scope_from_claims(claims)
        assert ctx.scopes == ("widgets:read", "widgets:write")

    def test_missing_org_defaults_none(self) -> None:
        claims = {"sub": "user-123", "tenant": "tenant-abc", "teams": [], "scope": []}
        ctx = scope_from_claims(claims)
        assert ctx.org_id is None

    def test_missing_teams_and_scope_default_empty(self) -> None:
        claims = {"sub": "user-123", "tenant": "tenant-abc"}
        ctx = scope_from_claims(claims)
        assert ctx.team_ids == ()
        assert ctx.scopes == ()

    def test_missing_tenant_raises(self) -> None:
        claims = {"sub": "user-123", "teams": [], "scope": []}
        with pytest.raises(ScopeValidationError):
            scope_from_claims(claims)

    def test_blank_tenant_raises(self) -> None:
        claims = {"sub": "user-123", "tenant": "", "teams": [], "scope": []}
        with pytest.raises(ScopeValidationError):
            scope_from_claims(claims)

    def test_missing_sub_raises(self) -> None:
        claims = {"tenant": "tenant-abc", "teams": [], "scope": []}
        with pytest.raises(ScopeValidationError):
            scope_from_claims(claims)
