# Elastic Pooled Quota — Phase 4b: Reconcile, Release, Expiry Sweep

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6 (reconcile), §7, D5, D7.

Spec gap closed here: D7 says blocked/errored requests "release their reservation via TTL", but a TTL on the reservation RECORD does not refund the counters that `reserve` already incremented — the credits would leak until the cycle resets. This phase adds an explicit `release` (used by Task 27 when the pipeline blocks after reserving) and a `sweep_expired` (driven by the Task 29 sweeper) so a record that expires unreconciled (crash, client disconnect) refunds its credits.

---

### Task 22: `QuotaLimiter.reconcile`, `release`, `sweep_expired`

**Files:**
- Modify: `shared/quota/lease.py` (add `LUA_RECONCILE`, register it in `_SCRIPTS`, add three methods)
- Test: `tests/integration/quota/test_lease_reconcile.py` (real Valkey), `tests/unit/quota/test_lease_reconcile_wrapper.py` (fake client)

**Interfaces:**
- Consumes: Task 21 `QuotaLimiter` (`_run`, `_clock`, constants), `ReconcileResult` (Task 11), `keys.OVERRIDE_PREFIX`/`reservation`/`RESERVATION_INDEX`.
- Produces:
  - `async QuotaLimiter.reconcile(reservation_id: str, actual_credits: int, override_caps: Sequence[tuple[int, int]] = ()) -> ReconcileResult` — adjusts the counters named IN THE RESERVATION RECORD (never the clock) by `actual - reserved`: extra credits take remaining guarantee first then the buffer (overage = buffer consumption above its cap, recorded not blocked); a refund returns buffer credits first, then reservation credits; counters never go below zero and expired counters are never resurrected; each `(override_id, cap)` counter in the record's cycle is debited the ACTUAL credits and any now over its cap is reported in `over_cap_override_ids`; the record and its index entry are deleted. Missing record -> `ReconcileResult(applied=False)`. Any error -> `applied=False` (never raises).
  - `async QuotaLimiter.release(reservation_id: str) -> ReconcileResult` — reconcile with 0 actual credits and no overrides (full refund).
  - `async QuotaLimiter.sweep_expired(*, now: float | None = None, batch: int = 100) -> int` — releases up to `batch` reservations whose index score `< now`; returns how many were refunded; errors return 0.

- [ ] **Step 1: Write the failing tests**

`tests/integration/quota/test_lease_reconcile.py`:

