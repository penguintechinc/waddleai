"""Reconcile/release/sweep wrapper behaviour with a scripted fake client."""

from shared.quota import keys
from shared.quota.lease import QuotaLimiter


class FakeValkey:
    """Scripted async client for evalsha + sorted-set reads."""

    def __init__(self, eval_results=(), due=()):
        """Queue scripted evalsha replies and the due index members."""
        self.eval_results, self.calls = list(eval_results), []
        self.due = due if isinstance(due, Exception) else list(due)

    async def script_load(self, script):
        """Return a fixed sha."""
        return "sha"

    async def evalsha(self, sha, numkeys, *args):
        """Record the call and pop the next scripted reply (raise if it is an Exception)."""
        self.calls.append(args)
        item = self.eval_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def zrangebyscore(self, key, lo, hi, start=None, num=None):
        """Return the due members (or raise the scripted error)."""
        if isinstance(self.due, Exception):
            raise self.due
        return self.due


async def test_reconcile_parses_result_and_builds_args():
    """KEYS: record, index; ARGV: id, actual, ttl, override prefix, n, (id, cap) pairs."""
    fake = FakeValkey([[1, 5, 1, 3, b"5,7"]])
    r = await QuotaLimiter(fake).reconcile("rid", 9, [(5, 10), (7, 20)])
    assert (r.applied, r.from_reservation, r.from_buffer, r.overage, r.over_cap_override_ids) == (
        True,
        5,
        1,
        3,
        [5, 7],
    )
    args = fake.calls[0]
    assert args[:2] == (keys.reservation("rid"), keys.RESERVATION_INDEX)
    assert args[2:] == (
        "rid",
        "9",
        str(QuotaLimiter.COUNTER_TTL_S),
        keys.OVERRIDE_PREFIX,
        "2",
        "5",
        "10",
        "7",
        "20",
    )


async def test_reconcile_clamps_negative_actual_and_handles_no_overs():
    """A negative actual is treated as 0; an empty over-cap string is an empty list."""
    fake = FakeValkey([[1, 0, 0, 0, ""]])
    r = await QuotaLimiter(fake).reconcile("rid", -5)
    assert r.over_cap_override_ids == [] and fake.calls[0][3] == "0"


async def test_reconcile_missing_record_and_errors_are_not_applied(caplog):
    """Orphan -> applied False; Valkey error -> applied False + logged; neither raises."""
    assert (await QuotaLimiter(FakeValkey([[0, 0, 0, 0, ""]])).reconcile("x", 1)).applied is False
    with caplog.at_level("WARNING"):
        assert (
            await QuotaLimiter(FakeValkey([ConnectionError("down")])).reconcile("x", 1)
        ).applied is False
    assert "reconcile" in caplog.text


async def test_release_is_a_zero_actual_reconcile():
    """release() sends actual=0 and no override pairs."""
    fake = FakeValkey([[1, 0, 0, 0, ""]])
    assert (await QuotaLimiter(fake).release("rid")).applied is True
    assert fake.calls[0][2:] == (
        "rid",
        "0",
        str(QuotaLimiter.COUNTER_TTL_S),
        keys.OVERRIDE_PREFIX,
        "0",
    )


async def test_sweep_counts_refunds_and_survives_errors():
    """Two due ids, one applied one orphan -> 1; a zrange failure -> 0, never raises."""
    fake = FakeValkey([[1, 0, 0, 0, ""], [0, 0, 0, 0, ""]], due=[b"a", "b"])
    assert await QuotaLimiter(fake).sweep_expired(now=1.0) == 1
    assert await QuotaLimiter(FakeValkey(due=ConnectionError("down"))).sweep_expired(now=1.0) == 0
