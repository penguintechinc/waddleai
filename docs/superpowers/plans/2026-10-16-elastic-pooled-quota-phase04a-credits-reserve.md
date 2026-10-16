# Elastic Pooled Quota — Phase 4a: Credit Estimate + Atomic Lease (Reserve)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6, D4, D8.

---

### Task 20: Request-size heuristic shared by the budget stage and the quota engine

`TokenBudgetStage` (`pipeline/stages.py:436`) estimates input as `len(content)//4` and output as 2x input, and crashes on Anthropic-style block-list content. Extract one robust helper both stages use.

**Files:**
- Create: `shared/quota/credits.py`
- Modify: `proxy/apps/proxy_server/pipeline/stages.py` (`TokenBudgetStage.__call__`, the three lines computing `estimated_input`/`estimated_output`/`total_estimated`; add the import)
- Test: `tests/unit/quota/test_credits.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `shared.quota.credits.estimate_io_tokens(messages: Sequence[Mapping[str, Any]]) -> tuple[int, int]` returning `(estimated_input_tokens, estimated_output_tokens)`: input = `sum(len(text)//4)` with a floor of 1, output = `2 * input` (worst-case). Content may be `str`, a list of blocks (`{"type": "text", "text": ...}` or bare strings), or `None`.

- [ ] **Step 1: Write the failing test**

`tests/unit/quota/test_credits.py`:

```python
"""The shared request-size heuristic (conservative: 4 chars/token in, 2x out)."""

import pytest

from shared.quota.credits import estimate_io_tokens


def test_string_content_four_chars_per_token_and_double_output():
    """400 chars -> 100 in, 200 out."""
    assert estimate_io_tokens([{"role": "user", "content": "x" * 400}]) == (100, 200)


def test_block_list_content_counts_text_blocks():
    """Anthropic block lists: text blocks and bare strings count, non-text blocks do not."""
    msgs = [{"content": [{"type": "text", "text": "a" * 40}, {"type": "image", "source": {}}, "b" * 40]}]
    assert estimate_io_tokens(msgs) == (20, 40)


@pytest.mark.parametrize("messages", [[], [{"content": None}], [{"role": "user"}], [{"content": ""}]])
def test_empty_input_floors_at_one_token(messages):
    """regression-focus: an empty prompt must still reserve >= 1 credit's worth, never 0."""
    assert estimate_io_tokens(messages) == (1, 2)


def test_messages_are_summed():
    """Per-message floors do not apply; the sum does."""
    assert estimate_io_tokens([{"content": "x" * 40}, {"content": "y" * 40}]) == (20, 40)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/quota/test_credits.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.credits`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/credits.py`:

```python
"""Request-size heuristic shared by TokenBudgetStage and the pooled-quota engine (spec §6.2).

Provider/tokenizer are unknown pre-dispatch, so reserve with a conservative worst case:
~4 characters per input token and an output of twice the input.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def _text_len(content: Any) -> int:
    """Character length of message content: str, block list (text blocks / bare str), or None."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, Mapping):
                total += _text_len(block.get("text"))
        return total
    return 0


def estimate_io_tokens(messages: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Return ``(estimated_input_tokens, estimated_output_tokens)`` for ``messages``.

    Input is ``sum(chars // 4)`` floored at 1 so an empty prompt never reserves zero; output
    is twice the input.
    """
    estimated_input = sum(_text_len(m.get("content")) // 4 for m in messages) or 1
    return estimated_input, estimated_input * 2
```

In `stages.py` add `from shared.quota.credits import estimate_io_tokens` to the imports and replace in `TokenBudgetStage.__call__`:

```python
        # Estimate input tokens (simplified: ~4 chars per token)
        estimated_input = sum(len(m.get("content", "")) // 4 for m in ctx.messages) or 1
        # Conservative estimate: 2x input for output
        estimated_output = estimated_input * 2
        total_estimated = estimated_input + estimated_output
```

with:

```python
        estimated_input, estimated_output = estimate_io_tokens(ctx.messages)
        total_estimated = estimated_input + estimated_output
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/quota/test_credits.py tests/unit/proxy/test_pipeline_stages.py -v --no-cov`
Expected: PASS (existing `TokenBudgetStage` tests keep their numbers: same arithmetic for string content).

- [ ] **Step 5: Commit**

```bash
git add shared/quota/credits.py proxy/apps/proxy_server/pipeline/stages.py tests/unit/quota/test_credits.py
git commit -m "refactor(quota): shared estimate_io_tokens (block-list safe) used by TokenBudgetStage"
git push
```

---

### Task 21: `QuotaLimiter.reserve` — the atomic lease-from-buffer Lua

New Lua (not the old limiter script): debit the user's reservation first, lease the remainder from the team buffer (first-come, no preemption), else deny; check daily sub-cap and override caps read-only; **all checks before any mutation** (Bug A, built in). The reservation record stores the counter key names and cycle so reconcile never reads a clock (Bug B, built in). Fail-open on any Valkey error (D10).

**Files:**
- Create: `shared/quota/lease.py`
- Test: `tests/integration/quota/test_lease_reserve.py` (real Valkey), `tests/unit/quota/test_lease_wrapper.py` (fake client)

**Interfaces:**
- Consumes: `valkey_async` fixture (Task 1); `shared.quota.keys`; `OverrideRule`, `QuotaDecision`, `REASON_*` (Task 11).
- Produces: `QuotaLimiter(valkey, *, clock: Callable[[], float] = time.time)`; `async QuotaLimiter.reserve(*, user_id: int, team_id: int, cycle: int, day: str, credits: int, guaranteed: int, buffer_cap: int, daily_subcap: int | None, overrides: Sequence[OverrideRule]) -> QuotaDecision` (credits clamped to >= 1; on allow, `reservation_id` set and `from_reservation + from_buffer == credits`; on deny `reason` is one of `quota_exhausted`/`daily_subcap_exceeded`/`override_exceeded` and NOTHING was mutated; on any Valkey/script error returns `QuotaDecision(allowed=True, degraded=True)`); class constants `RESERVATION_TTL_S = 3600`, `RECORD_TTL_S = 4200`, `COUNTER_TTL_S = 35 * 86400`; internal `_run(name, keys, args)` (EVALSHA with one NOSCRIPT reload-and-retry) reused by Task 22. Reservation record JSON fields: `user_key, buf_key, day_key, daily_on, cycle, credits, from_resv, from_buf, guaranteed, buffer_cap`; every reservation id is also `ZADD`ed to `wa:quota:resv_idx` with score `now + 3600`.

- [ ] **Step 1: Write the failing tests**

`tests/integration/quota/test_lease_reserve.py`:

```python
"""The lease Lua against a real Valkey: reservation -> buffer -> deny, atomically, no preemption."""

import asyncio
import json

from shared.quota import keys
from shared.quota.lease import QuotaLimiter
from shared.quota.types import OverrideRule

BASE = {"team_id": 4, "cycle": 7, "day": "20260203", "daily_subcap": None, "overrides": ()}


def _req(user_id=1, credits=4, guaranteed=10, buffer_cap=5, **kw):
    """Keyword args for one reserve() call with sensible defaults."""
    return {**BASE, "user_id": user_id, "credits": credits, "guaranteed": guaranteed,
            "buffer_cap": buffer_cap, **kw}  # fmt: skip


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def test_reservation_is_consumed_before_the_buffer(valkey_async):
    """Within the guarantee: from_reservation only, buffer untouched."""
    d = await QuotaLimiter(valkey_async).reserve(**_req())
    assert (d.allowed, d.from_reservation, d.from_buffer, d.degraded) == (True, 4, 0, False)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 4
    assert await valkey_async.get(keys.buffer_counter(4, 7)) is None


async def test_spill_into_buffer_then_exhaustion_leaves_no_residue(valkey_async):
    """Guarantee 10: 4 + 8 spills 2 into the buffer; a request the buffer cannot cover is denied untouched."""
    limiter = QuotaLimiter(valkey_async)
    await limiter.reserve(**_req(credits=4))
    d = await limiter.reserve(**_req(credits=8))
    assert (d.allowed, d.from_reservation, d.from_buffer) == (True, 6, 2)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 10
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2
    before = sorted([k async for k in valkey_async.scan_iter(match="wa:quota:*")])
    denied = await limiter.reserve(**_req(credits=4))  # needs 4 from buffer, 2+4 > 5
    assert (denied.allowed, denied.reason, denied.reservation_id) == (False, "quota_exhausted", None)
    assert sorted([k async for k in valkey_async.scan_iter(match="wa:quota:*")]) == before
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2


async def test_exact_buffer_boundary_is_allowed_one_more_is_not(valkey_async):
    """Remaining buffer exactly equals the need -> allowed; then 1 more credit -> denied."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(credits=5, guaranteed=0, buffer_cap=5))).allowed is True
    assert (await limiter.reserve(**_req(credits=1, guaranteed=0, buffer_cap=5))).reason == "quota_exhausted"


async def test_leasing_never_touches_another_users_reservation(valkey_async):
    """No preemption: B (no guarantee) drains the buffer; A's guarantee is still fully available."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(user_id=2, credits=5, guaranteed=0, buffer_cap=5))).allowed
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    a = await limiter.reserve(**_req(user_id=1, credits=10, guaranteed=10, buffer_cap=5))
    assert (a.allowed, a.from_reservation, a.from_buffer) == (True, 10, 0)


async def test_daily_subcap_denies_without_mutation(valkey_async):
    """A per-day sub-cap of 5: 3 ok, another 3 denied, counters show only the first 3."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(credits=3, daily_subcap=5))).allowed
    denied = await limiter.reserve(**_req(credits=3, daily_subcap=5))
    assert (denied.allowed, denied.reason) == (False, "daily_subcap_exceeded")
    assert await _n(valkey_async, keys.daily_counter(1, "20260203")) == 3
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 3


async def test_override_is_checked_but_not_debited_at_reserve(valkey_async):
    """Override cap 5 with 4 already used: 3 more is denied (best-effort check); a pass debits nothing."""
    limiter = QuotaLimiter(valkey_async)
    rule = OverrideRule(id=9, cap_credits=5, user_id=1)
    await valkey_async.set(keys.override_counter(9, 7), 4)
    denied = await limiter.reserve(**_req(credits=3, overrides=(rule,)))
    assert (denied.allowed, denied.reason) == (False, "override_exceeded")
    assert await valkey_async.get(keys.user_counter(1, 7)) is None
    ok = await limiter.reserve(**_req(credits=1, overrides=(rule,)))
    assert ok.allowed and await _n(valkey_async, keys.override_counter(9, 7)) == 4


async def test_concurrent_reserves_never_over_lease_the_buffer(valkey_async):
    """30 users race 3 credits each against a 50-credit buffer: exactly 16 win, 48 leased, no overshoot."""
    limiter = QuotaLimiter(valkey_async)
    results = await asyncio.gather(
        *[limiter.reserve(**_req(user_id=100 + i, credits=3, guaranteed=0, buffer_cap=50)) for i in range(30)]
    )
    assert sum(r.allowed for r in results) == 16
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 48
    assert all(r.reason == "quota_exhausted" for r in results if not r.allowed)


async def test_reservation_record_carries_cycle_ttl_and_is_indexed(valkey_async):
    """The record stores its own cycle + counter keys (Bug B) and is in the expiry index."""
    d = await QuotaLimiter(valkey_async, clock=lambda: 1000.0).reserve(**_req(credits=6, guaranteed=4))
    record = json.loads(await valkey_async.get(keys.reservation(d.reservation_id)))
    assert (record["cycle"], record["from_resv"], record["from_buf"]) == ("7", 4, 2)
    assert record["user_key"] == keys.user_counter(1, 7) and record["buf_key"] == keys.buffer_counter(4, 7)
    assert 0 < await valkey_async.ttl(keys.reservation(d.reservation_id)) <= QuotaLimiter.RECORD_TTL_S
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) == 4600.0


async def test_script_cache_flush_is_recovered_transparently(valkey_async):
    """SCRIPT FLUSH (or a Valkey restart) must not turn every request into a fail-open."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req())).degraded is False
    await valkey_async.script_flush()
    again = await limiter.reserve(**_req(user_id=2))
    assert again.allowed and again.degraded is False and again.reservation_id


async def test_non_positive_credits_are_clamped_to_one(valkey_async):
    """A zero-credit reserve would be free; it is clamped to 1."""
    d = await QuotaLimiter(valkey_async).reserve(**_req(credits=0))
    assert d.from_reservation + d.from_buffer == 1
```

`tests/unit/quota/test_lease_wrapper.py`:

```python
"""QuotaLimiter wrapper behaviour with a scripted fake client: args, fail-open, NOSCRIPT retry."""

import pytest
from redis.exceptions import NoScriptError

from shared.quota import keys
from shared.quota.lease import QuotaLimiter
from shared.quota.types import OverrideRule


class FakeValkey:
    """Scripted async client: evalsha pops responses (exceptions are raised)."""

    def __init__(self, *responses):
        self.responses, self.calls, self.loads = list(responses), [], 0

    async def script_load(self, script):
        self.loads += 1
        return f"sha{self.loads}"

    async def evalsha(self, sha, numkeys, *args):
        self.calls.append((sha, numkeys, args))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _kw(**over):
    """Default reserve() kwargs."""
    return {"user_id": 1, "team_id": 2, "cycle": 3, "day": "20260203", "credits": 5, "guaranteed": 10,
            "buffer_cap": 7, "daily_subcap": None, "overrides": (), **over}  # fmt: skip


async def test_allowed_result_is_parsed_and_args_are_positioned():
    """Keys: user, buffer, day, resv, index, overrides...; ARGV carries caps after the 10 fixed args."""
    fake = FakeValkey([1, "", 4, 1])
    rule = OverrideRule(id=8, cap_credits=99, user_id=1)
    d = await QuotaLimiter(fake, clock=lambda: 50.0).reserve(**_kw(overrides=(rule,), daily_subcap=6))
    assert (d.allowed, d.from_reservation, d.from_buffer, d.degraded) == (True, 4, 1, False)
    _sha, numkeys, args = fake.calls[0]
    assert numkeys == 6
    assert args[:2] == (keys.user_counter(1, 3), keys.buffer_counter(2, 3))
    assert args[2] == keys.daily_counter(1, "20260203") and args[4] == keys.RESERVATION_INDEX
    assert args[5] == keys.override_counter(8, 3)
    argv = args[6:]
    assert argv[:4] == ("5", "10", "7", "6") and argv[9] == "1" and argv[-1] == "99"


async def test_denied_result_with_bytes_reason_is_decoded():
    """A client without decode_responses returns bytes; the reason must still be a str."""
    d = await QuotaLimiter(FakeValkey([0, b"quota_exhausted", 0, 0])).reserve(**_kw())
    assert (d.allowed, d.reason, d.reservation_id) == (False, "quota_exhausted", None)


@pytest.mark.parametrize("bad", [ConnectionError("down"), None, [1], "nope"])
async def test_any_failure_or_malformed_reply_fails_open(bad, caplog):
    """D10: outage / garbage => allowed + degraded, never an exception."""
    with caplog.at_level("WARNING"):
        d = await QuotaLimiter(FakeValkey(bad)).reserve(**_kw())
    assert (d.allowed, d.degraded, d.reservation_id) == (True, True, None)


async def test_noscript_reloads_and_retries_once():
    """A NOSCRIPT reply triggers one script_load + retry, then succeeds."""
    fake = FakeValkey(NoScriptError("NOSCRIPT"), [1, "", 5, 0])
    d = await QuotaLimiter(fake).reserve(**_kw())
    assert d.allowed and not d.degraded and fake.loads == 2 and len(fake.calls) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/quota/test_lease_wrapper.py tests/integration/quota/test_lease_reserve.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.lease`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/lease.py`:

```python
"""Atomic Valkey lease/reconcile for the pooled quota (spec §6, D4, D8).

``reserve`` debits a user's guaranteed reservation first, leases the remainder from the team's
shared buffer (first-come, NO preemption of anyone's reservation), else denies. Every limit is
checked read-only BEFORE any counter is mutated (so a denied request leaves no residue), and
the stored reservation record carries the counter key names and cycle so reconcile/release never
read a clock. Counter keys are derived inside the script from stored names, so this requires a
standalone/sentinel Valkey (not cluster) -- the same constraint the existing limiter has.
Any Valkey failure fails OPEN (D10): the request is allowed and logged.
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from redis.exceptions import NoScriptError

from shared.quota import keys
from shared.quota.types import OverrideRule, QuotaDecision

logger = logging.getLogger(__name__)

LUA_RESERVE = """
local credits = tonumber(ARGV[1])
local guaranteed = tonumber(ARGV[2])
local buffer_cap = tonumber(ARGV[3])
local daily_cap = tonumber(ARGV[4])
local rid = ARGV[5]
local now = tonumber(ARGV[6])
local resv_ttl = tonumber(ARGV[7])
local record_ttl = tonumber(ARGV[8])
local counter_ttl = tonumber(ARGV[9])
local n = tonumber(ARGV[10])
local cycle = ARGV[11]

-- Phase 1: read-only checks. Nothing is mutated until EVERY limit has passed.
if daily_cap > 0 then
    local used = tonumber(redis.call("GET", KEYS[3])) or 0
    if used + credits > daily_cap then return {0, "daily_subcap_exceeded", 0, 0} end
end
for i = 1, n do
    local cap = tonumber(ARGV[11 + i])
    if cap > 0 then
        local used = tonumber(redis.call("GET", KEYS[5 + i])) or 0
        if used + credits > cap then return {0, "override_exceeded", 0, 0} end
    end
end
local user_used = tonumber(redis.call("GET", KEYS[1])) or 0
local headroom = guaranteed - user_used
if headroom < 0 then headroom = 0 end
local from_resv = credits
if from_resv > headroom then from_resv = headroom end
local from_buf = credits - from_resv
if from_buf > 0 then
    local buf_used = tonumber(redis.call("GET", KEYS[2])) or 0
    if buf_used + from_buf > buffer_cap then return {0, "quota_exhausted", 0, 0} end
end

-- Phase 2: every check passed; apply all mutations.
if from_resv > 0 then
    redis.call("INCRBY", KEYS[1], from_resv)
    redis.call("EXPIRE", KEYS[1], counter_ttl)
end
if from_buf > 0 then
    redis.call("INCRBY", KEYS[2], from_buf)
    redis.call("EXPIRE", KEYS[2], counter_ttl)
end
local daily_on = 0
if daily_cap > 0 then
    daily_on = 1
    redis.call("INCRBY", KEYS[3], credits)
    redis.call("EXPIRE", KEYS[3], 172800)
end
redis.call("SET", KEYS[4], cjson.encode({
    user_key = KEYS[1], buf_key = KEYS[2], day_key = KEYS[3], daily_on = daily_on,
    cycle = cycle, credits = credits, from_resv = from_resv, from_buf = from_buf,
    guaranteed = guaranteed, buffer_cap = buffer_cap
}), "EX", record_ttl)
redis.call("ZADD", KEYS[5], now + resv_ttl, rid)
return {1, "", from_resv, from_buf}
"""


def _text(value: Any) -> str:
    """Decode a Valkey reply element to ``str`` (bytes when ``decode_responses`` is off)."""
    return value.decode() if isinstance(value, bytes) else str(value)


class QuotaLimiter:
    """Atomic reserve (and, from Task 22, reconcile/release/sweep) over Valkey Lua scripts."""

    RESERVATION_TTL_S = 3600
    RECORD_TTL_S = 4200
    COUNTER_TTL_S = 35 * 86400
    _SCRIPTS: dict[str, str] = {"reserve": LUA_RESERVE}

    def __init__(self, valkey: Any, *, clock: Callable[[], float] = time.time) -> None:
        """Bind the async Valkey client and an injectable clock (tests pass a fixed one)."""
        self.valkey = valkey
        self._clock = clock
        self._shas: dict[str, str] = {}

    async def _run(self, name: str, script_keys: list[str], args: list[str]) -> Any:
        """EVALSHA ``name``; on NOSCRIPT (script cache flushed/restarted) reload once and retry."""
        script = self._SCRIPTS[name]
        sha = self._shas.get(name)
        if sha is None:
            sha = self._shas[name] = await self.valkey.script_load(script)
        try:
            return await self.valkey.evalsha(sha, len(script_keys), *script_keys, *args)
        except NoScriptError:
            sha = self._shas[name] = await self.valkey.script_load(script)
            return await self.valkey.evalsha(sha, len(script_keys), *script_keys, *args)

    async def reserve(
        self,
        *,
        user_id: int,
        team_id: int,
        cycle: int,
        day: str,
        credits: int,
        guaranteed: int,
        buffer_cap: int,
        daily_subcap: int | None,
        overrides: Sequence[OverrideRule],
    ) -> QuotaDecision:
        """Atomically reserve ``credits`` (reservation -> buffer -> deny); fail open on error."""
        credits = max(1, int(credits))
        rid = uuid.uuid4().hex
        rules = list(overrides)
        script_keys = [
            keys.user_counter(user_id, cycle),
            keys.buffer_counter(team_id, cycle),
            keys.daily_counter(user_id, day),
            keys.reservation(rid),
            keys.RESERVATION_INDEX,
            *[keys.override_counter(r.id, cycle) for r in rules],
        ]
        args = [
            credits, guaranteed, buffer_cap, daily_subcap or 0, rid, self._clock(),
            self.RESERVATION_TTL_S, self.RECORD_TTL_S, self.COUNTER_TTL_S, len(rules), cycle,
            *[r.cap_credits for r in rules],
        ]  # fmt: skip
        try:
            result = await self._run("reserve", script_keys, [str(a) for a in args])
            allowed = bool(int(result[0]))
            reason = _text(result[1]) or None
            from_resv, from_buf = int(result[2]), int(result[3])
        except Exception as exc:  # noqa: BLE001 -- D10: a dead/odd Valkey must never block a request
            logger.warning("quota reserve failed open (%s: %s)", type(exc).__name__, exc)
            return QuotaDecision(allowed=True, degraded=True)
        if not allowed:
            return QuotaDecision(allowed=False, reason=reason)
        return QuotaDecision(
            allowed=True, reservation_id=rid, from_reservation=from_resv, from_buffer=from_buf
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/quota/test_lease_wrapper.py -v --no-cov && make test-valkey`
Expected: PASS. (`test_any_failure_or_malformed_reply_fails_open` covers `None`, `[1]` (IndexError) and `"nope"` via the broad except.)

- [ ] **Step 5: Commit**

```bash
git add shared/quota/lease.py tests/integration/quota/test_lease_reserve.py tests/unit/quota/test_lease_wrapper.py
git commit -m "feat(quota): atomic lease-from-buffer reserve Lua (check-then-mutate, no preemption, fail-open)"
git push
```