```python
"""Reconcile/release/sweep against a real Valkey: refunds, overage, stored-cycle accounting."""

from shared.quota import keys
from shared.quota.lease import QuotaLimiter

BASE = {"team_id": 4, "cycle": 7, "day": "20260203", "daily_subcap": None, "overrides": ()}


def _req(user_id=1, credits=4, guaranteed=10, buffer_cap=5, **kw):
    """Keyword args for one reserve() call."""
    return {**BASE, "user_id": user_id, "credits": credits, "guaranteed": guaranteed,
            "buffer_cap": buffer_cap, **kw}  # fmt: skip


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def test_actual_below_reserved_refunds_buffer_first(valkey_async):
    """guarantee 5, reserve 8 (5 resv + 3 buf), actual 6: 2 credits return to the BUFFER."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=8, guaranteed=5, buffer_cap=9))
    r = await limiter.reconcile(d.reservation_id, 6)
    assert (r.applied, r.from_reservation, r.from_buffer, r.overage) == (True, 5, 1, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 5
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 1
    assert await valkey_async.get(keys.reservation(d.reservation_id)) is None
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is None


async def test_actual_above_reserved_takes_remaining_guarantee_first(valkey_async):
    """reserve 4 of a 10 guarantee, actual 9: the 5 extra come from the guarantee, not the buffer."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=4))
    r = await limiter.reconcile(d.reservation_id, 9)
    assert (r.from_reservation, r.from_buffer, r.overage) == (9, 0, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 9
    assert await valkey_async.get(keys.buffer_counter(4, 7)) is None


async def test_buffer_overage_is_recorded_not_blocked(valkey_async):
    """Underestimated request: buffer may exceed its cap; overage reports the excess."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=2, guaranteed=0, buffer_cap=3))
    r = await limiter.reconcile(d.reservation_id, 6)  # delta 4 -> buffer 6 vs cap 3
    assert (r.applied, r.from_buffer, r.overage) == (True, 6, 3)
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 6
    denied = await limiter.reserve(**_req(user_id=2, credits=1, guaranteed=0, buffer_cap=3))
    assert denied.reason == "quota_exhausted"  # the NEXT request is blocked


async def test_release_refunds_everything_and_never_goes_negative(valkey_async):
    """Full refund (cache hit / blocked request) returns counters to zero; a vanished counter is not recreated."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=8, guaranteed=5, buffer_cap=9))
    r = await limiter.release(d.reservation_id)
    assert (r.applied, r.from_reservation, r.from_buffer) == (True, 0, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 0
    d2 = await limiter.reserve(**_req(user_id=3, credits=4))
    await valkey_async.delete(keys.user_counter(3, 7))  # counter expired meanwhile
    await limiter.release(d2.reservation_id)
    assert await valkey_async.get(keys.user_counter(3, 7)) is None


async def test_reconcile_charges_the_reservations_own_cycle_not_a_later_one(valkey_async):
    """Review-focus 5: reserved in cycle 7, another request lands in cycle 8, reconcile still charges 7."""
    limiter = QuotaLimiter(valkey_async)
    first = await limiter.reserve(**_req(credits=4, cycle=7))
    await limiter.reserve(**{**_req(credits=2), "cycle": 8})
    await limiter.reconcile(first.reservation_id, 1)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 1
    assert await _n(valkey_async, keys.user_counter(1, 8)) == 2


async def test_override_counters_are_debited_actual_and_over_cap_is_reported(valkey_async):
    """Override caps are authoritative at reconcile: debit actual in the stored cycle, report over-cap ids."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=4))
    r = await limiter.reconcile(d.reservation_id, 12, [(5, 10), (6, 0)])
    assert r.over_cap_override_ids == [5]  # cap 0 = unlimited, never reported
    assert await _n(valkey_async, keys.override_counter(5, 7)) == 12
    d2 = await limiter.reserve(**_req(credits=1))
    r2 = await limiter.reconcile(d2.reservation_id, 1, [(5, 10)])
    assert r2.over_cap_override_ids == [5] and await _n(valkey_async, keys.override_counter(5, 7)) == 13


async def test_daily_counter_follows_the_actual(valkey_async):
    """Daily sub-cap counter is adjusted by the same delta: reserve 10, actual 4 -> 4."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=10, daily_subcap=100))
    await limiter.reconcile(d.reservation_id, 4)
    assert await _n(valkey_async, keys.daily_counter(1, "20260203")) == 4


async def test_unknown_reservation_is_not_applied_and_creates_nothing(valkey_async):
    """Orphan reconcile (record expired/swept): applied False, no stray keys."""
    r = await QuotaLimiter(valkey_async).reconcile("does-not-exist", 5, [(5, 10)])
    assert r.applied is False
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:*")] == []


async def test_sweep_refunds_only_expired_reservations(valkey_async):
    """Index score = reserve time + 3600: a sweep before expiry does nothing, after it refunds."""
    limiter = QuotaLimiter(valkey_async, clock=lambda: 1000.0)
    d = await limiter.reserve(**_req(credits=4))
    assert await limiter.sweep_expired(now=2000.0) == 0
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 4
    assert await limiter.sweep_expired(now=5000.0) == 1
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is None
    assert await limiter.sweep_expired(now=9999.0) == 0


async def test_sweep_drops_index_entries_whose_record_is_gone(valkey_async):
    """An index entry with no record is removed (not retried forever) and not counted as a refund."""
    await valkey_async.zadd(keys.RESERVATION_INDEX, {"ghost": 1.0})
    assert await QuotaLimiter(valkey_async).sweep_expired(now=10.0) == 0
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, "ghost") is None
```

`tests/unit/quota/test_lease_reconcile_wrapper.py`:

