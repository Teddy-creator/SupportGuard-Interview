from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.config import Settings
from supportguard.db.models import RuntimeJob
from supportguard.services.admission import admit_command
from supportguard.services.runtime_jobs import RuntimeConflict


class AllowingRedis:
    async def eval(self, *args: object) -> int:
        return 1


class DenyingRedis:
    async def eval(self, *args: object) -> int:
        return 0


class UnavailableRedis:
    async def eval(self, *args: object) -> int:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_admission_uses_scoped_redis_token_buckets(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    mode = await admit_command(
        db_session,
        AllowingRedis(),  # type: ignore[arg-type]
        Settings(),
        tenant_id="tenant_demo",
        principal_id="principal_demo",
    )
    assert mode == "redis_token_bucket"
    with pytest.raises(RuntimeConflict, match="command_rate_limited"):
        await admit_command(
            db_session,
            DenyingRedis(),  # type: ignore[arg-type]
            Settings(),
            tenant_id="tenant_demo",
            principal_id="principal_demo",
        )


@pytest.mark.asyncio
async def test_admission_uses_bounded_postgres_fallback_when_redis_is_down(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    mode = await admit_command(
        db_session,
        UnavailableRedis(),  # type: ignore[arg-type]
        Settings(fallback_commands_per_minute=1),
        tenant_id="tenant_demo",
        principal_id="principal_fallback",
    )
    assert mode == "postgres_fallback"


@pytest.mark.asyncio
async def test_durable_backlog_fails_closed_before_redis(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    db_session.add(
        RuntimeJob(
            id="job_backpressure",
            tenant_id="tenant_demo",
            ticket_id="ticket_demo",
            run_id="run_demo",
            dispatch_sequence=1,
            kind="agent_start",
            status="queued",
            available_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    with pytest.raises(RuntimeConflict, match="runtime_backpressure"):
        await admit_command(
            db_session,
            AllowingRedis(),  # type: ignore[arg-type]
            Settings(max_durable_backlog=1),
            tenant_id="tenant_demo",
            principal_id="principal_demo",
        )


@pytest.mark.asyncio
async def test_oldest_nonterminal_job_triggers_backpressure(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    db_session.add(
        RuntimeJob(
            id="job_too_old",
            tenant_id="tenant_demo",
            ticket_id="ticket_demo",
            run_id="run_demo",
            dispatch_sequence=1,
            kind="agent_start",
            status="queued",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    await db_session.flush()
    with pytest.raises(RuntimeConflict, match="runtime_backpressure"):
        await admit_command(
            db_session,
            AllowingRedis(),  # type: ignore[arg-type]
            Settings(runtime_operational_horizon_seconds=60),
            tenant_id="tenant_demo",
            principal_id="principal_demo",
        )
