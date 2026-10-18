"""Rebuilding Valkey counters from the durable ledger (real Valkey, real schema)."""

from shared.quota import keys
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry, QuotaLedger


def test_rebuild_restores_missing_counters_and_never_overwrites_live_ones(
    real_schema_db, valkey_sync
):
    """Flushed counters come back from the ledger; a live counter keeps its (higher) value."""
    db = real_schema_db
    org = int(db.organizations.insert(name="acme"))
    db.commit()
    ledger = QuotaLedger(db)
    ledger.add(org, 7, [LedgerEntry(SCOPE_USER, 11, 30), LedgerEntry(SCOPE_BUFFER, 4, 12),
                        LedgerEntry(SCOPE_OVERRIDE, 5, 9)])  # fmt: skip
    valkey_sync.set(keys.user_counter(11, 7), 99)
    assert ledger.rebuild_counters(valkey_sync, org, 7) == 2
    assert int(valkey_sync.get(keys.user_counter(11, 7))) == 99
    assert int(valkey_sync.get(keys.buffer_counter(4, 7))) == 12
    assert int(valkey_sync.get(keys.override_counter(5, 7))) == 9
    assert 0 < valkey_sync.ttl(keys.buffer_counter(4, 7)) <= 35 * 86400
    assert ledger.rebuild_counters(valkey_sync, org, 7) == 0  # idempotent
