"""§14.6 Pro-tier seat metering.

Distinct billable identities for the license server's periodic
``keepalive({"users": N, "nodes": M})`` checkin. Mirrors
``shared/fleet/caps.py``'s ``count_managed_nodes`` -- a pure,
DB-adjacent counting function the eventual scheduled checkin job (§14.6:
"on a scheduled job (supercronic)") calls for the ``users`` half of the
payload; no such job exists in this codebase yet (surveyed: no
``keepalive``/``entitlement_usage`` caller anywhere outside
``shared/licensing/python_client.py`` itself), so this module is the seat
half of that not-yet-wired metering pipeline, built alongside H1's
service-account identity so seat counting is correct from the day the job
lands.

A seat is any enabled ``users`` row -- human or machine -- **except** a
service account whose ``service_kind`` is WaddleAI's own internal plumbing
(health checks, migration runners): see ``critical-rules.md`` Licensing
Model: Nodes & Seats ("product-internal identities ... never consume a
seat"). A customer-provisioned machine identity (``ci``, ``agent``, or any
``service_kind`` outside :data:`INTERNAL_SERVICE_KINDS`) counts exactly like
a human user -- the distinction that matters for metering is "is this
WaddleAI's own machinery", not "is this a human".
"""

from __future__ import annotations

from typing import Any

#: ``users.service_kind`` values that are WaddleAI-internal machinery, never
#: a customer-facing identity. Kept as the single source of truth so a
#: caller cannot special-case a kind name inline -- documented here is the
#: full internal set to date; anything else (``ci``, ``agent``, ``None`` for
#: ordinary human users) is billable.
INTERNAL_SERVICE_KINDS: frozenset[str] = frozenset({"health-check", "migration-runner"})


def is_billable_seat(*, is_service_account: bool, service_kind: str | None) -> bool:
    """True when a ``users`` row (already filtered to ``enabled``) counts as a seat.

    Pure predicate over the two H1 identity columns -- no DB access, so the
    counting rule is unit-testable without a database and reusable by both
    :func:`count_billable_seats` and any future admin-facing "who counts as
    a seat" reporting view.
    """
    if not is_service_account:
        return True
    return service_kind not in INTERNAL_SERVICE_KINDS


def count_billable_seats(db: Any) -> int:
    """Count distinct billable identities for the §14.6 ``keepalive({"users": N})`` checkin.

    Filters in Python rather than pushing the internal-kind exclusion into
    SQL: ``service_kind`` is nullable, and a ``NOT IN (...)`` predicate
    against a ``NULL`` column evaluates to ``NULL`` (excluded), not ``TRUE``,
    in standard SQL -- silently under-counting every human/non-internal
    seat unless paired with an explicit ``IS NULL`` branch. Fetching enabled
    rows and applying :func:`is_billable_seat` in Python sidesteps that
    footgun entirely and keeps the one counting rule in one place.

    Args:
        db: A penguin-dal/PyDAL-style handle exposing a reflected ``users``
            table (production path: Alembic-authoritative schema, per
            ``shared/database/models.py``'s ``get_db(reflect=True)``).

    """
    rows = db(db.users.enabled == True).select(  # noqa: E712 -- PyDAL query operator
        db.users.is_service_account, db.users.service_kind
    )
    return sum(
        1
        for row in rows
        if is_billable_seat(
            is_service_account=bool(row.is_service_account), service_kind=row.service_kind
        )
    )
