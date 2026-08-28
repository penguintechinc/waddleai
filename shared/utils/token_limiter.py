"""Valkey-backed atomic token/budget rate limiting.

Replaces in-memory, thread-locked per-minute counters with stateless Valkey
operations using Lua scripts for atomicity. Supports:
- TPM (tokens per minute) limiting
- Monthly token budget limiting
- Monthly USD budget limiting

All operations are atomic at the Valkey layer via Lua scripts.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KeyLimits:
    """Rate limit configuration for a virtual key."""

    tpm_limit: int | None
    monthly_token_limit: int | None
    monthly_usd_limit: int | None


@dataclass(slots=True)
class GateDecision:
    """Result of a reserve/check operation."""

    allowed: bool
    reason: str | None = None  # tpm_exceeded | monthly_tokens_exceeded | monthly_usd_exceeded
    reservation_id: str | None = None  # UUID for reconciliation


class TokenLimiter:
    """Atomic token/budget gate using Valkey Lua scripts."""

    # Lua script for TPM + budget check and reservation
    # Returns [allowed (0/1), reason_or_null, reservation_id]
    LUA_RESERVE = """
    local vkey_id = ARGV[1]
    local estimated_tokens = tonumber(ARGV[2])
    local estimated_usd = tonumber(ARGV[3])
    local tpm_limit = tonumber(ARGV[4])
    local monthly_token_limit = tonumber(ARGV[5])
    local monthly_usd_limit = tonumber(ARGV[6])
    local reservation_id = ARGV[7]
    local now_minute = ARGV[8]
    local now_month = ARGV[9]

    -- Check TPM limit
    if tpm_limit ~= nil and tpm_limit > 0 then
        local tpm_key = "waddleai:tpm:" .. vkey_id .. ":" .. now_minute
        local current_tpm = tonumber(redis.call("GET", tpm_key)) or 0
        if current_tpm + estimated_tokens > tpm_limit then
            return {0, "tpm_exceeded", cjson.null}
        end
        redis.call("INCRBY", tpm_key, estimated_tokens)
        redis.call("EXPIRE", tpm_key, 60)
    end

    -- Check monthly token budget
    if monthly_token_limit ~= nil and monthly_token_limit > 0 then
        local tok_key = "waddleai:budget:tok:" .. vkey_id .. ":" .. now_month
        local current_tok = tonumber(redis.call("GET", tok_key)) or 0
        if current_tok + estimated_tokens > monthly_token_limit then
            return {0, "monthly_tokens_exceeded", cjson.null}
        end
        redis.call("INCRBY", tok_key, estimated_tokens)
        redis.call("EXPIRE", tok_key, 2592000)  -- 30 days
    end

    -- Check monthly USD budget
    if monthly_usd_limit ~= nil and monthly_usd_limit > 0 then
        local usd_key = "waddleai:budget:usd:" .. vkey_id .. ":" .. now_month
        local current_usd = tonumber(redis.call("GET", usd_key)) or 0
        if current_usd + estimated_usd > monthly_usd_limit then
            return {0, "monthly_usd_exceeded", cjson.null}
        end
        redis.call("INCRBYFLOAT", usd_key, estimated_usd)
        redis.call("EXPIRE", usd_key, 2592000)  -- 30 days
    end

    -- All checks passed; store reservation for reconcile
    local resv_key = "waddleai:resv:" .. reservation_id
    redis.call("SET", resv_key, cjson.encode({
        vkey_id = vkey_id,
        estimated_tokens = estimated_tokens,
        estimated_usd = estimated_usd,
        created_at = redis.call("TIME")[1]
    }), "EX", 3600)  -- 1 hour TTL

    return {1, cjson.null, reservation_id}
    """

    # Lua script for reconciliation (adjust reserved estimate with actual usage)
    LUA_RECONCILE = """
    local reservation_id = ARGV[1]
    local actual_tokens = tonumber(ARGV[2])
    local actual_usd = tonumber(ARGV[3])

    local resv_key = "waddleai:resv:" .. reservation_id
    local resv_data = redis.call("GET", resv_key)
    if not resv_data then
        return {0, "reservation_not_found"}
    end

    local resv = cjson.decode(resv_data)
    local token_delta = actual_tokens - resv.estimated_tokens
    local usd_delta = actual_usd - resv.estimated_usd

    -- Adjust TPM window if tokens differ
    if token_delta ~= 0 then
        local now_minute = ARGV[4]
        local tpm_key = "waddleai:tpm:" .. resv.vkey_id .. ":" .. now_minute
        redis.call("INCRBY", tpm_key, token_delta)
    end

    -- Adjust token budget if tokens differ
    if token_delta ~= 0 then
        local now_month = ARGV[5]
        local tok_key = "waddleai:budget:tok:" .. resv.vkey_id .. ":" .. now_month
        redis.call("INCRBY", tok_key, token_delta)
    end

    -- Adjust USD budget if cost differs
    if usd_delta ~= 0 then
        local now_month = ARGV[5]
        local usd_key = "waddleai:budget:usd:" .. resv.vkey_id .. ":" .. now_month
        redis.call("INCRBYFLOAT", usd_key, usd_delta)
    end

    -- Clean up reservation
    redis.call("DEL", resv_key)
    return {1, cjson.null}
    """

    def __init__(self, valkey, features) -> None:
        """Initialize TokenLimiter.

        Args:
            valkey: redis.asyncio.Redis or similar async client
            features: Feature flag helper (with is_feature_enabled method)

        """
        self.valkey = valkey
        self.features = features
        self._reserve_sha: str | None = None
        self._reconcile_sha: str | None = None

    async def _load_scripts(self) -> None:
        """Load and cache Lua scripts."""
        if self._reserve_sha is None:
            try:
                self._reserve_sha = await self.valkey.script_load(self.LUA_RESERVE)
            except Exception as e:
                logger.warning("Failed to load reserve script: %s", e)

        if self._reconcile_sha is None:
            try:
                self._reconcile_sha = await self.valkey.script_load(self.LUA_RECONCILE)
            except Exception as e:
                logger.warning("Failed to load reconcile script: %s", e)

    async def reserve(
        self,
        vkey_id: int,
        estimated_tokens: int,
        estimated_usd: float,
        limits: KeyLimits,
    ) -> GateDecision:
        """Reserve tokens/budget for a request.

        Args:
            vkey_id: Virtual key ID
            estimated_tokens: Estimated token usage
            estimated_usd: Estimated cost in micro-USD
            limits: KeyLimits with tpm/monthly limits

        Returns:
            GateDecision with allowed/reason/reservation_id

        """
        # Load scripts on first use
        await self._load_scripts()

        # Check if feature flag is enabled; if not, always allow
        if not self.features.is_feature_enabled(
            "waddleai.native_rate_limit", distinct_id=str(vkey_id)
        ):
            return GateDecision(allowed=True, reason=None, reservation_id=None)

        # If all limits are None, no gating needed
        if (
            limits.tpm_limit is None
            and limits.monthly_token_limit is None
            and limits.monthly_usd_limit is None
        ):
            return GateDecision(allowed=True, reason=None, reservation_id=None)

        # Generate reservation ID for later reconciliation
        reservation_id = str(uuid.uuid4())

        # Get current time components
        now = datetime.utcnow()
        now_minute = now.strftime("%Y%m%d%H%M")
        now_month = now.strftime("%Y%m")

        try:
            # Execute Lua script atomically
            result = await self.valkey.evalsha(
                self._reserve_sha,
                0,  # numkeys=0 (all keys passed via ARGV)
                str(vkey_id),
                str(estimated_tokens),
                str(estimated_usd),
                str(limits.tpm_limit or 0),
                str(limits.monthly_token_limit or 0),
                str(limits.monthly_usd_limit or 0),
                reservation_id,
                now_minute,
                now_month,
            )

            if result is None or len(result) < 2:
                logger.error("Invalid Lua script response: %s", result)
                return GateDecision(allowed=True, reason=None, reservation_id=None)

            allowed = bool(result[0])
            reason = result[1] if not allowed else None
            resv_id = result[2] if allowed else None

            return GateDecision(allowed=allowed, reason=reason, reservation_id=resv_id)

        except Exception as e:
            logger.error("Error in reserve: %s", e)
            # Fail open: allow the request if there's an error
            return GateDecision(allowed=True, reason=None, reservation_id=None)

    async def reconcile(self, reservation_id: str, actual_tokens: int, actual_usd: float) -> None:
        """Reconcile actual usage against reserved estimate.

        Args:
            reservation_id: UUID from reserve() response
            actual_tokens: Actual tokens used
            actual_usd: Actual cost in micro-USD

        """
        # Load scripts on first use
        await self._load_scripts()

        if self._reconcile_sha is None:
            logger.warning("Reconcile script not loaded, skipping reconciliation")
            return

        # Get current time components
        now = datetime.utcnow()
        now_minute = now.strftime("%Y%m%d%H%M")
        now_month = now.strftime("%Y%m")

        try:
            await self.valkey.evalsha(
                self._reconcile_sha,
                0,  # numkeys=0
                reservation_id,
                str(actual_tokens),
                str(actual_usd),
                now_minute,
                now_month,
            )
        except Exception as e:
            logger.warning("Error reconciling reservation %s: %s", reservation_id, e)


def create_token_limiter(valkey, features) -> TokenLimiter:
    """Factory function to create a TokenLimiter instance."""
    return TokenLimiter(valkey, features)
