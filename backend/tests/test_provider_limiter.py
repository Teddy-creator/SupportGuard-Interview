from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from supportguard.providers.limiter import ProviderLimitError, RedisProviderLimiter


def _redis_url() -> str | None:
    return os.getenv("TEST_WORKER_REDIS_URL")


async def _cleanup(*keys: str) -> None:
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        return
    redis = Redis.from_url(url, decode_responses=False)
    try:
        await redis.delete(*keys)
    finally:
        await redis.aclose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_shared_provider_limiter_caps_three_contenders_at_two() -> None:
    url = _redis_url()
    if not url:
        pytest.skip("TEST_WORKER_REDIS_URL is required")
    redis = Redis.from_url(url, decode_responses=False)
    namespace = f"supportguard:test:provider:{uuid4().hex}"
    limiter = RedisProviderLimiter(
        redis,
        namespace=namespace,
        max_inflight=2,
        rpm_capacity=10,
        acquire_timeout_seconds=1,
    )
    first = await limiter.acquire()
    second = await limiter.acquire()
    third_task = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.08)
    assert not third_task.done()
    assert await redis.zcard(limiter.inflight_key) == 2
    await limiter.release(first)
    third = await asyncio.wait_for(third_task, timeout=1)
    assert await redis.zcard(limiter.inflight_key) == 2
    await limiter.release(second)
    await limiter.release(third)
    await _cleanup(
        limiter.inflight_key,
        limiter.rpm_key,
        limiter.waiters_key,
        limiter.sequence_key,
    )
    await redis.aclose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_provider_rpm_bucket_and_crash_lease_are_bounded() -> None:
    url = _redis_url()
    if not url:
        pytest.skip("TEST_WORKER_REDIS_URL is required")
    redis = Redis.from_url(url, decode_responses=False)
    namespace = f"supportguard:test:provider:{uuid4().hex}"
    limiter = RedisProviderLimiter(
        redis,
        namespace=namespace,
        max_inflight=2,
        rpm_capacity=2,
        lease_ms=100,
        acquire_timeout_seconds=0.12,
    )
    first = await limiter.acquire()
    await limiter.release(first)
    second = await limiter.acquire()
    await limiter.release(second)
    with pytest.raises(ProviderLimitError, match="provider_rpm_exhausted"):
        await limiter.acquire()
    await _cleanup(
        limiter.inflight_key,
        limiter.rpm_key,
        limiter.waiters_key,
        limiter.sequence_key,
    )

    crash_limiter = RedisProviderLimiter(
        redis,
        namespace=f"{namespace}:crash",
        max_inflight=1,
        rpm_capacity=10,
        lease_ms=80,
        acquire_timeout_seconds=0.5,
    )
    await crash_limiter.acquire()  # simulate process death before release
    await asyncio.sleep(0.1)
    recovered = await crash_limiter.acquire()
    await crash_limiter.release(recovered)
    await _cleanup(
        crash_limiter.inflight_key,
        crash_limiter.rpm_key,
        crash_limiter.waiters_key,
        crash_limiter.sequence_key,
    )
    await redis.aclose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_cancelled_provider_waiter_does_not_block_fifo_head() -> None:
    url = _redis_url()
    if not url:
        pytest.skip("TEST_WORKER_REDIS_URL is required")
    redis = Redis.from_url(url, decode_responses=False)
    namespace = f"supportguard:test:provider:{uuid4().hex}"
    limiter = RedisProviderLimiter(
        redis,
        namespace=namespace,
        max_inflight=1,
        rpm_capacity=10,
        acquire_timeout_seconds=1,
    )
    active = await limiter.acquire()
    cancelled = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.04)
    successor = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.04)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await limiter.release(active)
    acquired = await asyncio.wait_for(successor, timeout=1)
    assert await redis.zcard(limiter.waiters_key) == 0
    await limiter.release(acquired)
    await _cleanup(
        limiter.inflight_key,
        limiter.rpm_key,
        limiter.waiters_key,
        limiter.sequence_key,
    )
    await redis.aclose()
