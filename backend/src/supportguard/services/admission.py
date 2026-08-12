from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.config import Settings
from supportguard.db.models import IdempotencyRequest, RuntimeJob
from supportguard.services.runtime_jobs import RuntimeConflict

BUCKET_SCRIPT = """
local now = redis.call('TIME')
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms')
local capacity = tonumber(ARGV[1])
local refill_per_ms = capacity / 60000
local tokens = tonumber(values[1]) or capacity
local updated_ms = tonumber(values[2]) or now_ms
tokens = math.min(capacity, tokens + math.max(0, now_ms - updated_ms) * refill_per_ms)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], 120000)
return allowed
"""


def _rate_key(scope: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:24]
    return f"supportguard:rate:v1:{scope}:{digest}"


async def admit_command(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    tenant_id: str,
    principal_id: str,
) -> str:
    if session.get_bind().dialect.name == "postgresql":
        snapshot = await session.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
        if not isinstance(snapshot, dict):
            raise RuntimeError("runtime control-plane snapshot is invalid")
        if not bool(snapshot.get("admitted")):
            raise RuntimeConflict("runtime_backpressure")
        backlog = int(snapshot["active_count"])
        oldest = None
        backlog_limit = int(snapshot["backlog_count_limit"])
        oldest_limit_seconds = int(snapshot["oldest_backlog_seconds"])
        fallback_count = int(snapshot.get("fallback_principal_count", 0))
    else:
        backlog = int(
            await session.scalar(
                select(func.count())
                .select_from(RuntimeJob)
                .where(RuntimeJob.status.in_({"queued", "retry_wait", "leased"}))
            )
            or 0
        )
        oldest = await session.scalar(
            select(func.min(RuntimeJob.created_at)).where(
                RuntimeJob.status.in_({"queued", "retry_wait", "leased"})
            )
        )
        backlog_limit = settings.max_durable_backlog
        oldest_limit_seconds = settings.runtime_operational_horizon_seconds
        fallback_count = int(
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRequest)
                .where(
                    IdempotencyRequest.tenant_id == tenant_id,
                    IdempotencyRequest.principal_id == principal_id,
                    IdempotencyRequest.created_at
                    >= datetime.now(UTC) - timedelta(minutes=1),
                )
            )
            or 0
        )
    if backlog >= backlog_limit:
        raise RuntimeConflict("runtime_backpressure")
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        if datetime.now(UTC) - oldest >= timedelta(seconds=oldest_limit_seconds):
            raise RuntimeConflict("runtime_backpressure")
    try:
        tenant_allowed = await cast(
            Awaitable[Any],
            redis.eval(
                BUCKET_SCRIPT,
                1,
                _rate_key("tenant", tenant_id),
                str(settings.tenant_commands_per_minute),
            ),
        )
        principal_allowed = await cast(
            Awaitable[Any],
            redis.eval(
                BUCKET_SCRIPT,
                1,
                _rate_key("principal", f"{tenant_id}:{principal_id}"),
                str(settings.principal_commands_per_minute),
            ),
        )
        if not tenant_allowed or not principal_allowed:
            raise RuntimeConflict("command_rate_limited")
        return "redis_token_bucket"
    except RuntimeConflict:
        raise
    except Exception:
        if fallback_count <= settings.fallback_commands_per_minute:
            return "postgres_fallback"
        raise RuntimeConflict("runtime_backpressure") from None
