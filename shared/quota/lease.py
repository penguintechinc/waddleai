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
from shared.quota.types import OverrideRule, QuotaDecision, ReconcileResult

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

-- Consume the record FIRST: a mid-script error then loses a refund (user-adverse, recoverable)
-- instead of leaving the record to be refunded again (a double-refund = quota bypass).
redis.call("DEL", KEYS[1])
redis.call("ZREM", KEYS[2], rid)

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
        redis.call("EXPIRE", r.day_key, 172800)
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

return {1, from_resv, from_buf, overage, table.concat(over_ids, ",")}
"""


def _text(value: Any) -> str:
    """Decode a Valkey reply element to ``str`` (bytes when ``decode_responses`` is off)."""
    return value.decode() if isinstance(value, bytes) else str(value)


class QuotaLimiter:
    """Atomic reserve, reconcile, release and expiry sweep over Valkey Lua scripts."""

    RESERVATION_TTL_S = 3600
    RECORD_TTL_S = 4200
    COUNTER_TTL_S = 35 * 86400
    _SCRIPTS: dict[str, str] = {"reserve": LUA_RESERVE, "reconcile": LUA_RECONCILE}

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
            *[r.cap_credits or 0 for r in rules],  # 0 = unlimited; never let None reach Lua
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

    async def reconcile(
        self,
        reservation_id: str,
        actual_credits: int,
        override_caps: Sequence[tuple[int, int]] = (),
    ) -> ReconcileResult:
        """Settle a reservation against actual credits using ITS stored cycle/keys; never raises.

        Cap value 0 (or None) means unlimited. Missing record (settled/swept) -> not applied.
        """
        pairs: list[str] = []
        for oid, cap in override_caps:
            if oid is None:  # never coerce an id: it would charge the wrong counter
                logger.warning("quota reconcile rejected a None override id")
                return ReconcileResult(applied=False)
            pairs += [str(oid), str(cap or 0)]
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
        """Refund up to ``batch`` reservations whose index score is in the past; return the count.

        Backstop for reservations never reconciled/released, including a lost reserve reply.
        """
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
