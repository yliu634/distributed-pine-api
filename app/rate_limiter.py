from __future__ import annotations

import os
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import NoScriptError

from .config import APIKeyLimits


@dataclass
class RateLimitOutcome:
    allowed: bool
    rpm_usage: int
    input_tokens_usage: int
    output_tokens_usage: int
    limit_flag: int


_LUA_SCRIPT = """
local rpm_zset = KEYS[1]
local rpm_hash = KEYS[2]
local rpm_total = KEYS[3]
local input_zset = KEYS[4]
local input_hash = KEYS[5]
local input_total = KEYS[6]
local output_zset = KEYS[7]
local output_hash = KEYS[8]
local output_total = KEYS[9]

local window_seconds = tonumber(ARGV[1])
local now_ms = tonumber(ARGV[2])
local rpm_limit = tonumber(ARGV[3])
local input_limit = tonumber(ARGV[4])
local output_limit = tonumber(ARGV[5])
local input_tokens = tonumber(ARGV[6])
local output_tokens = tonumber(ARGV[7])
local ttl = tonumber(ARGV[8])

local bucket = math.floor(now_ms / 1000)
local oldest_bucket = bucket - window_seconds + 1

local function ensure_total(key)
    local current = redis.call('GET', key)
    if not current then
        redis.call('SET', key, '0')
        return 0
    end
    return tonumber(current)
end

local function prune(zset_key, hash_key, total_key)
    local expired = redis.call('ZRANGEBYSCORE', zset_key, 0, oldest_bucket - 1)
    if #expired > 0 then
        for _, bucket_id in ipairs(expired) do
            local amount = redis.call('HGET', hash_key, bucket_id)
            if amount then
                redis.call('HDEL', hash_key, bucket_id)
                redis.call('INCRBY', total_key, -tonumber(amount))
            end
        end
        redis.call('ZREMRANGEBYSCORE', zset_key, 0, oldest_bucket - 1)
    end
    return ensure_total(total_key)
end

local current_rpm = prune(rpm_zset, rpm_hash, rpm_total)
local current_input = prune(input_zset, input_hash, input_total)
local current_output = prune(output_zset, output_hash, output_total)

local limit_hit = 0
if rpm_limit > 0 and current_rpm + 1 > rpm_limit then
    limit_hit = 1
elseif input_limit > 0 and current_input + input_tokens > input_limit then
    limit_hit = 2
elseif output_limit > 0 and current_output + output_tokens > output_limit then
    limit_hit = 3
end

if limit_hit == 0 then
    redis.call('ZADD', rpm_zset, bucket, bucket)
    redis.call('HINCRBY', rpm_hash, bucket, 1)
    redis.call('INCRBY', rpm_total, 1)
    redis.call('ZADD', input_zset, bucket, bucket)
    redis.call('HINCRBY', input_hash, bucket, input_tokens)
    redis.call('INCRBY', input_total, input_tokens)
    redis.call('ZADD', output_zset, bucket, bucket)
    redis.call('HINCRBY', output_hash, bucket, output_tokens)
    redis.call('INCRBY', output_total, output_tokens)
    current_rpm = current_rpm + 1
    current_input = current_input + input_tokens
    current_output = current_output + output_tokens
    redis.call('EXPIRE', rpm_zset, ttl); redis.call('EXPIRE', rpm_hash, ttl); redis.call('EXPIRE', rpm_total, ttl)
    redis.call('EXPIRE', input_zset, ttl); redis.call('EXPIRE', input_hash, ttl); redis.call('EXPIRE', input_total, ttl)
    redis.call('EXPIRE', output_zset, ttl); redis.call('EXPIRE', output_hash, ttl); redis.call('EXPIRE', output_total, ttl)
end

return {limit_hit == 0 and 1 or 0, current_rpm, current_input, current_output, limit_hit}
"""


class RateLimiter:
    def __init__(self, redis_url: str, window_seconds: int = 60) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._window_seconds = window_seconds
        self._script_sha: str | None = None
        self._ttl_seconds = window_seconds + 5
        self._bypass = False # os.getenv("BYPASS_LIMITER", "0") == "1"

    async def initialize(self) -> None:
        self._script_sha = await self._redis.script_load(_LUA_SCRIPT)

    async def close(self) -> None:
        await self._redis.aclose()

    async def _eval_script(self, *args, keys):
        if not self._script_sha:
            await self.initialize()
        try:
            return await self._redis.evalsha(self._script_sha, len(keys), *keys, *args)
        except NoScriptError:
            await self.initialize()
            return await self._redis.evalsha(self._script_sha, len(keys), *keys, *args)

    async def check_and_consume(
        self,
        api_key: str,
        limits: APIKeyLimits,
        input_tokens: int,
        output_tokens: int,
    ) -> RateLimitOutcome:
        if self._bypass:
            return RateLimitOutcome(
                allowed=True,
                rpm_usage=0,
                input_tokens_usage=0,
                output_tokens_usage=0,
                limit_flag=0,
            )
        now_ms = int(time.time() * 1000)
        prefix = f"rl:{api_key}"
        keys = [
            f"{prefix}:rpm:z",
            f"{prefix}:rpm:h",
            f"{prefix}:rpm:total",
            f"{prefix}:input:z",
            f"{prefix}:input:h",
            f"{prefix}:input:total",
            f"{prefix}:output:z",
            f"{prefix}:output:h",
            f"{prefix}:output:total",
        ]
        args = [
            self._window_seconds,
            now_ms,
            limits.rpm,
            limits.input_tpm,
            limits.output_tpm,
            max(0, input_tokens),
            max(0, output_tokens),
            self._ttl_seconds,
        ]
        allowed, rpm_usage, input_usage, output_usage, limit_flag = await self._eval_script(
            *args, keys=keys
        )
        return RateLimitOutcome(
            allowed=bool(allowed),
            rpm_usage=int(rpm_usage),
            input_tokens_usage=int(input_usage),
            output_tokens_usage=int(output_usage),
            limit_flag=int(limit_flag),
        )
