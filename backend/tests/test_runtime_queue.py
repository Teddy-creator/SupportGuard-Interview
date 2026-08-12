from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, exc, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import AgentRunStore
from supportguard.config import Settings
from supportguard.contracts.queue import RuntimeJobMessage
from supportguard.db.base import Base
from supportguard.db.models import (
    AgentRun,
    ConversationTurn,
    InboxDelivery,
    OutboxEvent,
    QueueDeliveryAudit,
    ReconcileIntent,
    RedisDeliveryObservation,
    RetentionTrimIntent,
    RetentionTrimReceipt,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.seed import seed_demo_data
from supportguard.services.runtime_jobs import JobLease
from supportguard.services.runtime_queue import (
    OutboxDispatcher,
    RuntimeReconciler,
    RuntimeWorker,
    bounded_stream_add,
    ensure_consumer_group,
    trim_terminal_deliveries,
)
from supportguard.services.runtime_timing import RuntimeTiming


def _tenant_demo_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        connect_args={"server_settings": {"app.tenant_id": "tenant_demo"}},
    )


async def _delete_conversation_fixture(
    session: AsyncSession, *, run_id: str, message_id: str, ticket_id: str
) -> None:
    await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(turn_id=None))
    await session.execute(
        update(TicketMessage)
        .where(TicketMessage.id == message_id)
        .values(turn_id=None, run_id=None)
    )
    await session.execute(
        delete(ConversationTurn).where(ConversationTurn.customer_message_id == message_id)
    )
    await session.execute(delete(AgentRun).where(AgentRun.id == run_id))
    await session.execute(delete(TicketMessage).where(TicketMessage.id == message_id))
    await session.execute(delete(SupportTicket).where(SupportTicket.id == ticket_id))


