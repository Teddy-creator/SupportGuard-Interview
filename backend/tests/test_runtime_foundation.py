from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.db.models import (
    AgentRun,
    ConversationTurn,
    OutboxEvent,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.runtime_jobs import (
    IdempotencyRepository,
    RuntimeConflict,
    RuntimeJobRepository,
    canonical_request_hash,
)
from supportguard.services.runtime_queue import RuntimeReconciler, _validate_worker_finish_result


@pytest.mark.asyncio
async def test_idempotency_same_hash_reuses_and_different_hash_conflicts(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    repository = IdempotencyRepository(db_session)
    arguments = dict(
        tenant_id="tenant_demo",
        principal_id="user_demo",
        route="POST /api/tickets",
        key="stable-key-001",
        payload={"message": "hello"},
        resource_ids={"ticket_id": "ticket_demo"},
        response_snapshot={"status": "queued"},
        expires_at=None,
    )
    first = await repository.accept(**arguments)
    second = await repository.accept(**arguments)
    assert first.reused is False and second.reused is True
    assert canonical_request_hash({"b": 2, "a": 1}) == canonical_request_hash({"a": 1, "b": 2})
    with pytest.raises(RuntimeConflict, match="idempotency_conflict"):
        await repository.accept(**{**arguments, "payload": {"message": "different"}})


@pytest.mark.asyncio
async def test_new_claim_increments_fence_and_rejects_old_writer(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    repository = RuntimeJobRepository(db_session)
    job = await repository.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    now = datetime(2026, 7, 12, tzinfo=UTC)
    first = await repository.claim(job_id=job.id, owner="worker-a", now=now)
    await repository.assert_fence(first)
    job.status = "queued"
    run.status = "queued"
    second = await repository.claim(job_id=job.id, owner="worker-b", now=now)
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await repository.assert_fence(first)


@pytest.mark.asyncio
async def test_expired_lease_rejects_current_owner_and_token(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    repository = RuntimeJobRepository(db_session)
    job = await repository.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await repository.claim(job_id=job.id, owner="worker-a")
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await repository.assert_fence(lease)


@pytest.mark.asyncio
async def test_typed_domain_failure_is_terminal_without_retry_wait(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    repository = RuntimeJobRepository(db_session)
    job = await repository.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await repository.claim(job_id=job.id, owner="worker-a")

    outcome = await repository.terminal_fail(
        lease,
        error_code="domain:ticket_scope_violation",
    )

    stored_job = await db_session.get(RuntimeJob, job.id)
    ticket = await db_session.get(SupportTicket, run.ticket_id)
    assert outcome == "dead"
    assert stored_job is not None and stored_job.status == "dead"
    assert stored_job.last_error == "domain:ticket_scope_violation"
    assert run.status == "failed" and run.active_job_id is None
    assert ticket is not None and ticket.status == "failed"
    record_predicate_operands(
        requirement_id="C5-P0-10",
        predicate_id="domain_terminal_no_infra_retry",
        subject_kind="domain_terminal_job_state",
        operands={
            "outcome": outcome,
            "job_status": stored_job.status,
            "job_error": stored_job.last_error,
            "run_status": run.status,
            "active_job_present": run.active_job_id is not None,
            "ticket_status": ticket.status,
            "retry_wait_count": int(stored_job.status == "retry_wait"),
        },
    )


@pytest.mark.asyncio
async def test_ticket_dispatch_claim_is_fifo_and_single_leased(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    first_run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert first_run is not None and ticket is not None
    first_run.status = "queued"
    ticket.next_message_sequence = 1
    second_message = TicketMessage(
        id="message_fifo_second",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        content="second",
        conversation_sequence=2,
    )
    second_run = AgentRun(
        id="run_fifo_second",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=second_message.id,
        status="queued",
        checkpoint_stage="request_created",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native",
        prompt_version="v1.1",
        schema_version="agent.v1",
        context_version="context.v1",
    )
    db_session.add_all([second_message, second_run])
    await db_session.flush()
    repository = RuntimeJobRepository(db_session)
    first_job = await repository.create(
        tenant_id=ticket.tenant_id,
        run_id=first_run.id,
        kind="agent_start",
    )
    second_job = await repository.create(
        tenant_id=ticket.tenant_id,
        run_id=second_run.id,
        kind="agent_start",
    )
    assert (first_job.dispatch_sequence, second_job.dispatch_sequence) == (1, 2)

    with pytest.raises(RuntimeConflict, match="ticket_fifo_blocked"):
        await repository.claim(job_id=second_job.id, owner="worker-late")

    first_lease = await repository.claim(job_id=first_job.id, owner="worker-head")
    with pytest.raises(RuntimeConflict, match="ticket_fifo_blocked"):
        await repository.claim(job_id=second_job.id, owner="worker-late")
    leased_count = await db_session.scalar(
        select(func.count())
        .select_from(RuntimeJob)
        .where(
            RuntimeJob.ticket_id == ticket.id,
            RuntimeJob.status == "leased",
        )
    )
    assert leased_count == 1

    await repository.complete(first_lease, outcome="completed")
    second_lease = await repository.claim(job_id=second_job.id, owner="worker-next")
    assert second_lease.ticket_id == ticket.id
    assert second_lease.dispatch_sequence == 2


@pytest.mark.asyncio
async def test_sqlite_reconciler_does_not_redeliver_past_ticket_head(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    first_run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert first_run is not None and ticket is not None
    first_run.status = "queued"
    second_message = TicketMessage(
        id="message_reconcile_second",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        content="later delivery",
    )
    second_run = AgentRun(
        id="run_reconcile_second",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=second_message.id,
        status="queued",
        checkpoint_stage="request_created",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native",
        prompt_version="v1.1",
        schema_version="agent.v1",
        context_version="context.v1",
    )
    db_session.add_all([second_message, second_run])
    await db_session.flush()
    repository = RuntimeJobRepository(db_session)
    head = await repository.create(
        tenant_id=ticket.tenant_id,
        run_id=first_run.id,
        kind="agent_start",
    )
    later = await repository.create(
        tenant_id=ticket.tenant_id,
        run_id=second_run.id,
        kind="agent_start",
    )
    head.status = "retry_wait"
    head.available_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    repaired = await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0)
    async with factory() as verification:
        later_outbox_count = await verification.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.job_id == later.id)
        )
        generated_deliveries = await verification.scalar(
            select(func.count())
            .select_from(RuntimeJob)
            .where(RuntimeJob.id.in_((head.id, later.id)), RuntimeJob.status == "leased")
        )
    assert repaired == 0
    assert later_outbox_count == 0
    assert generated_deliveries == 0


@pytest.mark.asyncio
async def test_dead_job_publishes_failure_and_activates_oldest_accepted_turn(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    current_message = await db_session.get(TicketMessage, "message_demo")
    current_turn = await db_session.get(ConversationTurn, "turn_demo")
    assert (
        run is not None
        and ticket is not None
        and current_message is not None
        and current_turn is not None
    )
    current_message.message_kind = "customer"
    current_message.conversation_sequence = 1
    ticket.next_message_sequence = 1
    current_turn.activity_state = "queued"
    run.status = "queued"
    accepted_message = TicketMessage(
        id="message_accepted",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        content="follow up",
        conversation_sequence=2,
    )
    accepted_turn = ConversationTurn(
        id="turn_accepted",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_message_id=accepted_message.id,
        ordinal=2,
        activity_state="accepted",
        automation_mode="agent",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native",
        context_version="context.v1",
    )
    accepted_message.turn_id = accepted_turn.id
    ticket.next_message_sequence = 2
    db_session.add_all([accepted_message, accepted_turn])
    await db_session.flush()

    repository = RuntimeJobRepository(db_session)
    failed_job = await repository.create(
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        kind="agent_start",
    )
    lease = await repository.claim(job_id=failed_job.id, owner="worker-failed")
    await repository.terminal_fail(lease, error_code="checkpoint_unavailable")
    await db_session.flush()

    failure_reply = await db_session.scalar(
        select(TicketMessage).where(
            TicketMessage.publication_key == f"runtime-failure:{failed_job.id}"
        )
    )
    activated_run = (
        await db_session.get(AgentRun, accepted_turn.run_id)
        if accepted_turn.run_id is not None
        else None
    )
    activated_job = (
        await db_session.scalar(select(RuntimeJob).where(RuntimeJob.run_id == activated_run.id))
        if activated_run is not None
        else None
    )
    assert current_turn.activity_state == "failed"
    assert current_turn.result_state == "failed"
    assert failure_reply is not None and "稍后重试" in failure_reply.content
    assert accepted_turn.activity_state == "queued"
    assert activated_run is not None and activated_run.status == "queued"
    assert activated_job is not None
    assert activated_job.ticket_id == ticket.id
    assert activated_job.dispatch_sequence == 2
    assert ticket.automation_mode == "agent"
    assert ticket.status == "queued"


def test_postgres_finish_contract_rejects_fake_manual_queue_and_partial_activation() -> None:
    with pytest.raises(RuntimeError, match="converted failure to manual_takeover"):
        _validate_worker_finish_result(
            {
                "status": "dead",
                "outcome": "manual_takeover",
                "ticket_status": "manual_takeover",
            },
            requested_outcome="failed:OperationalError",
        )
    with pytest.raises(RuntimeError, match="partial Turn activation"):
        _validate_worker_finish_result(
            {
                "status": "succeeded",
                "outcome": "completed",
                "activated_turn_id": "turn_next",
            },
            requested_outcome="completed",
        )
    explicit_manual = _validate_worker_finish_result(
        {
            "status": "succeeded",
            "outcome": "manual_takeover",
            "automation_mode": "human_queue",
        },
        requested_outcome="manual_takeover",
    )
    assert explicit_manual["outcome"] == "manual_takeover"
