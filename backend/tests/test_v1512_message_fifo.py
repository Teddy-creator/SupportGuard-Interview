from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.config import Settings
from supportguard.db.models import (
    AgentRun,
    AuditEvent,
    ConversationTurn,
    IdempotencyRequest,
    OutboxEvent,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.services.admission import admit_command
from supportguard.services.commands import CommandCoordinator, activate_next_turn
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository


class _AllowingRedis:
    async def eval(self, *args: object) -> int:
        return 1


async def _create_conversation(session: AsyncSession, *, key: str) -> tuple[str, str, str]:
    accepted = await CommandCoordinator(
        session,
        provider_identity=("deterministic-fake", "fake", "native_fixture"),
    ).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key=key,
        message="请诊断当前请求。",
        trace_id=f"trace-{key}",
    )
    assert accepted.run_id is not None
    assert accepted.job_id is not None
    return accepted.ticket_id, accepted.run_id, accepted.job_id


async def _ticket_counts(session: AsyncSession, ticket_id: str) -> dict[str, int]:
    async def count(model: type[Any]) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.ticket_id == ticket_id)
            )
            or 0
        )

    run_ids = select(AgentRun.id).where(AgentRun.ticket_id == ticket_id)
    return {
        "messages": await count(TicketMessage),
        "turns": await count(ConversationTurn),
        "runs": await count(AgentRun),
        "jobs": int(
            await session.scalar(
                select(func.count()).select_from(RuntimeJob).where(RuntimeJob.run_id.in_(run_ids))
            )
            or 0
        ),
        "outbox": int(
            await session.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id.in_(run_ids))
            )
            or 0
        ),
    }


async def test_agent_messages_materialize_fifo_work_while_ticket_lane_is_leased(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, _, first_job_id = await _create_conversation(
        db_session,
        key="v1512-fifo-create",
    )
    first_lease = await RuntimeJobRepository(db_session).claim(
        job_id=first_job_id,
        owner="worker-head",
    )

    coordinator = CommandCoordinator(
        db_session,
        provider_identity=("deterministic-fake", "fake", "native_fixture"),
    )
    second = await coordinator.accept_message(
        ticket_id=ticket_id,
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="v1512-fifo-second",
        message="补充第二条消息。",
        trace_id="trace-v1512-fifo-second",
    )
    third = await coordinator.accept_message(
        ticket_id=ticket_id,
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="v1512-fifo-third",
        message="补充第三条消息。",
        trace_id="trace-v1512-fifo-third",
    )

    assert second.status == third.status == "queued"
    assert second.run_id is not None and second.job_id is not None
    assert third.run_id is not None and third.job_id is not None
    assert second.run_id != third.run_id
    assert second.job_id != third.job_id

    turns = (
        await db_session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.ticket_id == ticket_id)
            .order_by(ConversationTurn.ordinal)
        )
    ).all()
    jobs = (
        await db_session.scalars(
            select(RuntimeJob)
            .where(RuntimeJob.ticket_id == ticket_id)
            .order_by(RuntimeJob.dispatch_sequence)
        )
    ).all()
    messages = (
        await db_session.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.conversation_sequence)
        )
    ).all()

    assert [turn.activity_state for turn in turns] == ["running", "queued", "queued"]
    assert all(turn.run_id is not None for turn in turns)
    assert [message.run_id for message in messages] == [turn.run_id for turn in turns]
    assert [message.turn_id for message in messages] == [turn.id for turn in turns]
    assert [message.conversation_sequence for message in messages] == [1, 2, 3]
    assert [job.dispatch_sequence for job in jobs] == [1, 2, 3]
    assert [job.status for job in jobs] == ["leased", "queued", "queued"]
    assert [job.run_id for job in jobs[1:]] == [second.run_id, third.run_id]
    assert await _ticket_counts(db_session, ticket_id) == {
        "messages": 3,
        "turns": 3,
        "runs": 3,
        "jobs": 3,
        "outbox": 3,
    }

    with pytest.raises(RuntimeConflict, match="ticket_fifo_blocked"):
        await RuntimeJobRepository(db_session).claim(
            job_id=second.job_id,
            owner="worker-late",
        )
    assert first_lease.dispatch_sequence == 1