```python
"""Reconcile/release/sweep wrapper behaviour with a scripted fake client."""

from shared.quota import keys
from shared.quota.lease import QuotaLimiter


class FakeValkey:
    """Scripted async client for evalsha + sorted-set reads."""

    def __init__(self, eval_results=(), due=()):
        self.eval_results, self.calls = list(eval_results), []
        self.due = due if isinstance(due, Exception) else list(due)

    async def script_load(self, script):
        return "sha"

    async def evalsha(self, sha, numkeys, *args):
        self.calls.append(args)
        item = self.eval_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def zrangebyscore(self, key, lo, hi, start=None, num=None):
        if isinstance(self.due, Exception):
            raise self.due
        return self.due


async def test_reconcile_parses_result_and_builds_args():
    """KEYS: record, index; ARGV: id, actual, ttl, override prefix, n, (id, cap) pairs."""
    fake = FakeValkey([[1, 5, 1, 3, b"5,7"]])
    r = await QuotaLimiter(fake).reconcile("rid", 9, [(5, 10), (7, 20)])
    assert (r.applied, r.from_reservation, r.from_buffer, r.overage, r.over_cap_override_ids) == (True, 5, 1, 3, [5, 7])
    args = fake.calls[0]
    assert args[:2] == (keys.reservation("rid"), keys.RESERVATION_INDEX)
    assert args[2:] == ("rid", "9", str(QuotaLimiter.COUNTER_TTL_S), keys.OVERRIDE_PREFIX, "2", "5", "10", "7", "20")


async def test_reconcile_clamps_negative_actual_and_handles_no_overs():
    """A negative actual is treated as 0; an empty over-cap string is an empty list."""
    fake = FakeValkey([[1, 0, 0, 0, ""]])
    r = await QuotaLimiter(fake).reconcile("rid", -5)
    assert r.over_cap_override_ids == [] and fake.calls[0][3] == "0"


async def test_reconcile_missing_record_and_errors_are_not_applied(caplog):
    """Orphan -> applied False; Valkey error -> applied False + logged; neither raises."""
    assert (await QuotaLimiter(FakeValkey([[0, 0, 0, 0, ""]])).reconcile("x", 1)).applied is False
    with caplog.at_level("WARNING"):
        assert (await QuotaLimiter(FakeValkey([ConnectionError("down")])).reconcile("x", 1)).applied is False
    assert "reconcile" in caplog.text


async def test_release_is_a_zero_actual_reconcile():
    """release() sends actual=0 and no override pairs."""
    fake = FakeValkey([[1, 0, 0, 0, ""]])
    assert (await QuotaLimiter(fake).release("rid")).applied is True
    assert fake.calls[0][2:] == ("rid", "0", str(QuotaLimiter.COUNTER_TTL_S), keys.OVERRIDE_PREFIX, "0")


async def test_sweep_counts_refunds_and_survives_errors():
    """Two due ids, one applied one orphan -> 1; a zrange failure -> 0, never raises."""
    fake = FakeValkey([[1, 0, 0, 0, ""], [0, 0, 0, 0, ""]], due=[b"a", "b"])
    assert await QuotaLimiter(fake).sweep_expired(now=1.0) == 1
    assert await QuotaLimiter(FakeValkey(due=ConnectionError("down"))).sweep_expired(now=1.0) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/quota/test_lease_reconcile_wrapper.py tests/integration/quota/test_lease_reconcile.py -v --no-cov`
Expected: FAIL — `AttributeError: 'QuotaLimiter' object has no attribute 'reconcile'`.

- [ ] **Step 3: Write minimal implementation**

In `lease.py` add `ReconcileResult` to the `shared.quota.types` import, then add below `LUA_RESERVE`:

