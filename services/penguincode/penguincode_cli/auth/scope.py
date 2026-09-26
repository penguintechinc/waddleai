"""ScopeContext: penguincode's per-request tenant/org/team/user/scope handle.

Derived once per request from a validated WaddleAI JWT (see
``auth.middleware``) and threaded to every store (vector/graph) so reads and
writes are scope-filtered at a single chokepoint -- never per callsite. The
dataclass shape is fixed by the platform plan's Shared Contracts; every
other penguincode-knowledge-platform task consumes this exact signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ScopeValidationError(ValueError):
    """Raised when validated JWT claims cannot yield a valid ScopeContext.

    Covers the tenant hard-boundary (missing/blank ``tenant`` claim) and a
    missing ``sub`` (no ScopeContext can exist without a user identity).
    """


@dataclass(slots=True, frozen=True)
class ScopeContext:
    """Immutable request scope: tenant (hard boundary), org, teams, user, scopes.

    Field names/types are exact per the platform plan's Shared Contracts --
    do not rename or reorder without updating every consuming task (T6/T8/T10
    stores, T14 retrieval).
    """

    tenant_id: str
    org_id: str | None
    team_ids: tuple[str, ...]
    user_id: str
    scopes: tuple[str, ...]


def scope_from_claims(claims: dict[str, Any]) -> ScopeContext:
    """Map validated JWT claims (``tenant``, ``org``, ``teams``, ``sub``, ``scope``) to a ScopeContext.

    *claims* must already be signature/exp/iss/aud-validated (see
    ``auth.middleware.WaddleAIJWTValidator.validate``) -- this function only
    performs the claims-to-dataclass mapping and enforces the tenant/user
    hard boundary. Raises ``ScopeValidationError`` if ``tenant`` or ``sub``
    is missing or blank; never guesses or defaults them.
    """
    tenant_id = claims.get("tenant")
    if not tenant_id:
        raise ScopeValidationError("JWT missing required 'tenant' claim (hard boundary)")

    user_id = claims.get("sub")
    if not user_id:
        raise ScopeValidationError("JWT missing required 'sub' claim")

    org_id = claims.get("org") or None

    raw_teams = claims.get("teams") or []
    if isinstance(raw_teams, str):
        team_ids = tuple(raw_teams.split())
    else:
        team_ids = tuple(str(t) for t in raw_teams)

    raw_scope = claims.get("scope") or []
    if isinstance(raw_scope, str):
        scopes = tuple(raw_scope.split())
    else:
        scopes = tuple(str(s) for s in raw_scope)

    return ScopeContext(
        tenant_id=str(tenant_id),
        org_id=str(org_id) if org_id is not None else None,
        team_ids=team_ids,
        user_id=str(user_id),
        scopes=scopes,
    )
