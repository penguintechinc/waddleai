"""Ledger accumulation, CAS retry, ordering and org scoping."""

import pytest

from shared.quota.ledger import SCOPE_BUFFER, SCOPE_USER, LedgerEntry, QuotaLedger


def _org(db, name="acme"):
    """Insert an organization and return its id."""
    org = int(db.organizations.insert(name=name))
    db.commit()
    return org


def test_add_accumulates_per_scope_and_cycle(real_schema_db):
    """Two adds to the same scope+cycle sum; another cycle or scope is a separate row."""
    ledger, org = QuotaLedger(real_schema_db), _org(real_schema_db)
    ledger.add(org, 3, [LedgerEntry(SCOPE_USER, 7, 10), LedgerEntry(SCOPE_BUFFER, 4, 2)])
    ledger.add(org, 3, [LedgerEntry(SCOPE_USER, 7, 5)])
    ledger.add(org, 4, [LedgerEntry(SCOPE_USER, 7, 1)])
    assert ledger.usage(org, 3) == [LedgerEntry(SCOPE_BUFFER, 4, 2), LedgerEntry(SCOPE_USER, 7, 15)]
    assert ledger.usage(org, 4) == [LedgerEntry(SCOPE_USER, 7, 1)]


@pytest.mark.parametrize("credits", [0, -3])
def test_non_positive_entries_are_skipped(real_schema_db, credits):
    """Zero/negative credits write nothing (the engine also filters; this is the backstop)."""
    ledger, org = QuotaLedger(real_schema_db), _org(real_schema_db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 7, credits)])
    assert ledger.usage(org, 1) == []


def test_usage_is_org_scoped(real_schema_db):
    """One tenant never sees another's ledger rows."""
    ledger = QuotaLedger(real_schema_db)
    a, b = _org(real_schema_db, "a"), _org(real_schema_db, "b")
    ledger.add(a, 1, [LedgerEntry(SCOPE_USER, 1, 5)])
    assert ledger.usage(b, 1) == []


def test_cas_conflict_is_retried_and_no_credit_is_lost(real_schema_db):
    """A competing writer wins between our read and update: the retry re-adds on top."""
    db, org = real_schema_db, _org(real_schema_db)

    class Racy(QuotaLedger):
        """Returns a STALE row on the first read, as if another writer had just updated."""

        stale_once = True

        def _read(self, query):
            row = super()._read(query)
            if Racy.stale_once and row is not None:
                Racy.stale_once = False
                query.update(credits_used=int(row.credits_used) + 100)  # the competing writer
            return row

    ledger = Racy(db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])  # creates the row (value 1)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 5)])  # stale read -> CAS miss -> retry
    assert ledger.usage(org, 1) == [LedgerEntry(SCOPE_USER, 9, 106)]


def test_cas_gives_up_loudly_after_bounded_retries(real_schema_db, monkeypatch):
    """A permanently losing CAS raises instead of looping forever or silently dropping credits."""
    db, org = real_schema_db, _org(real_schema_db)
    ledger = QuotaLedger(db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])
    real_read = ledger._read

    def always_stale(query):
        row = real_read(query)
        query.update(credits_used=int(row.credits_used) + 1)
        return row

    monkeypatch.setattr(ledger, "_read", always_stale)
    with pytest.raises(RuntimeError, match="did not converge"):
        ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])


def test_insert_race_integrity_error_is_retried_without_rollback(real_schema_db, monkeypatch):
    """A lost insert race (unique violation) re-reads and CAS-adds, losing no credits."""
    db, org = real_schema_db, _org(real_schema_db)
    ledger = QuotaLedger(db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 4)])
    real_read, calls = ledger._read, []

    def missing_once(query):
        calls.append(1)
        return None if len(calls) == 1 else real_read(query)

    monkeypatch.setattr(ledger, "_read", missing_once)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 3)])
    assert ledger.usage(org, 1) == [LedgerEntry(SCOPE_USER, 9, 7)]


def test_rebuild_skips_unknown_scope_types(real_schema_db):
    """A ledger row with an unrecognised scope type is ignored by rebuild, not a crash."""
    ledger, org = QuotaLedger(real_schema_db), _org(real_schema_db)
    ledger.add(org, 1, [LedgerEntry("mystery", 1, 5)])

    class Fake:
        """Valkey stand-in recording SET calls."""

        sets: list = []

        def set(self, *a, **k):
            self.sets.append(a)
            return True

    assert ledger.rebuild_counters(Fake(), org, 1) == 0
    assert Fake.sets == []