```python
LUA_RECONCILE = """
local rid = ARGV[1]
local actual = tonumber(ARGV[2])
local counter_ttl = tonumber(ARGV[3])
local override_prefix = ARGV[4]
local n = tonumber(ARGV[5])

local data = redis.call("GET", KEYS[1])
if not data then
    redis.call("ZREM", KEYS[2], rid)
    return {0, 0, 0, 0, ""}
end
local r = cjson.decode(data)

-- Decrement only a counter that still exists (never resurrect an expired one), floor at 0.
local function dec(key, amount)
    if amount > 0 and redis.call("EXISTS", key) == 1 then
        local v = redis.call("DECRBY", key, amount)
        if v < 0 then redis.call("SET", key, 0, "KEEPTTL") end
    end
end

local reserved = r.from_resv + r.from_buf
local delta = actual - reserved
local from_resv, from_buf, overage = r.from_resv, r.from_buf, 0

if delta > 0 then
    local used = tonumber(redis.call("GET", r.user_key)) or 0
    local headroom = r.guaranteed - used
    if headroom < 0 then headroom = 0 end
    local add_resv = delta
    if add_resv > headroom then add_resv = headroom end
    local add_buf = delta - add_resv
    if add_resv > 0 then
        redis.call("INCRBY", r.user_key, add_resv)
        redis.call("EXPIRE", r.user_key, counter_ttl)
    end
    if add_buf > 0 then
        local new_buf = redis.call("INCRBY", r.buf_key, add_buf)
        redis.call("EXPIRE", r.buf_key, counter_ttl)
        local over = new_buf - r.buffer_cap
        if over > 0 then overage = math.min(over, add_buf) end
    end
    from_resv = from_resv + add_resv
    from_buf = from_buf + add_buf
elseif delta < 0 then
    local refund = -delta
    local r_buf = math.min(refund, from_buf)
    local r_resv = refund - r_buf
    dec(r.buf_key, r_buf)
    dec(r.user_key, r_resv)
    from_buf = from_buf - r_buf
    from_resv = from_resv - r_resv
end

if r.daily_on == 1 and delta ~= 0 then
    if delta > 0 then
        redis.call("INCRBY", r.day_key, delta)
    else
        dec(r.day_key, -delta)
    end
end

-- Override caps are authoritative HERE (provider now known): debit the ACTUAL credits.
local over_ids = {}
if actual > 0 then
    for i = 0, n - 1 do
        local id = ARGV[6 + i * 2]
        local cap = tonumber(ARGV[7 + i * 2])
        local key = override_prefix .. id .. ":" .. r.cycle
        local used = redis.call("INCRBY", key, actual)
        redis.call("EXPIRE", key, counter_ttl)
        if cap > 0 and used > cap then table.insert(over_ids, id) end
    end
end

redis.call("DEL", KEYS[1])
redis.call("ZREM", KEYS[2], rid)
return {1, from_resv, from_buf, overage, table.concat(over_ids, ",")}
"""
```

Change `_SCRIPTS` to `{"reserve": LUA_RESERVE, "reconcile": LUA_RECONCILE}` and add to the class:

```python
    async def reconcile(
        self,
        reservation_id: str,
        actual_credits: int,
        override_caps: Sequence[tuple[int, int]] = (),
    ) -> ReconcileResult:
        """Settle a reservation against the actual credits (see the module docstring); never raises."""
        pairs = [str(v) for oid, cap in override_caps for v in (oid, cap)]
        args = [
            reservation_id, max(0, int(actual_credits)), self.COUNTER_TTL_S, keys.OVERRIDE_PREFIX,
            len(override_caps), *pairs,
        ]  # fmt: skip
        script_keys = [keys.reservation(reservation_id), keys.RESERVATION_INDEX]
        try:
            result = await self._run("reconcile", script_keys, [str(a) for a in args])
            if not int(result[0]):
                return ReconcileResult(applied=False)
            over = _text(result[4])
            return ReconcileResult(
                applied=True, from_reservation=int(result[1]), from_buffer=int(result[2]),
                overage=int(result[3]),
                over_cap_override_ids=[int(x) for x in over.split(",") if x],
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 -- reconcile must never fail the response
            logger.warning("quota reconcile failed (%s: %s)", type(exc).__name__, exc)
            return ReconcileResult(applied=False)

    async def release(self, reservation_id: str) -> ReconcileResult:
        """Refund a reservation in full (blocked request, cache hit, expired record)."""
        return await self.reconcile(reservation_id, 0, ())

    async def sweep_expired(self, *, now: float | None = None, batch: int = 100) -> int:
        """Refund up to ``batch`` reservations whose index score is in the past; return the count."""
        moment = self._clock() if now is None else now
        try:
            due = await self.valkey.zrangebyscore(
                keys.RESERVATION_INDEX, "-inf", moment, start=0, num=batch
            )
        except Exception as exc:  # noqa: BLE001 -- a sweeper pass failing is retried next tick
            logger.warning("quota sweep failed to read the index (%s: %s)", type(exc).__name__, exc)
            return 0
        released = 0
        for raw in due:
            if (await self.release(_text(raw))).applied:
                released += 1
        return released
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/quota/test_lease_reconcile_wrapper.py tests/unit/quota/test_lease_wrapper.py -v --no-cov && make test-valkey`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/quota/lease.py tests/integration/quota/test_lease_reconcile.py tests/unit/quota/test_lease_reconcile_wrapper.py
git commit -m "feat(quota): reconcile/release/sweep Lua — stored-cycle accounting, refunds, authoritative override caps"
git push
```