@pytest.mark.asyncio
async def test_reconciler_outer_retry_reuses_intent_and_observation() -> None:
    counters = {"candidate": 0, "prepare": 0, "repair": 0, "observe": 0}
    repair_sqlstates: list[str] = []

    class RetryFault(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate
            super().__init__(sqlstate)

    class CandidateRows:
        def mappings(self) -> CandidateRows:
            return self

        def all(self) -> list[dict[str, object]]:
            return [{"job_id": "job-retry-owner", "status_version": 7}]

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def begin(self) -> FakeSession:
            return self

        async def execute(self, statement: object, parameters: object) -> CandidateRows:
            del statement, parameters
            counters["candidate"] += 1
            return CandidateRows()

        async def scalar(self, statement: object, parameters: object) -> object:
            del parameters
            sql = str(statement)
            if "reconciler_prepare" in sql:
                counters["prepare"] += 1
                return {
                    "result": "prepared",
                    "intent_id": "intent-retry-owner",
                    "observation_nonce": "nonce-retry-owner",
                }
            counters["repair"] += 1
            if counters["repair"] <= 2:
                sqlstate = ("40001", "40P01")[counters["repair"] - 1]
                repair_sqlstates.append(sqlstate)
                raise exc.DBAPIError(sql, {}, RetryFault(sqlstate), False)
            return "repaired"

    def factory() -> FakeSession:
        return FakeSession()

    reconciler = RuntimeReconciler(factory, None)  # type: ignore[arg-type]

    async def observe(prepared: dict[str, object]) -> dict[str, object]:
        assert prepared["intent_id"] == "intent-retry-owner"
        counters["observe"] += 1
        return {
            "schema_version": "redis-delivery-observation.v1",
            "intent_id": prepared["intent_id"],
            "status": "known",
        }

    reconciler._observe_reconcile_intent = observe  # type: ignore[method-assign]
    repaired = await reconciler._reconcile_postgresql(redelivery_grace_seconds=0)
    assert repaired == 1
    assert counters == {"candidate": 1, "prepare": 1, "repair": 3, "observe": 1}
    assert repair_sqlstates == ["40001", "40P01"]
    record_predicate_operands(
        requirement_id="C6-P0-04",
        predicate_id="outer_retry_owner_exact",
        subject_kind="reconciler_outer_retry_process",
        operands={
            "candidate_fetch_count": counters["candidate"],
            "prepare_call_count": counters["prepare"],
            "observation_call_count": counters["observe"],
            "repair_call_count": counters["repair"],
            "retry_sqlstates": repair_sqlstates,
            "max_repair_attempts": 3,
            "repaired_effect_count": repaired,
        },
    )


def test_runtime_message_is_minimal_and_rejects_unknown_user_data() -> None:
    message = RuntimeJobMessage(
        event_id="event_1",
        delivery_id="delivery_1",
        job_id="job_1",
        run_id="run_1",
        tenant_id="tenant_1",
        delivery_generation=1,
    )
    assert set(message.model_dump()) == {
        "schema_version",
        "event_id",
        "delivery_id",
        "job_id",
        "run_id",
        "tenant_id",
        "delivery_generation",
        "traceparent",
    }
    with pytest.raises(ValidationError):
        RuntimeJobMessage.model_validate({**message.model_dump(), "user_message": "secret"})


@pytest.mark.asyncio
async def test_worker_cancellation_drains_handler_and_heartbeat_tasks() -> None:
    handler_started = asyncio.Event()
    heartbeat_started = asyncio.Event()
    handler_stopped = asyncio.Event()
    heartbeat_stopped = asyncio.Event()
    never = asyncio.Event()

    async def handler(_message: RuntimeJobMessage, _lease: JobLease) -> str:
        handler_started.set()
        try:
            await never.wait()
        finally:
            handler_stopped.set()
        return "completed"

    worker = RuntimeWorker(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        stream="test-stream",
        group="test-group",
        consumer="test-worker",
        handler=handler,
    )

    async def heartbeat(_lease: JobLease) -> None:
        heartbeat_started.set()
        try:
            await never.wait()
        finally:
            heartbeat_stopped.set()

    worker._heartbeat = heartbeat  # type: ignore[method-assign]
    message = RuntimeJobMessage(
        event_id="event_cancel",
        delivery_id="delivery_cancel",
        job_id="job_cancel",
        run_id="run_cancel",
        tenant_id="tenant_demo",
        delivery_generation=1,
    )
    lease = JobLease(
        job_id=message.job_id,
        run_id=message.run_id,
        tenant_id=message.tenant_id,
        owner="worker_cancel",
        fencing_token=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    execution = asyncio.create_task(worker._run_handler_with_heartbeat(message, lease))
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert handler_stopped.is_set()
    assert heartbeat_stopped.is_set()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_real_redis_stream_does_not_auto_trim() -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:trim:{uuid4().hex}"
    try:
        for index in range(500):
            await bounded_stream_add(
                redis,
                stream=stream,
                fields={"payload": str(index)},
                maxlen=100,
            )
        stream_length = await redis.xlen(stream)
        assert stream_length == 500
        record_predicate_operands(
            requirement_id="C5-P0-17",
            predicate_id="default_no_auto_trim",
            subject_kind="redis_stream_default_retention",
            operands={
                "write_count": 500,
                "requested_legacy_maxlen": 100,
                "stream_length": stream_length,
            },
        )
    finally:
        await redis.delete(stream)
        await redis.aclose()


def test_runtime_thresholds_have_one_versioned_source() -> None:
    settings = Settings()
    timing = RuntimeTiming.from_settings(settings)
    assert settings.runtime_threshold_schema_version == "runtime-thresholds.v1"
    assert settings.runtime_operational_horizon_seconds == 600
    assert timing.operational_horizon == timedelta(minutes=10)
    assert timing.job_lease.total_seconds() == settings.runtime_job_lease_seconds
    assert timing.heartbeat_interval.total_seconds() == settings.runtime_heartbeat_interval_seconds
    assert (
        timing.reconciler_interval.total_seconds() == settings.runtime_reconciler_interval_seconds
    )
    assert timing.pel_min_idle_ms == settings.redis_pel_min_idle_ms
    record_predicate_operands(
        requirement_id="C5-P0-17",
        predicate_id="runtime_threshold_single_source",
        subject_kind="runtime_threshold_contract",
        operands={
            "schema_version": settings.runtime_threshold_schema_version,
            "operational_horizon_seconds": settings.runtime_operational_horizon_seconds,
            "timing_operational_horizon_seconds": int(timing.operational_horizon.total_seconds()),
            "job_lease_seconds": timing.job_lease.total_seconds(),
            "settings_job_lease_seconds": settings.runtime_job_lease_seconds,
            "heartbeat_interval_seconds": timing.heartbeat_interval.total_seconds(),
            "settings_heartbeat_interval_seconds": (settings.runtime_heartbeat_interval_seconds),
            "reconciler_interval_seconds": timing.reconciler_interval.total_seconds(),
            "settings_reconciler_interval_seconds": (settings.runtime_reconciler_interval_seconds),
            "pel_min_idle_ms": timing.pel_min_idle_ms,
            "settings_pel_min_idle_ms": settings.redis_pel_min_idle_ms,
        },
    )


@pytest.mark.redis
@pytest.mark.asyncio
async def test_maintenance_trim_requires_terminal_age_and_no_pel_reference() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_REDIS_URL are required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    maintenance_engine = create_async_engine(
        make_url(database_url).set(
            username="supportguard_maintenance",
            password="supportguard_maintenance",  # noqa: S106 - local role fixture
        )
    )
    maintenance_factory = async_sessionmaker(maintenance_engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url, decode_responses=False)
    suffix = uuid4().hex[:12]
    stream = f"supportguard:test:maintenance-trim:{suffix}"
    group = "supportguard-workers-v1"
    ticket_id = f"ticket_trim_{suffix}"
    message_id = f"message_trim_{suffix}"
    run_id = f"run_trim_{suffix}"
    job_ids = {
        "eligible": f"job_trim_eligible_{suffix}",
        "pg_finalize": f"job_trim_pg_finalize_{suffix}",
        "active": f"job_trim_active_{suffix}",
        "pending": f"job_trim_pending_{suffix}",
    }
    redis_ids: dict[str, str] = {}
    try:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add_all(
                [
                    SupportTicket(
                        id=ticket_id,
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        status="running",
                    ),
                    TicketMessage(
                        id=message_id,
                        tenant_id="tenant_demo",
                        ticket_id=ticket_id,
                        role="user",
                        content="Maintenance trim fixture",
                    ),
                ]
            )
            await session.flush()
            session.add(
                AgentRun(
                    id=run_id,
                    tenant_id="tenant_demo",
                    ticket_id=ticket_id,
                    customer_id="cust_demo",
                    message_id=message_id,
                    status="running",
                    model="fake",
                    provider_mode="fake",
                    tool_call_mode="native_fixture",
                    prompt_version="integration",
                    schema_version="agent.v1",
                    context_version="context.v1.2",
                )
            )
            await session.flush()
            session.add_all(
                [
                    RuntimeJob(
                        id=job_ids["eligible"],
                        tenant_id="tenant_demo",
                        ticket_id=ticket_id,
                        run_id=run_id,
                        kind="agent_start",
                        status="succeeded",
                        terminal_at=datetime.now(UTC) - timedelta(days=8),
                    ),
                    RuntimeJob(
                        id=job_ids["active"],
                        tenant_id="tenant_demo",
                        ticket_id=ticket_id,
                        run_id=run_id,
                        kind="agent_start",
                        status="queued",
                    ),
                    RuntimeJob(
                        id=job_ids["pg_finalize"],
                        tenant_id="tenant_demo",
                        ticket_id=ticket_id,
                        run_id=run_id,
                        kind="agent_start",
                        status="succeeded",
                        terminal_at=datetime.now(UTC) - timedelta(days=8),
                    ),
                    RuntimeJob(
                        id=job_ids["pending"],
                        tenant_id="tenant_demo",
                        ticket_id=ticket_id,
                        run_id=run_id,
                        kind="agent_start",
                        status="dead",
                        terminal_at=datetime.now(UTC) - timedelta(days=8),
                    ),
                ]
            )
        for label, job_id in job_ids.items():
            runtime_message = RuntimeJobMessage(
                event_id=f"event_{label}_{suffix}",
                delivery_id=f"delivery_{label}_{suffix}",
                job_id=job_id,
                run_id=run_id,
                tenant_id="tenant_demo",
                delivery_generation=1,
            )
            raw_id = await redis.xadd(stream, runtime_message.redis_fields())
            redis_ids[label] = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        await ensure_consumer_group(redis, stream=stream, group=group)
        await redis.xreadgroup(group, "worker-1", {stream: ">"}, count=4)
        await redis.xack(
            stream,
            group,
            redis_ids["eligible"],
            redis_ids["pg_finalize"],
            redis_ids["active"],
        )

        old = datetime.now(UTC) - timedelta(days=8)
        async with factory() as session, session.begin():
            for label, job_id in job_ids.items():
                outbox = OutboxEvent(
                    id=f"outbox_trim_{label}_{suffix}",
                    delivery_id=f"delivery_{label}_{suffix}",
                    redis_message_id=redis_ids[label],
                    tenant_id="tenant_demo",
                    job_id=job_id,
                    run_id=run_id,
                    event_type="runtime_job_available",
                    payload={},
                    published_at=old,
                )
                session.add(outbox)
                session.add(
                    InboxDelivery(
                        id=f"inbox_trim_{label}_{suffix}",
                        tenant_id="tenant_demo",
                        job_id=job_id,
                        delivery_id=outbox.delivery_id,
                        redis_message_id=redis_ids[label],
                        consumer_group=group,
                        status="acked" if label != "pending" else "claimed",
                        terminal_at=old if label != "pending" else None,
                    )
                )

        timing = RuntimeTiming.from_settings(Settings())
        future = datetime.now(UTC) + timing.operational_horizon + timedelta(seconds=1)
        async with maintenance_factory() as session, session.begin():
            dry_run = await trim_terminal_deliveries(
                session, redis, stream=stream, timing=timing, apply=False, now=future
            )
        assert dry_run.eligible == 2 and dry_run.deleted == 0
        assert dry_run.skipped == {"postgres_job_not_terminal": 1, "pel_active": 1}

        guard_rejections = 0

        async def assert_trim_guard(_: str) -> None:
            nonlocal guard_rejections
            with pytest.raises(exc.DBAPIError, match="retention_trim_pending"):
                async with factory() as guarded, guarded.begin():
                    await guarded.execute(
                        update(OutboxEvent)
                        .where(OutboxEvent.job_id == job_ids["eligible"])
                        .values(published_at=datetime.now(UTC))
                    )
            guard_rejections += 1
            with pytest.raises(exc.DBAPIError, match="retention_trim_pending"):
                async with factory() as guarded, guarded.begin():
                    guarded.add(
                        QueueDeliveryAudit(
                            tenant_id="tenant_demo",
                            job_id=job_ids["eligible"],
                            delivery_id=f"delivery_eligible_{suffix}",
                            redis_message_id=redis_ids["eligible"],
                            consumer_group="late-audit-fixture",
                            outcome="late_reference",
                            payload_hash="a" * 64,
                            details={"phase": "authorize_to_xdel"},
                        )
                    )
                    await guarded.flush()
            guard_rejections += 1

        async def crash_after_xdel(_: str) -> None:
            raise RuntimeError("simulated_post_xdel_crash")

        async def crash_after_pg_finalize(_: str) -> None:
            raise RuntimeError("simulated_post_pg_finalize_crash")

        with pytest.raises(RuntimeError, match="simulated_post_xdel_crash"):
            async with maintenance_factory() as session, session.begin():
                await trim_terminal_deliveries(
                    session,
                    redis,
                    stream=stream,
                    timing=timing,
                    apply=True,
                    now=future,
                    audit_factory=maintenance_factory,
                    _after_authorize=assert_trim_guard,
                    _after_redis_trim=crash_after_xdel,
                )
        assert (
            await redis.xrange(stream, min=redis_ids["eligible"], max=redis_ids["eligible"]) == []
        )
        interrupted_tombstone = (
            "supportguard:retention:v1:"
            + hashlib.sha256(f"{stream}|{redis_ids['eligible']}".encode()).hexdigest()
        )
        await redis.delete(interrupted_tombstone)
        async with factory() as session:
            interrupted = await session.scalar(
                select(RetentionTrimIntent).where(
                    RetentionTrimIntent.redis_message_id == redis_ids["eligible"]
                )
            )
            assert interrupted is not None and interrupted.status == "authorized"
            lost_entry_outbox_count = int(
                await session.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.job_id == job_ids["eligible"]
                    )
                )
                or 0
            )
            assert lost_entry_outbox_count == 1

        with pytest.raises(RuntimeError, match="simulated_post_pg_finalize_crash"):
            async with maintenance_factory() as session, session.begin():
                await trim_terminal_deliveries(
                    session,
                    redis,
                    stream=stream,
                    timing=timing,
                    apply=True,
                    now=future,
                    audit_factory=maintenance_factory,
                    _after_pg_finalize=crash_after_pg_finalize,
                )
        async with factory() as session:
            pg_finalized_intent = await session.scalar(
                select(RetentionTrimIntent).where(
                    RetentionTrimIntent.redis_message_id == redis_ids["pg_finalize"]
                )
            )
            assert pg_finalized_intent is not None
            assert pg_finalized_intent.status == "finalized"
            assert pg_finalized_intent.updated_at == pg_finalized_intent.finalized_at
            unknown_intent = await session.scalar(
                select(RetentionTrimIntent).where(
                    RetentionTrimIntent.redis_message_id == redis_ids["eligible"]
                )
            )
            assert unknown_intent is not None
            assert unknown_intent.status == "unknown_trim_state"
        pg_finalize_tombstone = (
            "supportguard:retention:v1:"
            + hashlib.sha256(f"{stream}|{redis_ids['pg_finalize']}".encode()).hexdigest()
        )
        assert await redis.pttl(pg_finalize_tombstone) == -1

        async with maintenance_factory() as session, session.begin():
            applied = await trim_terminal_deliveries(
                session,
                redis,
                stream=stream,
                timing=timing,
                apply=True,
                now=future,
                audit_factory=maintenance_factory,
            )
        assert applied.eligible == 0 and applied.deleted == 0
        assert guard_rejections == 2
        assert await redis.pttl(pg_finalize_tombstone) > 0
        assert (
            await redis.xrange(stream, min=redis_ids["eligible"], max=redis_ids["eligible"]) == []
        )
        assert len(await redis.xrange(stream)) == 2
        active_rows = await redis.xrange(stream, min=redis_ids["active"], max=redis_ids["active"])
        pending_rows = await redis.xrange(
            stream, min=redis_ids["pending"], max=redis_ids["pending"]
        )
        maintenance_identity: str
        async with maintenance_engine.connect() as identity_connection:
            maintenance_identity = str(
                await identity_connection.scalar(text("SELECT current_user"))
            )
        async with factory() as session, session.begin():
            intents = list(
                (
                    await session.scalars(
                        select(RetentionTrimIntent).where(
                            RetentionTrimIntent.redis_message_id.in_(
                                [redis_ids["eligible"], redis_ids["pg_finalize"]]
                            )
                        )
                    )
                ).all()
            )
            assert len(intents) == 2
            assert {intent.status for intent in intents} == {
                "finalized",
                "unknown_trim_state",
            }
            finalized = next(item for item in intents if item.status == "finalized")
            assert finalized.finalized_at is not None
            assert finalized.updated_at > finalized.finalized_at
            intent = next(
                item for item in intents if item.redis_message_id == redis_ids["eligible"]
            )
            persisted = await session.scalar(
                select(RetentionTrimIntent).where(
                    RetentionTrimIntent.redis_message_id == redis_ids["eligible"]
                )
            )
            assert persisted is not None and persisted.id == intent.id
            receipt = await session.scalar(
                select(RetentionTrimReceipt).where(RetentionTrimReceipt.intent_id == intent.id)
            )
            assert receipt is None
            with pytest.raises(exc.DBAPIError, match="retention_trim_pending"):
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.job_id == job_ids["eligible"])
                    .values(published_at=datetime.now(UTC))
                )
        operands = {
            "dry_eligible": dry_run.eligible,
            "dry_deleted": dry_run.deleted,
            "dry_skipped": dry_run.skipped,
            "guard_rejections": guard_rejections,
            "active_stream_rows": len(active_rows),
            "pending_stream_rows": len(pending_rows),
            "intent_count": len(intents),
            "intent_statuses": sorted(item.status for item in intents),
            "unknown_receipt_present": receipt is not None,
            "lost_entry_outbox_count": lost_entry_outbox_count,
            "pre_finalize_ttl": -1,
            "post_finalize_ttl_positive": await redis.pttl(pg_finalize_tombstone) > 0,
            "maintenance_identity": maintenance_identity,
            "expected_maintenance_identity": "supportguard_maintenance",
            "consumer_group": group,
            "expected_consumer_group": "supportguard-workers-v1",
            "terminal_age_days": 8,
            "required_terminal_age_days": 7,
            "operational_horizon_seconds": int(timing.operational_horizon.total_seconds()),
            "final_stream_length": len(await redis.xrange(stream)),
            "eligible_entry_remaining": len(
                await redis.xrange(stream, min=redis_ids["eligible"], max=redis_ids["eligible"])
            ),
        }
        for predicate_id in (
            "retention_terminal_anchor_boundary_exact",
            "base_and_final_trim_eligibility_separate",
            "trim_pending_guard_exact",
            "authorize_xdel_toctou_closed",
            "concurrent_reference_rejected",
            "consumer_group_registry_exact",
            "pel_active_delete_zero",
            "redis_uncertain_fail_closed",
            "trim_intent_concurrent_single_owner",
            "trim_intent_crash_resume",
            "lost_tombstone_preserves_pg_lineage",
            "tombstone_ttl_only_after_pg_receipt",
            "maintenance_trust_boundary_explicit",
            "db_lineage_requires_trim_receipt",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-10",
                predicate_id=predicate_id,
                subject_kind="postgres_redis_retention_transaction",
                operands=operands,
            )
        for predicate_id in ("maintenance_trim_eligible_only", "pel_active_not_trimmed"):
            record_predicate_operands(
                requirement_id="C5-P0-17",
                predicate_id=predicate_id,
                subject_kind="postgres_redis_retention_transaction",
                operands=operands,
            )
        async with factory() as session, session.begin():
            await session.execute(
                delete(QueueDeliveryAudit).where(
                    QueueDeliveryAudit.redis_message_id.in_(redis_ids.values())
                )
            )
            await session.execute(
                delete(RetentionTrimReceipt).where(
                    RetentionTrimReceipt.intent_id.in_([item.id for item in intents])
                )
            )
            await session.execute(
                delete(RetentionTrimIntent).where(
                    RetentionTrimIntent.id.in_([item.id for item in intents])
                )
            )
            await session.execute(
                delete(InboxDelivery).where(InboxDelivery.job_id.in_(job_ids.values()))
            )
            await session.execute(
                delete(OutboxEvent).where(OutboxEvent.job_id.in_(job_ids.values()))
            )
            await session.execute(delete(RuntimeJob).where(RuntimeJob.id.in_(job_ids.values())))
            await _delete_conversation_fixture(
                session, run_id=run_id, message_id=message_id, ticket_id=ticket_id
            )
    finally:
        for redis_message_id in redis_ids.values():
            tombstone = (
                "supportguard:retention:v1:"
                + hashlib.sha256(f"{stream}|{redis_message_id}".encode()).hexdigest()
            )
            await redis.delete(tombstone)
        await redis.delete(stream)
        await redis.aclose()
        await maintenance_engine.dispose()
        await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_real_redis_outbox_publish_and_inbox_delivery() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_REDIS_URL are required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dispatcher_engine = create_async_engine(
        make_url(database_url).set(
            username="supportguard_dispatcher",
            password="supportguard_dispatcher",  # noqa: S106 - local role fixture
        )
    )
    dispatcher_factory = async_sessionmaker(dispatcher_engine, expire_on_commit=False)
    worker_engine = create_async_engine(
        make_url(database_url).set(
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106 - local role fixture
        )
    )
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url, decode_responses=False)
    suffix = uuid4().hex[:12]
    stream = f"supportguard:test:{suffix}"
    drain_stream = f"supportguard:test:drain:{suffix}"
    drainer = OutboxDispatcher(dispatcher_factory, redis, stream=drain_stream)
    for _ in range(20):
        if await drainer.dispatch_once() == 0:
            break
    else:
        pytest.fail("shared integration database outbox backlog did not drain within 20 batches")
    await redis.delete(drain_stream)
    job_id = f"job_it_{suffix}"
    outbox_id = f"outbox_it_{suffix}"
    delivery_id = f"delivery_it_{suffix}"
    ticket_id = f"ticket_it_{suffix}"
    message_id = f"message_it_{suffix}"
    run_id = f"run_it_{suffix}"
    async with factory() as session, session.begin():
        ticket = SupportTicket(
            id=ticket_id,
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id=message_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            role="user",
            content="Redis integration fixture",
        )
        session.add_all([ticket, message])
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=message_id,
                status="queued",
                model="fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="integration",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        job = RuntimeJob(
            id=job_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            run_id=run_id,
            kind="agent_start",
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id=outbox_id,
                delivery_id=delivery_id,
                tenant_id="tenant_demo",
                job_id=job_id,
                run_id=run_id,
                event_type="runtime_job_available",
                payload={"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},
            )
        )
    dispatcher = OutboxDispatcher(dispatcher_factory, redis, stream=stream)
    assert await dispatcher.dispatch_once() == 1
    assert await dispatcher.dispatch_once() == 0
    rows = await redis.xrange(stream)
    assert len(rows) == 1
    redis_id, fields = rows[0]
    message = RuntimeJobMessage.from_redis(fields)
    assert message.job_id == job_id and message.delivery_id == delivery_id
    assert "user" not in str(fields).lower()

    async def handler(_: RuntimeJobMessage, lease: JobLease) -> str:
        assert lease.job_id == job_id and lease.tenant_id == "tenant_demo"
        return "completed"

    worker = RuntimeWorker(
        worker_factory,
        redis,
        stream=stream,
        group=f"integration-workers-{suffix}",
        consumer=f"integration-worker-{suffix}",
        handler=handler,
    )
    assert await worker.consume_once(block_ms=100) == 1
    async with factory() as session, session.begin():
        existing = await session.scalar(select(InboxDelivery).where(InboxDelivery.job_id == job_id))
        completed = await session.get(RuntimeJob, job_id)
        assert existing is not None and existing.status == "acked"
        assert completed is not None and completed.status == "succeeded"
        await session.execute(delete(InboxDelivery).where(InboxDelivery.job_id == job_id))
        await session.execute(delete(OutboxEvent).where(OutboxEvent.job_id == job_id))
        await session.execute(delete(RuntimeJob).where(RuntimeJob.id == job_id))
        await _delete_conversation_fixture(
            session, run_id=run_id, message_id=message_id, ticket_id=ticket_id
        )
    await redis.delete(stream)
    await redis.aclose()
    await worker_engine.dispose()
    await dispatcher_engine.dispose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_real_reconciler_capability_holds_incomplete_delivery_observation() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_RECONCILER_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_RECONCILER_REDIS_URL are required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reconciler_engine = create_async_engine(
        make_url(database_url).set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106 - local role fixture
        )
    )
    reconciler_factory = async_sessionmaker(reconciler_engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url, decode_responses=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_reconcile_{suffix}"
    message_id = f"message_reconcile_{suffix}"
    run_id = f"run_reconcile_{suffix}"
    job_id = f"job_reconcile_{suffix}"
    outbox_id = f"outbox_reconcile_{suffix}"
    delivery_id = f"delivery_reconcile_{suffix}"
    old = datetime.now(UTC) - timedelta(minutes=2)
    async with factory() as session, session.begin():
        session.add_all(
            [
                SupportTicket(
                    id=ticket_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    status="queued",
                ),
                TicketMessage(
                    id=message_id,
                    tenant_id="tenant_demo",
                    ticket_id=ticket_id,
                    role="user",
                    content="Reconciler integration fixture",
                ),
            ]
        )
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=message_id,
                status="queued",
                model="fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="integration",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        session.add(
            RuntimeJob(
                id=job_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                run_id=run_id,
                kind="agent_start",
                status="retry_wait",
                available_at=old,
                attempt=1,
            )
        )
        await session.flush()
        session.add(
            OutboxEvent(
                id=outbox_id,
                delivery_id=delivery_id,
                redis_message_id=f"missing-{suffix}",
                tenant_id="tenant_demo",
                job_id=job_id,
                run_id=run_id,
                event_type="runtime_job_available",
                delivery_generation=1,
                published_at=old,
                last_delivery_at=old,
            )
        )
        session.add(
            InboxDelivery(
                tenant_id="tenant_demo",
                job_id=job_id,
                delivery_id=delivery_id,
                redis_message_id=f"missing-{suffix}",
                consumer_group=f"integration-workers-{suffix}",
                status="received",
            )
        )
    reconciler = RuntimeReconciler(
        reconciler_factory,
        redis,
        stream=f"supportguard:test:reconcile:{suffix}",
    )
    repaired = await reconciler.reconcile_once(redelivery_grace_seconds=0)
    assert repaired == 0
    async with factory() as session, session.begin():
        generations = list(
            await session.scalars(
                select(OutboxEvent.delivery_generation)
                .where(OutboxEvent.job_id == job_id)
                .order_by(OutboxEvent.delivery_generation)
            )
        )
        audit = await session.scalar(
            select(QueueDeliveryAudit).where(QueueDeliveryAudit.job_id == job_id)
        )
        job = await session.get(RuntimeJob, job_id)
        assert generations == [1]
        assert audit is None
        assert job is not None and job.status == "retry_wait"
        assert job.delivery_hold_reason == "state_unknown"
        intent_ids = select(ReconcileIntent.id).where(ReconcileIntent.job_id == job_id)
        await session.execute(
            delete(RedisDeliveryObservation).where(
                RedisDeliveryObservation.reconcile_intent_id.in_(intent_ids)
            )
        )
        await session.execute(delete(ReconcileIntent).where(ReconcileIntent.job_id == job_id))
        await session.execute(delete(QueueDeliveryAudit).where(QueueDeliveryAudit.job_id == job_id))
        await session.execute(delete(InboxDelivery).where(InboxDelivery.job_id == job_id))
        await session.execute(delete(OutboxEvent).where(OutboxEvent.job_id == job_id))
        await session.execute(delete(RuntimeJob).where(RuntimeJob.id == job_id))
        await _delete_conversation_fixture(
            session, run_id=run_id, message_id=message_id, ticket_id=ticket_id
        )
    await redis.aclose()
    await reconciler_engine.dispose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_worker_commits_terminal_state_before_stream_ack(tmp_path: Path) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/worker.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_worker",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id="message_worker",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="worker test",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "queued"
        job = RuntimeJob(
            id="job_worker",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id="outbox_worker",
                delivery_id="delivery_worker",
                tenant_id="tenant_demo",
                job_id=job.id,
                run_id=run.id,
                event_type="runtime_job_available",
            )
        )
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:worker:{uuid4().hex}"
    group = "test-workers"
    await OutboxDispatcher(factory, redis, stream=stream).dispatch_once()

    async def handler(message: RuntimeJobMessage, lease: JobLease) -> str:
        assert message.job_id == "job_worker" and lease.fencing_token == 1
        return "completed"

    worker = RuntimeWorker(
        factory,
        redis,
        stream=stream,
        group=group,
        consumer="worker-test",
        handler=handler,
    )
    assert await worker.consume_once(block_ms=100) == 1
    async with factory() as session:
        job = await session.get(RuntimeJob, "job_worker")
        assert job is not None
        inbox = await session.scalar(select(InboxDelivery).where(InboxDelivery.job_id == job.id))
        assert job.status == "succeeded"
        assert inbox is not None and inbox.status == "acked"
    pending = await redis.xpending(stream, group)
    assert pending["pending"] == 0
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_handler_before_any_late_effect(
    tmp_path: Path,
) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/heartbeat.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_heartbeat",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id="message_heartbeat",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="heartbeat cancellation",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "queued"
        job = RuntimeJob(
            id="job_heartbeat",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id="outbox_heartbeat",
                delivery_id="delivery_heartbeat",
                tenant_id="tenant_demo",
                job_id=job.id,
                run_id=run.id,
                event_type="runtime_job_available",
            )
        )
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:heartbeat:{uuid4().hex}"
    await OutboxDispatcher(factory, redis, stream=stream).dispatch_once()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    late_effect = False

    async def handler(_: RuntimeJobMessage, __: JobLease) -> str:
        nonlocal late_effect
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        late_effect = True
        return "completed"

    worker = RuntimeWorker(
        factory,
        redis,
        stream=stream,
        group="heartbeat-workers",
        consumer="heartbeat-worker",
        handler=handler,
    )

    async def failed_heartbeat(_: JobLease) -> None:
        await started.wait()
        raise RuntimeError("heartbeat_connection_lost")

    worker._heartbeat = failed_heartbeat  # type: ignore[method-assign]
    assert await worker.consume_once(block_ms=100) == 1
    assert cancelled.is_set() and late_effect is False
    async with factory() as session:
        stored = await session.get(RuntimeJob, "job_heartbeat")
        inbox = await session.scalar(
            select(InboxDelivery).where(InboxDelivery.job_id == "job_heartbeat")
        )
        assert stored is not None and stored.status == "retry_wait"
        assert stored.lease_owner is None and stored.lease_expires_at is None
        assert inbox is not None and inbox.status == "acked"
        record_predicate_operands(
            requirement_id="C4-P0-07b",
            predicate_id="c4_p0_07b",
            subject_kind="worker_heartbeat_cancellation",
            operands={
                "handler_cancelled": cancelled.is_set(),
                "late_effect": late_effect,
                "job_status": stored.status,
                "lease_owner": stored.lease_owner,
                "inbox_status": inbox.status,
            },
        )
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_poison_delivery_is_durably_audited_before_ack(tmp_path: Path) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/poison.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:poison:{uuid4().hex}"
    group = "poison-workers"
    await redis.xadd(stream, {"payload": "not-json"})

    async def handler(message: RuntimeJobMessage, lease: JobLease) -> str:
        raise AssertionError((message, lease))

    worker = RuntimeWorker(
        factory,
        redis,
        stream=stream,
        group=group,
        consumer="poison-worker",
        handler=handler,
    )
    assert await worker.consume_once(block_ms=100) == 1
    async with factory() as session:
        audit = await session.scalar(select(QueueDeliveryAudit))
        assert audit is not None and audit.outcome == "poison_invalid_schema"
        assert len(audit.payload_hash) == 64
    pending = await redis.xpending(stream, group)
    assert pending["pending"] == 0
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_redis_flush_is_rebuilt_from_postgres_outbox(tmp_path: Path) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/reconcile.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_reconcile",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id="message_reconcile",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="reconcile test",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "queued"
        job = RuntimeJob(
            id="job_reconcile",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id="outbox_reconcile_1",
                delivery_id="delivery_reconcile_1",
                tenant_id="tenant_demo",
                job_id=job.id,
                run_id=run.id,
                event_type="runtime_job_available",
            )
        )
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:reconcile:{uuid4().hex}"
    dispatcher = OutboxDispatcher(factory, redis, stream=stream)
    assert await dispatcher.dispatch_once() == 1
    assert (
        await RuntimeReconciler(factory, redis, stream=stream).reconcile_once(
            redelivery_grace_seconds=0
        )
        == 0
    )
    await redis.delete(stream)
    async with factory() as session, session.begin():
        event = await session.get(OutboxEvent, "outbox_reconcile_1")
        assert event is not None
        event.last_delivery_at = datetime(2000, 1, 1, tzinfo=UTC)
    assert await RuntimeReconciler(factory, redis, stream=stream).reconcile_once() == 1
    assert await dispatcher.dispatch_once() == 1
    assert await redis.xlen(stream) == 1
    async with factory() as session:
        generations = (
            await session.scalars(
                select(OutboxEvent.delivery_generation)
                .where(OutboxEvent.job_id == "job_reconcile")
                .order_by(OutboxEvent.delivery_generation)
            )
        ).all()
        assert generations == [1, 2]
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_reconciler_releases_expired_lease_when_pel_delivery_still_exists(
    tmp_path: Path,
) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/expired-lease.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_expired",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="running",
        )
        message = TicketMessage(
            id="message_expired",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="expired lease",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "running"
        job = RuntimeJob(
            id="job_expired",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
            status="leased",
            lease_owner="dead-worker",
            lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC),
            heartbeat_at=datetime(2000, 1, 1, tzinfo=UTC),
            fencing_token=1,
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id="outbox_expired",
                delivery_id="delivery_expired",
                tenant_id="tenant_demo",
                job_id=job.id,
                run_id=run.id,
                event_type="runtime_job_available",
            )
        )
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:expired:{uuid4().hex}"
    dispatcher = OutboxDispatcher(factory, redis, stream=stream)
    assert await dispatcher.dispatch_once() == 1

    assert await RuntimeReconciler(factory, redis, stream=stream).reconcile_once() == 0
    async with factory() as session:
        stored = await session.get(RuntimeJob, "job_expired")
        generations = await session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.job_id == "job_expired")
        )
        assert stored is not None and stored.status == "queued"
        assert stored.lease_owner is None and stored.lease_expires_at is None
        assert generations == 1
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_old_pel_is_reclaimed_during_continuous_new_traffic(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pel-fairness.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        for ordinal in range(11):
            ticket = SupportTicket(
                id=f"ticket_pel_{ordinal}",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
            message = TicketMessage(
                id=f"message_pel_{ordinal}",
                tenant_id="tenant_demo",
                ticket_id=ticket.id,
                role="user",
                content=f"pel fairness {ordinal}",
            )
            session.add_all([ticket, message])
            await session.flush()
            run = await AgentRunStore(session).create(
                ticket_id=ticket.id,
                customer_id="cust_demo",
                message_id=message.id,
                model="fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                context_version="context.v1.2",
            )
            run.status = "queued"
            job = RuntimeJob(
                id=f"job_pel_{ordinal}",
                tenant_id="tenant_demo",
                ticket_id=ticket.id,
                run_id=run.id,
                dispatch_sequence=1,
                kind="agent_start",
            )
            session.add(job)
            await session.flush()
            session.add(
                OutboxEvent(
                    id=f"outbox_pel_{ordinal}",
                    delivery_id=f"delivery_pel_{ordinal}",
                    tenant_id="tenant_demo",
                    job_id=job.id,
                    run_id=run.id,
                    event_type="runtime_job_available",
                )
            )
    redis = Redis.from_url(redis_url, decode_responses=False)
    stream = f"supportguard:test:pel:{uuid4().hex}"
    group = "pel-fairness-workers"
    dispatcher = OutboxDispatcher(factory, redis, stream=stream)
    assert await dispatcher.dispatch_once(batch_size=1) == 1
    await ensure_consumer_group(redis, stream=stream, group=group)
    claimed = await redis.xreadgroup(group, "dead-consumer", {stream: ">"}, count=1)
    assert claimed and len(claimed[0][1]) == 1
    await asyncio.sleep(35.1)

    first_new = asyncio.Event()

    async def produce_new() -> None:
        for index in range(10):
            assert await dispatcher.dispatch_once(batch_size=1) == 1
            if index == 0:
                first_new.set()
            await asyncio.sleep(0.05)

    processed: list[str] = []
    old_processed_at: float | None = None

    async def handler(message: RuntimeJobMessage, _lease: JobLease) -> str:
        nonlocal old_processed_at
        processed.append(message.job_id)
        if message.job_id == "job_pel_0":
            old_processed_at = time.monotonic()
        await asyncio.sleep(0.01)
        return "completed"

    producer = asyncio.create_task(produce_new())
    await first_new.wait()
    eligible_started = time.monotonic()
    worker = RuntimeWorker(
        factory,
        redis,
        stream=stream,
        group=group,
        consumer="fair-worker",
        handler=handler,
    )
    deadline = time.monotonic() + 10
    while len(processed) < 11 and time.monotonic() < deadline:
        await worker.consume_once(block_ms=100)
    await producer
    old_wait_seconds = (
        old_processed_at - eligible_started if old_processed_at is not None else float("inf")
    )
    pending = await redis.xpending(stream, group)
    assert len(processed) == 11
    assert old_wait_seconds < 2.5
    assert pending["pending"] == 0
    record_property("oldest_eligible_wait_seconds", old_wait_seconds)
    record_property("processed_jobs", len(processed))
    record_property("pel_pending_after_drain", pending["pending"])
    record_predicate_operands(
        requirement_id="C4-P0-11c",
        predicate_id="c4_p0_11c",
        subject_kind="redis_pel_fairness_gate",
        operands={
            "processed_job_count": len(processed),
            "expected_job_count": 11,
            "oldest_eligible_wait_seconds": old_wait_seconds,
            "maximum_wait_seconds": 2.5,
            "pel_pending_after_drain": pending["pending"],
        },
    )
    await redis.delete(stream)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_dead_letters_instead_of_creating_generation_six(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/generation.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_generation",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id="message_generation",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="generation",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "queued"
        job = RuntimeJob(
            id="job_generation",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
        )
        session.add(job)
        await session.flush()
        session.add(
            OutboxEvent(
                id="outbox_generation_5",
                delivery_id="delivery_generation_5",
                tenant_id="tenant_demo",
                job_id=job.id,
                run_id=run.id,
                delivery_generation=5,
                event_type="runtime_job_available",
                published_at=datetime(2000, 1, 1, tzinfo=UTC),
                last_delivery_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 0
    async with factory() as session:
        stored = await session.get(RuntimeJob, "job_generation")
        stored_run = await session.get(AgentRun, run.id)
        stored_ticket = await session.get(SupportTicket, ticket.id)
        maximum = await session.scalar(
            select(func.max(OutboxEvent.delivery_generation)).where(
                OutboxEvent.job_id == "job_generation"
            )
        )
        assert stored is not None and stored.status == "dead"
        assert stored.last_error == "delivery_generation_exhausted"
        assert stored.lease_owner is None and stored.lease_expires_at is None
        assert stored_run is not None and stored_run.status == "failed"
        assert stored_run.active_job_id is None and stored_run.active_fencing_token is None
        assert stored_ticket is not None and stored_ticket.status == "failed"
        assert maximum == 5
        record_predicate_operands(
            requirement_id="C4-P0-07a",
            predicate_id="c4_p0_07a",
            subject_kind="delivery_generation_exhaustion",
            operands={
                "job_status": stored.status,
                "last_error": stored.last_error,
                "lease_owner": stored.lease_owner,
                "run_status": stored_run.status,
                "active_job_id": stored_run.active_job_id,
                "ticket_status": stored_ticket.status,
                "maximum_generation": maximum,
            },
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_due_retry_wait_redelivers_even_when_xacked_stream_entry_still_exists(
    tmp_path: Path,
) -> None:
    class RetainingRedis:
        async def xrange(
            self, *_args: object, **_kwargs: object
        ) -> list[tuple[str, dict[str, str]]]:
            return [("1-0", {"payload": "retained-after-xack"})]

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/acked-redelivery.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_demo_data(session)
        ticket = SupportTicket(
            id="ticket_acked_retry",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        message = TicketMessage(
            id="message_acked_retry",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            role="user",
            content="retry after handler failure",
        )
        session.add_all([ticket, message])
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message_id=message.id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run.status = "queued"
        job = RuntimeJob(
            id="job_acked_retry",
            tenant_id="tenant_demo",
            ticket_id=ticket.id,
            run_id=run.id,
            dispatch_sequence=1,
            kind="agent_start",
            status="retry_wait",
            available_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        event = OutboxEvent(
            id="outbox_acked_retry",
            delivery_id="delivery_acked_retry",
            redis_message_id="1-0",
            tenant_id="tenant_demo",
            job_id=job.id,
            run_id=run.id,
            delivery_generation=1,
            event_type="runtime_job_available",
            published_at=datetime(2000, 1, 1, tzinfo=UTC),
            last_delivery_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        session.add(event)
        session.add(
            InboxDelivery(
                tenant_id="tenant_demo",
                job_id=job.id,
                delivery_id=event.delivery_id,
                redis_message_id="1-0",
                consumer_group="workers",
                status="acked",
                outcome="retry_wait",
            )
        )

    reconciler = RuntimeReconciler(
        factory,
        RetainingRedis(),  # type: ignore[arg-type]
        stream="supportguard:test",
    )
    assert await reconciler.reconcile_once(redelivery_grace_seconds=0) == 1
    async with factory() as session:
        job = await session.get(RuntimeJob, "job_acked_retry")
        generations = (
            await session.scalars(
                select(OutboxEvent.delivery_generation)
                .where(OutboxEvent.job_id == "job_acked_retry")
                .order_by(OutboxEvent.delivery_generation)
            )
        ).all()
        inbox = await session.scalar(
            select(InboxDelivery).where(InboxDelivery.delivery_id == "delivery_acked_retry")
        )
        audit = await session.scalar(
            select(QueueDeliveryAudit).where(
                QueueDeliveryAudit.delivery_id == "delivery_acked_retry"
            )
        )
        assert job is not None and job.status == "queued"
        assert generations == [1, 2]
        assert inbox is not None and inbox.status == "rejected"
        assert audit is not None and audit.outcome == "superseded_lost_or_expired"
    await engine.dispose()
