
    local vkey_id = ARGV[1]
    local estimated_tokens = tonumber(ARGV[2])
    local estimated_usd = tonumber(ARGV[3])
    local tpm_limit = tonumber(ARGV[4])
    local monthly_token_limit = tonumber(ARGV[5])
    local monthly_usd_limit = tonumber(ARGV[6])
    local reservation_id = ARGV[7]
    local now_minute = ARGV[8]
    local now_month = ARGV[9]

    local tpm_key = "waddleai:tpm:" .. vkey_id .. ":" .. now_minute
    local tok_key = "waddleai:budget:tok:" .. vkey_id .. ":" .. now_month
    local usd_key = "waddleai:budget:usd:" .. vkey_id .. ":" .. now_month

    -- Phase 1: read-only checks. Nothing is mutated until EVERY limit has passed (Bug A).
    if tpm_limit > 0 then
        local current = tonumber(redis.call("GET", tpm_key)) or 0
        if current + estimated_tokens > tpm_limit then
            return {0, "tpm_exceeded", ""}
        end
    end
    if monthly_token_limit > 0 then
        local current = tonumber(redis.call("GET", tok_key)) or 0
        if current + estimated_tokens > monthly_token_limit then
            return {0, "monthly_tokens_exceeded", ""}
        end
    end
    if monthly_usd_limit > 0 then
        local current = tonumber(redis.call("GET", usd_key)) or 0
        if current + estimated_usd > monthly_usd_limit then
            return {0, "monthly_usd_exceeded", ""}
        end
    end

    -- Phase 2: every check passed; apply all mutations.
    if tpm_limit > 0 then
        redis.call("INCRBY", tpm_key, estimated_tokens)
        redis.call("EXPIRE", tpm_key, 60)
    end
    if monthly_token_limit > 0 then
        redis.call("INCRBY", tok_key, estimated_tokens)
        redis.call("EXPIRE", tok_key, 2592000)  -- 30 days
    end
    if monthly_usd_limit > 0 then
        redis.call("INCRBYFLOAT", usd_key, estimated_usd)
        redis.call("EXPIRE", usd_key, 2592000)  -- 30 days
    end

    local resv_key = "waddleai:resv:" .. reservation_id
    redis.call("SET", resv_key, cjson.encode({
        vkey_id = vkey_id,
        estimated_tokens = estimated_tokens,
        estimated_usd = estimated_usd,
        now_minute = now_minute,
        now_month = now_month,
        created_at = redis.call("TIME")[1]
    }), "EX", 3600)  -- 1 hour TTL

    return {1, "", reservation_id}
    