"""Durable per-scope, per-cycle credit rollup (pooled-quota spec section 5, D7).

Written once per reconciled request with the FINAL reservation/buffer/override split, read by the
management usage report, and used to rebuild Valkey counters after a flush. penguin-dal has no
atomic ``col = col + n`` update, so ``add`` is an optimistic compare-and-set on the previous
value with a bounded retry; each statement is its own transaction (no rollback after a caught
``IntegrityError``). In-flight (unreconciled) requests are not in the ledger, so a rebuild can
under-count by at most the credits currently in flight.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from shared.quota import keys
from shared.quota.lease import QuotaLimiter

logger = logging.getLogger(__name__)

SCOPE_USER = "user"
SCOPE_BUFFER = "team_buffer"
SCOPE_OVERRIDE = "override"
_MAX_TRIES = 8


@dataclass(slots=True, frozen=True)
class LedgerEntry:
    """Credits consumed by one scope (user / team_buffer / override) in one cycle."""

    scope_type: str
    scope_id: int
    credits: int


class QuotaLedger:
    """Read/write access to ``quota_usage_ledger`` over a penguin-dal handle."""

    def __init__(self, db: Any) -> None:
        """Bind the DAL handle."""
        self.db = db

    def _query(self, org_id: int, cycle: int, entry: LedgerEntry) -> Any:
        """Query selecting the single ledger row for one scope in one cycle."""
        t = self.db.quota_usage_ledger
        return self.db(
            (t.scope_type == entry.scope_type) & (t.scope_id == entry.scope_id)
            & (t.organization_id == org_id) & (t.cycle_index == cycle)
        )  # fmt: skip

    def _read(self, query: Any) -> Any:
        """First row matching ``query`` (a seam the CAS-retry test overrides)."""
        return query.select().first()

    def _add_one(self, org_id: int, cycle: int, entry: LedgerEntry) -> None:
        """Insert-or-CAS-add one entry, retrying on a lost race."""
        for _ in range(_MAX_TRIES):
            query = self._query(org_id, cycle, entry)
            row = self._read(query)
            now = datetime.utcnow()
            if row is None:
                try:
                    self.db.quota_usage_ledger.insert(
                        scope_type=entry.scope_type, scope_id=entry.scope_id,
                        organization_id=org_id, cycle_index=cycle,
                        credits_used=entry.credits, updated_at=now,
                    )  # fmt: skip
                    return
                except IntegrityError:
                    logger.debug(
                        "ledger insert raced for %s:%s, retrying", entry.scope_type, entry.scope_id
                    )
                    continue  # a concurrent writer created it first: re-read and CAS
            current = int(row.credits_used or 0)
            if self._cas(org_id, cycle, entry, current, now):
                return
            logger.debug("ledger CAS missed for %s:%s, retrying", entry.scope_type, entry.scope_id)
        raise RuntimeError(
            f"quota ledger add did not converge for {entry.scope_type}:{entry.scope_id}"
        )

    def _cas(
        self, org_id: int, cycle: int, entry: LedgerEntry, current: int, now: datetime
    ) -> bool:
        """Update ``credits_used`` only if it still equals ``current``; True when it applied."""
        t = self.db.quota_usage_ledger
        guarded = self.db(
            (t.scope_type == entry.scope_type) & (t.scope_id == entry.scope_id)
            & (t.organization_id == org_id) & (t.cycle_index == cycle) & (t.credits_used == current)
        )  # fmt: skip
        return bool(guarded.update(credits_used=current + entry.credits, updated_at=now))

    def add(self, org_id: int, cycle: int, entries: Sequence[LedgerEntry]) -> None:
        """Accumulate ``entries`` into the ledger; non-positive credits are skipped."""
        for entry in entries:
            if entry.credits > 0:
                self._add_one(org_id, cycle, entry)

    def usage(self, org_id: int, cycle: int) -> list[LedgerEntry]:
        """All ledger rows of the org for ``cycle``, ordered by scope type then id."""
        t = self.db.quota_usage_ledger
        rows = self.db((t.organization_id == org_id) & (t.cycle_index == cycle)).select()
        entries = [
            LedgerEntry(r.scope_type, int(r.scope_id), int(r.credits_used or 0)) for r in rows
        ]
        return sorted(entries, key=lambda e: (e.scope_type, e.scope_id))

    def rebuild_counters(self, valkey: Any, org_id: int, cycle: int) -> int:
        """``SET NX`` each ledger row into its Valkey counter; return how many keys were created."""
        builders = {
            SCOPE_USER: keys.user_counter,
            SCOPE_BUFFER: keys.buffer_counter,
            SCOPE_OVERRIDE: keys.override_counter,
        }
        created = 0
        for entry in self.usage(org_id, cycle):
            build = builders.get(entry.scope_type)
            if build is None:
                continue
            if valkey.set(
                build(entry.scope_id, cycle), entry.credits, nx=True, ex=QuotaLimiter.COUNTER_TTL_S
            ):
                created += 1
        return created
