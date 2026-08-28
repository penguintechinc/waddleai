"""Pure, in-memory model-access policy resolution (design spec §3, §4).

``ModelAccessPolicyResolver.resolve()`` decides whether a requested model is
blocked for a given (org, user, key) scope chain, and if so what to do about
it (reject outright, or reroute to a fallback). It performs no I/O -- the
caller (proxy ``RoutingStage`` in a follow-up branch, or a management-side
consumer) fetches the candidate ``model_access_policies`` rows for the
request's scope chain (one combined query, same shape as
``CacheConfigResolver._fetch_rows`` in ``shared/cache/config.py``) and hands
them to ``resolve()``.

Every row is a block/deny rule (no allow-carve-out concept in this build --
see the model-access-policy design spec §3.1 for the richer allow-carve-out
shape considered and deferred). Resolution is narrowest-scope-wins:
``key`` > ``user`` > ``org`` > ``global``. The first scope level (searched
narrowest-first) that has an enabled row matching the requested model wins
outright; broader scopes are never consulted once a match is found. Absence
of any matching row across all four levels is allowed by default --
deny-by-policy only, never deny-by-default.

Pattern matching is a single ``model_pattern`` field with glob semantics
(``fnmatch``), not a separate ``model_pattern``/``match_type`` pair --
an exact id and a no-wildcard glob pattern already match identically under
``fnmatch``, so a second column would only risk drifting out of sync with
the pattern's actual content.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass

_SCOPE_ORDER: tuple[str, ...] = ("key", "user", "org", "global")


@dataclass(slots=True, frozen=True)
class ModelAccessPolicyRow:
    """One ``model_access_policies`` row, decoupled from any specific storage backend."""

    id: int
    scope_type: str  # "global" | "org" | "user" | "key"
    scope_ref: str | None  # org_id/user_id/key_id as string; None for scope_type="global"
    model_pattern: str
    action: str = "reject"  # "reject" | "reroute"
    fallback_model: str | None = None
    reason: str | None = None
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class ModelAccessDecision:
    """The effective outcome of resolving a request's model-access policy."""

    allowed: bool
    action: str | None = None
    matched_policy: int | None = None
    fallback_model: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class ModelAccessPolicyResolver:
    """Resolves ``model_access_policies`` rows at key > user > org > global precedence.

    Stateless and I/O-free by design -- see the module docstring for why.
    """

    @staticmethod
    def resolve(
        *,
        org_id: int | str | None,
        user_id: int | str | None,
        key_id: int | str | None,
        requested_model: str,
        policies: Iterable[ModelAccessPolicyRow],
        team_id: int | str | None = None,
    ) -> ModelAccessDecision:
        """Resolve the effective decision for ``requested_model`` given candidate rows.

        Searches ``key`` -> ``user`` -> ``org`` -> ``global``; the first scope
        with an enabled, matching row wins outright (narrowest wins -- a
        matching org-wide block never overrides a narrower key-level rule
        for the same pattern, and vice versa). Multiple matching rows at the
        *same* scope tie-break on exact-pattern-over-glob, then highest id
        (most recently created). ``team_id`` is accepted for forward
        compatibility with the JWT ``teams`` claim but never matched today --
        no ``teams`` table exists yet (design spec §2), so it is documented
        dead space rather than silently ignored.
        """
        del team_id
        scope_refs: dict[str, str | None] = {
            "global": None,
            "org": _as_ref(org_id),
            "user": _as_ref(user_id),
            "key": _as_ref(key_id),
        }

        by_scope: dict[str, list[ModelAccessPolicyRow]] = {s: [] for s in _SCOPE_ORDER}
        for row in policies:
            if not row.enabled or row.scope_type not in by_scope:
                continue
            if row.scope_ref != scope_refs[row.scope_type]:
                continue
            by_scope[row.scope_type].append(row)

        for scope_type in _SCOPE_ORDER:
            match = _best_match(by_scope[scope_type], requested_model)
            if match is not None:
                return ModelAccessDecision(
                    allowed=False,
                    action=match.action,
                    matched_policy=match.id,
                    fallback_model=match.fallback_model,
                    reason=match.reason,
                )

        return ModelAccessDecision(allowed=True)


def _as_ref(value: int | str | None) -> str | None:
    """Normalize an identifier to the string form ``scope_ref`` is stored as."""
    return None if value is None else str(value)


def _best_match(
    rows: list[ModelAccessPolicyRow], requested_model: str
) -> ModelAccessPolicyRow | None:
    """The most-specific matching row at one scope: exact beats glob; ties break on highest id."""
    candidates = [r for r in rows if fnmatch.fnmatchcase(requested_model, r.model_pattern)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (0 if _is_glob(r.model_pattern) else 1, r.id))


def _is_glob(pattern: str) -> bool:
    """A pattern containing any fnmatch wildcard character is a glob, not an exact id."""
    return any(ch in pattern for ch in "*?[")