async def test_agent_message_idempotent_replay_preserves_original_run_and_job(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, _, _ = await _create_conversation(
        db_session,
        key="v1512-replay-create",
    )
    coordinator = CommandCoordinator(
        db_session,
        provider_identity=("deterministic-fake", "fake", "native_fixture"),
    )
    arguments = {
        "ticket_id": ticket_id,
        "customer_id": "cust_demo",
        "principal_id": "user_customer_demo",
        "idempotency_key": "v1512-replay-message",
        "message": "同一条重试消息。",
        "trace_id": "trace-v1512-replay-first",
    }
    first = await coordinator.accept_message(**arguments)
    counts_after_first = await _ticket_counts(db_session, ticket_id)
    replay = await coordinator.accept_message(
        **{**arguments, "trace_id": "trace-v1512-replay-http-retry"}
    )

    assert replay.reused is True
    assert replay.accepted_at == first.accepted_at
    assert replay.run_id == first.run_id
    assert replay.job_id == first.job_id
    assert replay.status == "queued"
    assert await _ticket_counts(db_session, ticket_id) == counts_after_first

    request = await db_session.scalar(
        select(IdempotencyRequest).where(
            IdempotencyRequest.idempotency_key == "v1512-replay-message"
        )
    )
    ticket = await db_session.get(SupportTicket, ticket_id)
    assert request is not None
    assert request.resource_ids == {
        "ticket_id": ticket_id,
        "run_id": first.run_id,
        "job_id": first.job_id,
    }
    assert ticket is not None and ticket.next_dispatch_sequence == 2


async def test_next_turn_missing_runtime_identity_fails_closed_without_fake_run(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, first_run_id, first_job_id = await _create_conversation(
        db_session,
        key="v1512-runtime-identity-create",
    )
    ticket = await db_session.get(SupportTicket, ticket_id)
    first_run = await db_session.get(AgentRun, first_run_id)
    first_job = await db_session.get(RuntimeJob, first_job_id)
    first_turn = await db_session.scalar(
        select(ConversationTurn).where(ConversationTurn.run_id == first_run_id)
    )
    assert ticket is not None
    assert first_run is not None
    assert first_job is not None
    assert first_turn is not None
    first_turn.activity_state = "completed"
    first_turn.result_state = "answered"
    first_run.status = "completed"
    first_job.status = "succeeded"
    first_job.terminal_at = first_job.updated_at
    ticket.status = "resolved"
    ticket.next_message_sequence = 2
    message = TicketMessage(
        id=new_id("msg"),
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        content="这条遗留消息缺少运行时身份。",
        conversation_sequence=2,
    )
    db_session.add(message)
    await db_session.flush()
    accepted_turn = ConversationTurn(
        id=new_id("turn"),
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_message_id=message.id,
        ordinal=2,
        activity_state="accepted",
        automation_mode="agent",
        model=None,
        provider_mode=None,
        tool_call_mode=None,
    )
    db_session.add(accepted_turn)
    await db_session.flush()
    message.turn_id = accepted_turn.id
    counts_before = await _ticket_counts(db_session, ticket_id)

    with pytest.raises(RuntimeConflict, match="accepted_turn_runtime_identity_missing"):
        await activate_next_turn(
            db_session,
            ticket=ticket,
            trace_id="trace-v1512-runtime-identity-missing",
        )

    assert await _ticket_counts(db_session, ticket_id) == counts_before
    assert accepted_turn.activity_state == "accepted"
    assert accepted_turn.run_id is None


async def test_human_queue_message_is_recorded_without_agent_work(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, _, _ = await _create_conversation(
        db_session,
        key="v1512-human-create",
    )
    ticket = await db_session.get(SupportTicket, ticket_id)
    assert ticket is not None
    ticket.automation_mode = "human_queue"
    await db_session.flush()
    counts_before = await _ticket_counts(db_session, ticket_id)

    accepted = await CommandCoordinator(
        db_session,
        provider_identity=("deterministic-fake", "fake", "native_fixture"),
    ).accept_message(
        ticket_id=ticket_id,
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="v1512-human-message",
        message="这是人工队列的补充消息。",
        trace_id="trace-v1512-human-message",
    )

    assert accepted.status == "accepted"
    assert accepted.run_id is None and accepted.job_id is None
    assert await _ticket_counts(db_session, ticket_id) == {
        **counts_before,
        "messages": counts_before["messages"] + 1,
        "turns": counts_before["turns"] + 1,
    }
    latest_turn = await db_session.scalar(
        select(ConversationTurn)
        .where(ConversationTurn.ticket_id == ticket_id)
        .order_by(ConversationTurn.ordinal.desc())
    )
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.ticket_id == ticket_id,
            AuditEvent.event_type == "conversation_message_accepted",
        )
    )
    assert latest_turn is not None
    assert latest_turn.activity_state == "completed"
    assert latest_turn.result_state == "human_queue"
    assert latest_turn.run_id is None
    assert audit is not None and audit.run_id is None
    assert ticket.next_dispatch_sequence == 1


async def test_backpressure_rolls_back_the_entire_message_work_unit(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, _, _ = await _create_conversation(
        db_session,
        key="v1512-backpressure-create",
    )
    ticket = await db_session.get(SupportTicket, ticket_id)
    assert ticket is not None
    before_counts = await _ticket_counts(db_session, ticket_id)
    before_message_sequence = ticket.next_message_sequence
    before_dispatch_sequence = ticket.next_dispatch_sequence
    before_version = ticket.version

    with pytest.raises(RuntimeConflict, match="runtime_backpressure"):
        async with db_session.begin_nested():
            await CommandCoordinator(
                db_session,
                provider_identity=("deterministic-fake", "fake", "native_fixture"),
            ).accept_message(
                ticket_id=ticket_id,
                customer_id="cust_demo",
                principal_id="user_customer_demo",
                idempotency_key="v1512-backpressure-message",
                message="这条消息必须随拒绝一起回滚。",
                trace_id="trace-v1512-backpressure-message",
            )
            await admit_command(
                db_session,
                _AllowingRedis(),  # type: ignore[arg-type]
                Settings(max_durable_backlog=1),
                tenant_id="tenant_demo",
                principal_id="user_customer_demo",
            )

    assert await _ticket_counts(db_session, ticket_id) == before_counts
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(IdempotencyRequest)
            .where(IdempotencyRequest.idempotency_key == "v1512-backpressure-message")
        )
        == 0
    )
    await db_session.refresh(ticket)
    assert ticket.next_message_sequence == before_message_sequence
    assert ticket.next_dispatch_sequence == before_dispatch_sequence
    assert ticket.version == before_version


