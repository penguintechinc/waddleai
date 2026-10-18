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
