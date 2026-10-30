
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
