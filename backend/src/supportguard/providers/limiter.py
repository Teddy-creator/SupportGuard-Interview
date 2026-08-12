from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

_ACQUIRE = """
local inflight = KEYS[1]
local rpm = KEYS[2]
local waiters = KEYS[3]
local sequence = KEYS[4]
local owner = ARGV[1]
local max_inflight = tonumber(ARGV[2])
local rpm_capacity = tonumber(ARGV[3])
local rpm_window = tonumber(ARGV[4])
local lease_ms = tonumber(ARGV[5])
local waiter_ttl_ms = tonumber(ARGV[6])
local clock = redis.call('TIME')
local now = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local lease_until = now + lease_ms
redis.call('ZREMRANGEBYSCORE', inflight, '-inf', now)
redis.call('ZREMRANGEBYSCORE', rpm, '-inf', now - rpm_window)
redis.call('ZREMRANGEBYSCORE', waiters, '-inf', now - waiter_ttl_ms)
if not redis.call('ZSCORE', waiters, owner) then
  local ordinal = redis.call('INCR', sequence)
  redis.call('ZADD', waiters, now + (ordinal / 1000000), owner)
end
local head = redis.call('ZRANGE', waiters, 0, 0)[1]
if head ~= owner then return {0, 3, now} end
if redis.call('ZCARD', inflight) >= max_inflight then return {0, 1, now} end
if redis.call('ZCARD', rpm) >= rpm_capacity then return {0, 2, now} end
redis.call('ZREM', waiters, owner)
redis.call('ZADD', inflight, lease_until, owner)
redis.call('ZADD', rpm, now, owner)
redis.call('PEXPIRE', inflight, math.ceil(lease_until - now) + 1000)
redis.call('PEXPIRE', rpm, rpm_window + 1000)
redis.call('PEXPIRE', waiters, waiter_ttl_ms + 1000)
redis.call('PEXPIRE', sequence, waiter_ttl_ms + 1000)
return {1, 0, now}
"""

_RELEASE = "return redis.call('ZREM', KEYS[1], ARGV[1])"
_CANCEL_WAITER = "return redis.call('ZREM', KEYS[1], ARGV[1])"


class ProviderLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderPermit:
    owner: str
    acquired_at_ms: int


class RedisProviderLimiter:
    """Shared fail-closed concurrency semaphore and RPM token bucket."""

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = "supportguard:provider-limiter:v1",
        max_inflight: int = 2,
        rpm_capacity: int = 60,
        lease_ms: int = 40_000,
        acquire_timeout_seconds: float = 5.0,
    ) -> None:
        self.redis = redis
        self.inflight_key = f"{namespace}:inflight"
        self.rpm_key = f"{namespace}:rpm"
        self.waiters_key = f"{namespace}:waiters"
        self.sequence_key = f"{namespace}:waiter-sequence"
        self.max_inflight = max_inflight
        self.rpm_capacity = rpm_capacity
        self.lease_ms = lease_ms
        self.acquire_timeout_seconds = acquire_timeout_seconds

    async def acquire(self) -> ProviderPermit:
        deadline = monotonic() + self.acquire_timeout_seconds
        owner = f"permit:{uuid4().hex}"
        try:
            while True:
                try:
                    evaluate: Any = self.redis.eval
                    result = await evaluate(
                        _ACQUIRE,
                        4,
                        self.inflight_key,
                        self.rpm_key,
                        self.waiters_key,
                        self.sequence_key,
                        owner,
                        str(self.max_inflight),
                        str(self.rpm_capacity),
                        "60000",
                        str(self.lease_ms),
                        str(int(self.acquire_timeout_seconds * 1000) + 1000),
                    )
                except Exception as exc:
                    raise ProviderLimitError("provider_limiter_unavailable") from exc
                if int(result[0]) == 1:
                    return ProviderPermit(owner=owner, acquired_at_ms=int(result[2]))
                if monotonic() >= deadline:
                    reason = (
                        "provider_rpm_exhausted"
                        if int(result[1]) == 2
                        else "provider_busy"
                    )
                    raise ProviderLimitError(reason)
                await asyncio.sleep(0.025)
        except BaseException:
            with suppress(Exception):
                evaluate = self.redis.eval
                await evaluate(_CANCEL_WAITER, 1, self.waiters_key, owner)
            raise

    async def release(self, permit: ProviderPermit) -> None:
        try:
            evaluate: Any = self.redis.eval
            await evaluate(_RELEASE, 1, self.inflight_key, permit.owner)
        except Exception as exc:
            # The bounded lease is the crash/release-failure recovery mechanism.
            raise ProviderLimitError("provider_limiter_release_unknown") from exc

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[ProviderPermit]:
        permit = await self.acquire()
        try:
            yield permit
        finally:
            await self.release(permit)
