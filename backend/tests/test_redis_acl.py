from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError

from supportguard.services.admission import BUCKET_SCRIPT


def _role_url(role: str, password: str) -> str:
    configured = os.getenv("TEST_REDIS_URL")
    if not configured:
        pytest.skip("TEST_REDIS_URL is required")
    parsed = urlsplit(configured)
    return f"redis://{role}:{password}@{parsed.hostname}:{parsed.port or 6379}{parsed.path or '/0'}"


@pytest.mark.redis
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "password", "operation"),
    [
        ("worker", "worker_dev", "xadd"),
        ("dispatcher", "dispatcher_dev", "xread"),
        ("reconciler", "reconciler_dev", "xadd"),
        ("api", "api_dev", "xadd"),
        ("worker", "worker_dev", "hset"),
    ],
)
async def test_component_redis_acl_denies_unowned_commands(
    role: str, password: str, operation: str
) -> None:
    if not os.getenv("TEST_REDIS_URL"):
        pytest.skip("TEST_REDIS_URL is required")
    redis = Redis.from_url(_role_url(role, password), decode_responses=False)
    try:
        with pytest.raises(NoPermissionError):
            if operation == "xadd":
                await redis.xadd("supportguard:test:acl", {"value": "denied"})
            elif operation == "hset":
                await redis.hset("supportguard:test:acl", mapping={"value": "denied"})
            else:
                await redis.xread({"supportguard:test:acl": "0-0"}, count=1)
    finally:
        await redis.aclose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_api_acl_allows_every_command_used_inside_rate_limit_lua() -> None:
    if not os.getenv("TEST_REDIS_URL"):
        pytest.skip("TEST_REDIS_URL is required")
    redis = Redis.from_url(_role_url("api", "api_dev"), decode_responses=False)
    try:
        result = await redis.eval(
            BUCKET_SCRIPT,
            1,
            "supportguard:test:acl:api-lua",
            "10",
        )
        assert int(result) == 1
    finally:
        await redis.aclose()
