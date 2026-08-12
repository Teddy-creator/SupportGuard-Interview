from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.contracts.context import RequestContext
from supportguard.db.models import (
    AgentRun,
    ConversationTurn,
    IdempotencyRequest,
    OutboxEvent,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.commands import CommandCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict

pytestmark = pytest.mark.postgres


def _scope() -> RequestContext:
    return RequestContext(
        tenant_id="tenant_demo",
        authenticated_actor_id="cust_demo",
        authenticated_actor_role="customer_member",
        subject_customer_id="cust_demo",
        request_id=f"request-{uuid4().hex}",
        trace_id=f"trace-{uuid4().hex}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


async def _refresh_active_backlog_age(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as connection:
        await connection.execute(
            update(RuntimeJob)
            .where(RuntimeJob.status.in_({"queued", "retry_wait", "leased"}))
            .values(created_at=datetime.now(UTC), available_at=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_concurrent_same_key_claim_creates_exactly_one_resource_group() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin_engine = create_async_engine(database_url)
    await _refresh_active_backlog_age(admin_engine)
    engine = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    factory = create_scoped_session_factory(engine)
    key = f"v125-concurrent-{uuid4().hex}"

    async def submit(trace_id: str):
        async with factory.request(_scope()) as session:
            accepted = await CommandCoordinator(session).accept_new_ticket(
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=key,
                message="Diagnose the same concurrent request",
                trace_id=trace_id,
            )
            await session.commit()
            return accepted

    first, second = await asyncio.gather(submit("trace-a"), submit("trace-b"))
    assert (first.ticket_id, first.run_id, first.job_id) == (
        second.ticket_id,
        second.run_id,
        second.job_id,
    )
    assert sorted([first.reused, second.reused]) == [False, True]
    admin_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with admin_factory() as session:
        claim = await session.scalar(
            select(IdempotencyRequest).where(IdempotencyRequest.idempotency_key == key)
        )
        assert claim is not None
        assert claim.response_snapshot == {**first.response(), "reused": False}
        ticket_count = await session.scalar(
            select(func.count())
            .select_from(SupportTicket)
            .where(SupportTicket.id == first.ticket_id)
        )
        assert ticket_count == 1
        job_count = await session.scalar(
            select(func.count()).select_from(RuntimeJob).where(RuntimeJob.id == first.job_id)
        )
        assert job_count == 1
        outbox_count = await session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.job_id == first.job_id)
        )
        assert outbox_count == 1
        operands = {
            "first_resource_ids": [first.ticket_id, first.run_id, first.job_id],
            "second_resource_ids": [second.ticket_id, second.run_id, second.job_id],
            "reused_flags": sorted([first.reused, second.reused]),
            "response_snapshot": claim.response_snapshot,
            "expected_snapshot": {**first.response(), "reused": False},
            "ticket_count": ticket_count,
            "job_count": job_count,
            "outbox_count": outbox_count,
        }
        for predicate_id in (
            "same_key_same_hash_one_resource",
            "response_loss_replays_snapshot",
        ):
            record_predicate_operands(
                requirement_id="C5-P0-15",
                predicate_id=predicate_id,
                subject_kind="postgres_idempotency_same_hash",
                operands=operands,
            )
    await engine.dispose()
    await admin_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_key_different_hash_returns_stable_conflict() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin_engine = create_async_engine(database_url)
    await _refresh_active_backlog_age(admin_engine)
    engine = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    factory = create_scoped_session_factory(engine)
    key = f"v125-conflict-{uuid4().hex}"

    async def submit(message: str):
        async with factory.request(_scope()) as session:
            try:
                result = await CommandCoordinator(session).accept_new_ticket(
                    customer_id="cust_demo",
                    principal_id="cust_demo",
                    idempotency_key=key,
                    message=message,
                    trace_id=f"trace-{message}",
                )
                await session.commit()
                return result
            except RuntimeConflict as exc:
                await session.rollback()
                return exc.code

    results = await asyncio.gather(submit("payload-a"), submit("payload-b"))
    assert sum(result == "idempotency_conflict" for result in results) == 1
    assert sum(not isinstance(result, str) for result in results) == 1
    accepted = next(result for result in results if not isinstance(result, str))
    admin_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with admin_factory() as session:
        claim = await session.scalar(
            select(IdempotencyRequest).where(IdempotencyRequest.idempotency_key == key)
        )
        assert claim is not None
        assert claim.resource_ids == {
            "ticket_id": accepted.ticket_id,
            "run_id": accepted.run_id,
            "job_id": accepted.job_id,
        }
        ticket_count = await session.scalar(
            select(func.count(SupportTicket.id)).where(SupportTicket.id == accepted.ticket_id)
        )
        assert ticket_count == 1
        job_count = await session.scalar(
            select(func.count(RuntimeJob.id)).where(RuntimeJob.id == accepted.job_id)
        )
        assert job_count == 1
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.job_id == accepted.job_id)
        )
        assert outbox_count == 1
        record_predicate_operands(
            requirement_id="C5-P0-15",
            predicate_id="same_key_different_hash_409",
            subject_kind="postgres_idempotency_hash_conflict",
            operands={
                "conflict_count": sum(result == "idempotency_conflict" for result in results),
                "accepted_count": sum(not isinstance(result, str) for result in results),
                "resource_ids": claim.resource_ids,
                "expected_resource_ids": {
                    "ticket_id": accepted.ticket_id,
                    "run_id": accepted.run_id,
                    "job_id": accepted.job_id,
                },
                "ticket_count": ticket_count,
                "job_count": job_count,
                "outbox_count": outbox_count,
            },
        )
    await engine.dispose()
    await admin_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_follow_up_reuses_one_message_run_job_and_outbox() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin_engine = create_async_engine(database_url)
    await _refresh_active_backlog_age(admin_engine)
    engine = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    factory = create_scoped_session_factory(engine)
    async with factory.request(_scope()) as session:
        ticket = await CommandCoordinator(session).accept_new_ticket(
            customer_id="cust_demo",
            principal_id="cust_demo",
            idempotency_key=f"v126-message-parent-{uuid4().hex}",
            message="Create a parent ticket for the follow-up concurrency test",
            trace_id="trace-parent",
        )
        await session.commit()
    admin_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with admin_factory() as session, session.begin():
        await session.execute(
            update(SupportTicket)
            .where(SupportTicket.id == ticket.ticket_id)
            .values(status="resolved")
        )
        completed_at = datetime.now(UTC)
        await session.execute(
            update(ConversationTurn)
            .where(ConversationTurn.run_id == ticket.run_id)
            .values(activity_state="completed", completed_at=completed_at)
        )
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == ticket.run_id)
            .values(status="completed", checkpoint_stage="completed", completed_at=completed_at)
        )
        await session.execute(
            update(RuntimeJob)
            .where(RuntimeJob.id == ticket.job_id)
            .values(status="succeeded", terminal_at=completed_at)
        )
    key = f"v126-message-{uuid4().hex}"

    async def submit(trace_id: str):
        async with factory.request(_scope()) as session:
            accepted = await CommandCoordinator(session).accept_message(
                ticket_id=ticket.ticket_id,
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=key,
                message="Re-open this ticket with one follow-up",
                trace_id=trace_id,
            )
            await session.commit()
            return accepted

    first, second = await asyncio.gather(submit("trace-message-a"), submit("trace-message-b"))
    assert (first.ticket_id, first.run_id, first.job_id) == (
        second.ticket_id,
        second.run_id,
        second.job_id,
    )
    assert sorted([first.reused, second.reused]) == [False, True]
    async with admin_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.id == first.run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeJob).where(RuntimeJob.id == first.job_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.job_id == first.job_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TicketMessage)
                .where(
                    TicketMessage.ticket_id == ticket.ticket_id,
                    TicketMessage.content == "Re-open this ticket with one follow-up",
                )
            )
            == 1
        )
    await engine.dispose()
    await admin_engine.dispose()
