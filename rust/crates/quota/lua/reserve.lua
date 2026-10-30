
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