async def test_job_creation_failure_rolls_back_message_run_and_idempotency(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_business_facts(db_session)
    ticket_id, _, _ = await _create_conversation(
        db_session,
        key="v1512-error-create",
    )
    ticket = await db_session.get(SupportTicket, ticket_id)
    assert ticket is not None
    before_counts = await _ticket_counts(db_session, ticket_id)
    before_message_sequence = ticket.next_message_sequence
    before_dispatch_sequence = ticket.next_dispatch_sequence
    original_create = RuntimeJobRepository.create

    async def create_then_fail(
        repository: RuntimeJobRepository,
        **kwargs: Any,
    ) -> RuntimeJob:
        await original_create(repository, **kwargs)
        raise RuntimeError("injected_job_creation_failure")

    monkeypatch.setattr(RuntimeJobRepository, "create", create_then_fail)
    with pytest.raises(RuntimeError, match="injected_job_creation_failure"):
        async with db_session.begin_nested():
            await CommandCoordinator(
                db_session,
                provider_identity=("deterministic-fake", "fake", "native_fixture"),
            ).accept_message(
                ticket_id=ticket_id,
                customer_id="cust_demo",
                principal_id="user_customer_demo",
                idempotency_key="v1512-error-message",
                message="这条消息必须完整回滚。",
                trace_id="trace-v1512-error-message",
            )

    assert await _ticket_counts(db_session, ticket_id) == before_counts
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(IdempotencyRequest)
            .where(IdempotencyRequest.idempotency_key == "v1512-error-message")
        )
        == 0
    )
    await db_session.refresh(ticket)
    assert ticket.next_message_sequence == before_message_sequence
    assert ticket.next_dispatch_sequence == before_dispatch_sequence
