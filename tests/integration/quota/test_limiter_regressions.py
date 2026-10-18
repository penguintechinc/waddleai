"""Regressions for the two TokenLimiter bugs (spec §6), against a real Valkey."""

from datetime import datetime

from shared.utils.token_limiter import KeyLimits, TokenLimiter


class _FlagOn:
    """Feature helper stub: every flag on."""

    def is_feature_enabled(self, flag, **kwargs):
        """Report every flag as enabled."""
        return True


async def _keys(client, pattern):
    """All keys matching ``pattern`` (SCAN, never KEYS)."""
    return sorted([k async for k in client.scan_iter(match=pattern)])


async def test_regression_bug_a_denied_by_token_budget_leaks_no_tpm_counter(valkey_async):
    """TPM passes (80<=100) but the monthly-token budget denies (80>50): no counter may remain."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        7, 80, 0.0, KeyLimits(tpm_limit=100, monthly_token_limit=50, monthly_usd_limit=None)
    )
    assert decision.allowed is False
    assert decision.reason == "monthly_tokens_exceeded"
    assert await _keys(valkey_async, "waddleai:tpm:7:*") == []
    assert await _keys(valkey_async, "waddleai:budget:tok:7:*") == []
    assert await _keys(valkey_async, "waddleai:resv:*") == []


async def test_regression_bug_a_denied_by_usd_leaks_neither_tpm_nor_token_counter(valkey_async):
    """USD denial must leave both earlier counters untouched."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        8, 10, 5.0, KeyLimits(tpm_limit=100, monthly_token_limit=1000, monthly_usd_limit=1)
    )
    assert decision.allowed is False
    assert decision.reason == "monthly_usd_exceeded"
    assert await _keys(valkey_async, "waddleai:tpm:8:*") == []
    assert await _keys(valkey_async, "waddleai:budget:tok:8:*") == []
    assert await _keys(valkey_async, "waddleai:budget:usd:8:*") == []


async def test_allowed_request_still_increments_all_counters(valkey_async):
    """The fix must not break the happy path: an allowed reserve debits every counter."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        9, 40, 0.5, KeyLimits(tpm_limit=100, monthly_token_limit=1000, monthly_usd_limit=10)
    )
    assert decision.allowed is True
    assert decision.reservation_id
    (tpm_key,) = await _keys(valkey_async, "waddleai:tpm:9:*")
    (tok_key,) = await _keys(valkey_async, "waddleai:budget:tok:9:*")
    assert int(await valkey_async.get(tpm_key)) == 40
    assert int(await valkey_async.get(tok_key)) == 40


async def test_regression_bug_b_reconcile_uses_reservation_window_not_reconcile_clock(valkey_async):
    """Reserved on 2026-01-31 23:59; reconciled 'now' (a later month): January counters adjust."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    limits = KeyLimits(tpm_limit=1000, monthly_token_limit=10_000, monthly_usd_limit=None)
    decision = await limiter.reserve(11, 100, 0.0, limits, now=datetime(2026, 1, 31, 23, 59, 30))
    assert decision.allowed is True
    await limiter.reconcile(decision.reservation_id, 40, 0.0)
    assert int(await valkey_async.get("waddleai:budget:tok:11:202601")) == 40
    assert int(await valkey_async.get("waddleai:tpm:11:202601312359")) == 40
    # No stray counter (e.g. a negative delta) was created in the reconcile-time window.
    assert await _keys(valkey_async, "waddleai:*:11:*") == [
        "waddleai:budget:tok:11:202601",
        "waddleai:tpm:11:202601312359",
    ]
    assert await _keys(valkey_async, "waddleai:resv:*") == []


async def test_reconcile_does_not_resurrect_an_expired_tpm_window(valkey_async):
    """An already-expired 60s TPM key must not be recreated by reconcile (it would never expire)."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    limits = KeyLimits(tpm_limit=1000, monthly_token_limit=None, monthly_usd_limit=None)
    decision = await limiter.reserve(12, 100, 0.0, limits, now=datetime(2026, 1, 5, 10, 0, 0))
    await valkey_async.delete("waddleai:tpm:12:202601051000")  # simulate the 60s TTL elapsing
    await limiter.reconcile(decision.reservation_id, 10, 0.0)
    assert await _keys(valkey_async, "waddleai:tpm:12:*") == []
