
    local reservation_id = ARGV[1]
    local actual_tokens = tonumber(ARGV[2])
    local actual_usd = tonumber(ARGV[3])

    local resv_key = "waddleai:resv:" .. reservation_id
    local resv_data = redis.call("GET", resv_key)
    if not resv_data then
        return {0, "reservation_not_found"}
    end

    local resv = cjson.decode(resv_data)
    -- Reservations written before now_minute/now_month were stored fall back to the
    -- reconcile-time window (ARGV[4]/[5]); the stored window always wins when present.
    local now_minute = resv.now_minute or ARGV[4]
    local now_month = resv.now_month or ARGV[5]
    local token_delta = actual_tokens - resv.estimated_tokens
    local usd_delta = actual_usd - resv.estimated_usd

    -- Adjust the window the reservation was MADE in (Bug B), never the reconcile-time clock.
    local tpm_key = "waddleai:tpm:" .. resv.vkey_id .. ":" .. now_minute
    local tok_key = "waddleai:budget:tok:" .. resv.vkey_id .. ":" .. now_month
    local usd_key = "waddleai:budget:usd:" .. resv.vkey_id .. ":" .. now_month

    -- The TPM key has a 60s TTL: only adjust it while it still exists (INCRBY would recreate
    -- it without a TTL). The budget keys are only ever created by a reserve with that limit.
    if token_delta ~= 0 and redis.call("EXISTS", tpm_key) == 1 then
        redis.call("INCRBY", tpm_key, token_delta)
    end
    if token_delta ~= 0 and redis.call("EXISTS", tok_key) == 1 then
        redis.call("INCRBY", tok_key, token_delta)
    end
    if usd_delta ~= 0 and redis.call("EXISTS", usd_key) == 1 then
        redis.call("INCRBYFLOAT", usd_key, usd_delta)
    end

    redis.call("DEL", resv_key)
    return {1, ""}
    