from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import (
    _event_hash,
    _payload_hash,
    verify_ticket_event_chain,
)
from supportguard.contracts.context import RequestContext
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    IdempotencyRequest,
    OutboxEvent,
    ProposalRecord,
    ProposalWithdrawal,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.seed import seed_demo_data
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.commands import CommandCoordinator
from supportguard.services.proposal_withdrawals import ProposalWithdrawalCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from test_postgres_finalizer_faults import (
    _approver_scope,
)
from test_postgres_finalizer_faults import (
    _seed_pending_approval as _seed_pending_approval_fixture,
)
from test_v1512_action_effect_reconciliation_postgres import (
    _prepare_unknown_action_effect,
)

pytestmark = pytest.mark.postgres


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _role_url(database_url: str, *, username: str, password: str) -> str:
    return (
        make_url(database_url)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


def _customer_scope(prefix: str) -> RequestContext:
    return RequestContext(
        tenant_id="tenant_demo",
        authenticated_actor_id="cust_demo",
        authenticated_actor_role="customer_member",
        subject_customer_id="cust_demo",
        request_id=f"request-{prefix}",
        trace_id=f"trace-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _decision_request(
    prefix: str,
    *,
    decision: str,
    idempotency_key: str,
    edited_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "api-accept-approval-decision.v1",
        "actor_id": "user_approver_demo",
        "idempotency_key": idempotency_key,
        "reason": f"Phase 1 PostgreSQL {decision} contract proof.",
        "approver_note": "The persisted evidence and policy binding were reviewed.",
        "edited_payload": edited_payload or {},
        "idempotency_id": f"idem_{prefix}_{uuid4().hex[:8]}",
        "revision_id": f"revision_{prefix}_{uuid4().hex[:8]}",
        "decision_id": f"decision_{prefix}_{uuid4().hex[:8]}",
        "event_id": f"event_{prefix}_{uuid4().hex[:8]}",
        "job_id": f"job_{prefix}_{uuid4().hex[:8]}",
        "outbox_id": f"outbox_{prefix}_{uuid4().hex[:8]}",
        "delivery_id": f"delivery_{prefix}_{uuid4().hex[:8]}",
        "audit_id": f"audit_{prefix}_{uuid4().hex[:8]}",
        "trace_id": f"trace-{prefix}",
    }


async def _seed_pending_approval(
    factory,
    prefix: str,
    *,
    action_type: str = "refund",
) -> tuple[str, str]:
    """Make the imported fixture independent from demo seed/order assumptions."""
    async with factory() as session, session.begin():
        await seed_demo_data(session)
    return await _seed_pending_approval_fixture(
        factory,
        prefix,
        action_type=action_type,
    )


async def _raw_decide_and_commit(
    api_factory,
    *,
    prefix: str,
    approval_id: str,
    decision: str,
    idempotency_key: str,
    edited_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    request = _decision_request(
        prefix,
        decision=decision,
        idempotency_key=idempotency_key,
        edited_payload=edited_payload,
    )
    async with api_factory.request(_approver_scope(prefix)) as session:
        value = await session.scalar(
            text(
                "SELECT supportguard_api_accept_conversation_approval_decision("
                ":approval_id,:decision,CAST(:request AS jsonb))"
            ),
            {
                "approval_id": approval_id,
                "decision": decision,
                "request": json.dumps(request, sort_keys=True, separators=(",", ":")),
            },
        )
        await session.commit()
    assert isinstance(value, dict)
    return value


async def _withdraw(
    api_factory,
    *,
    prefix: str,
    approval_id: str,
    idempotency_key: str | None = None,
):
    async with api_factory.request(_customer_scope(prefix)) as session:
        accepted = await ProposalWithdrawalCoordinator(session).withdraw(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            principal_id="cust_demo",
            approval_id=approval_id,
            idempotency_key=idempotency_key or f"withdraw-{prefix}",
            reason="The customer no longer wants this high-risk action.",
            trace_id=f"trace-{prefix}-withdrawal",
        )
        await session.commit()
        return accepted


async def _seed_accepted_followup(
    factory,
    api_factory,
    *,
    prefix: str,
    approval_id: str,
) -> tuple[str, str]:
    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None
        ticket_id = approval.ticket_id
    async with api_factory.request(_customer_scope(prefix)) as session:
        accepted = await CommandCoordinator(session).accept_message(
            ticket_id=ticket_id,
            customer_id="cust_demo",
            principal_id="cust_demo",
            idempotency_key=f"followup-{prefix}",
            message="Please continue with the ordinary support question after this action.",
            trace_id=f"trace-{prefix}-followup",
        )
        await session.commit()
    assert accepted.run_id is not None
    async with factory() as session:
        run = await session.get(AgentRun, accepted.run_id)
        assert run is not None and run.turn_id is not None
        turn = await session.get(ConversationTurn, run.turn_id)
        assert turn is not None
        assert turn.activity_state == "queued"
        return turn.id, run.message_id


async def _approval_job_and_outbox_counts(
    session,
    *,
    approval_id: str,
) -> tuple[int, int]:
    job_count = int(
        await session.scalar(
            select(func.count(RuntimeJob.id)).where(RuntimeJob.approval_id == approval_id)
        )
        or 0
    )
    outbox_count = int(
        await session.scalar(
            select(func.count(OutboxEvent.id))
            .join(RuntimeJob, RuntimeJob.id == OutboxEvent.job_id)
            .where(RuntimeJob.approval_id == approval_id)
        )
        or 0
    )
    return job_count, outbox_count


async def _wait_until_blocked(
    admin_engine,
    *,
    backend_pid: int,
    attempts: int = 150,
) -> list[int]:
    for _ in range(attempts):
        async with admin_engine.connect() as observer:
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:backend_pid)"),
                {"backend_pid": backend_pid},
            )
        blocker_ids = [int(item) for item in blockers or []]
        if blocker_ids:
            return blocker_ids
        await asyncio.sleep(0.02)
    raise AssertionError(f"backend {backend_pid} did not reach the PostgreSQL barrier")


async def _approve(
    api_factory,
    *,
    prefix: str,
    approval_id: str,
):
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="The persisted evidence and policy binding were reviewed.",
            approver_note="Phase 1 PostgreSQL ticket-lane proof.",
            trace_id=f"trace-{prefix}-approval",
        )
        await session.commit()
        return accepted


async def _accept_followup(
    api_factory,
    *,
    prefix: str,
    ticket_id: str,
):
    async with api_factory.request(_customer_scope(prefix)) as session:
        accepted = await CommandCoordinator(
            session,
            provider_identity=("deterministic-fake", "fake", "native_fixture"),
        ).accept_message(
            ticket_id=ticket_id,
            customer_id="cust_demo",
            principal_id="cust_demo",
            idempotency_key=f"message-{prefix}",
            message=f"Please continue with the ordinary follow-up for {prefix}.",
            trace_id=f"trace-{prefix}-message",
        )
        await session.commit()
        return accepted


async def _assert_fifo_and_claim_head(
    factory,
    *,
    earlier_job_id: str,
    later_job_id: str,
    owner: str,
) -> None:
    async with factory() as session:
        earlier = await session.get(RuntimeJob, earlier_job_id)
        later = await session.get(RuntimeJob, later_job_id)
        assert earlier is not None and later is not None
        assert earlier.ticket_id == later.ticket_id
        assert earlier.dispatch_sequence < later.dispatch_sequence

    async with factory() as session, session.begin():
        with pytest.raises(RuntimeConflict) as blocked:
            await RuntimeJobRepository(session).claim(
                job_id=later_job_id,
                owner=f"{owner}-later",
            )
        assert blocked.value.code == "ticket_fifo_blocked"

    async with factory() as session:
        transaction = await session.begin()
        try:
            lease = await RuntimeJobRepository(session).claim(
                job_id=earlier_job_id,
                owner=f"{owner}-head",
            )
            assert lease.job_id == earlier_job_id
            assert lease.ticket_id == earlier.ticket_id
            assert lease.dispatch_sequence == earlier.dispatch_sequence
        finally:
            await transaction.rollback()


async def _worker_delivery_payload(
    factory,
    *,
    job_id: str,
    owner: str,
    redis_suffix: str,
) -> dict[str, object]:
    async with factory() as session:
        job = await session.get(RuntimeJob, job_id)
        outbox = await session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
        assert job is not None and outbox is not None
        return {
            "schema_version": "worker-delivery.v1",
            "event_id": outbox.id,
            "delivery_id": outbox.delivery_id,
            "job_id": job.id,
            "run_id": job.run_id,
            "tenant_id": job.tenant_id,
            "generation": outbox.delivery_generation,
            "redis_message_id": f"{redis_suffix}-0",
            "consumer_group": "supportguard-v1512-phase1",
            "owner": owner,
            "payload_hash": "a" * 64,
        }


async def _accept_worker_delivery(worker, payload: dict[str, object]) -> dict[str, object]:
    async with worker.begin() as connection:
        value = await connection.scalar(
            text("SELECT supportguard_worker_accept_delivery(CAST(:payload AS jsonb))"),
            {
                "payload": json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
    assert isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_ticket_lane_retry_wait_blocks_later_job_but_not_other_ticket() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_retry_fifo_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        _, ticket_a = await _seed_pending_approval(factory, f"{prefix}_a")
        _, ticket_b = await _seed_pending_approval(factory, f"{prefix}_b")
        head_a = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_a_head",
            ticket_id=ticket_a,
        )
        later_a = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_a_later",
            ticket_id=ticket_a,
        )
        head_b = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_b_head",
            ticket_id=ticket_b,
        )
        assert head_a.job_id and later_a.job_id and head_b.job_id

        async with factory() as session, session.begin():
            lease = await RuntimeJobRepository(session).claim(
                job_id=head_a.job_id,
                owner=f"worker-{prefix}-retry",
            )
            status = await RuntimeJobRepository(session).fail(
                lease,
                error_code="retryable_fixture",
            )
            assert status == "retry_wait"

        async with factory() as session, session.begin():
            with pytest.raises(RuntimeConflict) as not_due:
                await RuntimeJobRepository(session).claim(
                    job_id=head_a.job_id,
                    owner=f"worker-{prefix}-head-retry",
                )
            assert not_due.value.code == "job_not_due"
        async with factory() as session, session.begin():
            with pytest.raises(RuntimeConflict) as fifo_blocked:
                await RuntimeJobRepository(session).claim(
                    job_id=later_a.job_id,
                    owner=f"worker-{prefix}-later",
                )
            assert fifo_blocked.value.code == "ticket_fifo_blocked"
        async with factory() as session, session.begin():
            other_ticket_lease = await RuntimeJobRepository(session).claim(
                job_id=head_b.job_id,
                owner=f"worker-{prefix}-other-ticket",
            )
            assert other_ticket_lease.ticket_id == ticket_b

        async with factory() as session:
            stored_head = await session.get(RuntimeJob, head_a.job_id)
            stored_later = await session.get(RuntimeJob, later_a.job_id)
            stored_other = await session.get(RuntimeJob, head_b.job_id)
            assert stored_head is not None and stored_head.status == "retry_wait"
            assert stored_later is not None and stored_later.status == "queued"
            assert stored_other is not None and stored_other.status == "leased"
            assert stored_head.dispatch_sequence < stored_later.dispatch_sequence
            assert stored_other.ticket_id != stored_head.ticket_id
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_two_workers_serialize_one_ticket_while_other_ticket_claims_in_parallel() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_lane_parallel_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    pid_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
    try:
        _, ticket_a = await _seed_pending_approval(factory, f"{prefix}_a")
        _, ticket_b = await _seed_pending_approval(factory, f"{prefix}_b")
        head_a = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_a_head",
            ticket_id=ticket_a,
        )
        later_a = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_a_later",
            ticket_id=ticket_a,
        )
        head_b = await _accept_followup(
            api_factory,
            prefix=f"{prefix}_b_head",
            ticket_id=ticket_b,
        )
        assert head_a.job_id and later_a.job_id and head_b.job_id
        head_payload = await _worker_delivery_payload(
            factory,
            job_id=head_a.job_id,
            owner=f"worker-{prefix}-head",
            redis_suffix="151201",
        )
        later_payload = await _worker_delivery_payload(
            factory,
            job_id=later_a.job_id,
            owner=f"worker-{prefix}-later",
            redis_suffix="151202",
        )
        other_payload = await _worker_delivery_payload(
            factory,
            job_id=head_b.job_id,
            owner=f"worker-{prefix}-other",
            redis_suffix="151203",
        )

        async def blocked_delivery(
            label: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            async with worker.begin() as connection:
                backend_pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
                await pid_queue.put((label, backend_pid))
                value = await connection.scalar(
                    text("SELECT supportguard_worker_accept_delivery(CAST(:payload AS jsonb))"),
                    {
                        "payload": json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                )
            assert isinstance(value, dict)
            return value

        async with admin.connect() as barrier:
            transaction = await barrier.begin()
            await barrier.execute(
                text(
                    "SELECT id FROM support_tickets "
                    "WHERE tenant_id='tenant_demo' AND id=:ticket_id FOR UPDATE"
                ),
                {"ticket_id": ticket_a},
            )
            head_task = asyncio.create_task(blocked_delivery("head", head_payload))
            later_task = asyncio.create_task(blocked_delivery("later", later_payload))
            pids = dict([await pid_queue.get(), await pid_queue.get()])
            for backend_pid in pids.values():
                blockers = await _wait_until_blocked(
                    admin,
                    backend_pid=backend_pid,
                )
                assert blockers

            other_result = await asyncio.wait_for(
                _accept_worker_delivery(worker, other_payload),
                timeout=3,
            )
            assert other_result["result"] == "claimed"
            await transaction.rollback()
            head_result, later_result = await asyncio.gather(
                head_task,
                later_task,
            )

        assert head_result["result"] == "claimed"
        assert later_result["result"] == "not_claimable"
        async with factory() as session:
            head_job = await session.get(RuntimeJob, head_a.job_id)
            later_job = await session.get(RuntimeJob, later_a.job_id)
            other_job = await session.get(RuntimeJob, head_b.job_id)
            assert head_job is not None and head_job.status == "leased"
            assert later_job is not None and later_job.status == "queued"
            assert other_job is not None and other_job.status == "leased"
            assert (
                int(
                    await session.scalar(
                        select(func.count(RuntimeJob.id)).where(
                            RuntimeJob.tenant_id == "tenant_demo",
                            RuntimeJob.ticket_id == ticket_a,
                            RuntimeJob.status == "leased",
                        )
                    )
                    or 0
                )
                == 1
            )
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["approve", "withdraw"])
async def test_action_transaction_rollback_removes_idempotency_and_outbox_then_retry_commits(
    operation: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_tx_rollback_{operation}_{uuid4().hex[:8]}"
    key = f"rollback-{prefix}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.run_id is not None
            proposal_id = approval.proposal_id
            run_id = approval.run_id
            turn_id = approval.origin_turn_id
            ticket = await session.get(SupportTicket, ticket_id)
            assert ticket is not None
            baseline = {
                "approval_status": approval.status,
                "approval_version": approval.status_version,
                "ticket_status": ticket.status,
                "ticket_version": ticket.version,
                "message_count": int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                ),
                "event_count": int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(AgentEvent.ticket_id == ticket_id)
                    )
                    or 0
                ),
            }

        if operation == "approve":
            async with api_factory.request(_approver_scope(prefix)) as session:
                accepted = await ApprovalCommandCoordinator(session).decide(
                    tenant_id="tenant_demo",
                    approval_id=approval_id,
                    decision="approve",
                    actor_id="user_approver_demo",
                    idempotency_key=key,
                    reason="The persisted evidence and policy binding were reviewed.",
                    approver_note="Rollback-before-commit PostgreSQL proof.",
                    trace_id=f"trace-{prefix}-rollback",
                )
                assert accepted.job_id is not None
                await session.rollback()
        else:
            async with api_factory.request(_customer_scope(prefix)) as session:
                accepted = await ProposalWithdrawalCoordinator(session).withdraw(
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    principal_id="cust_demo",
                    approval_id=approval_id,
                    idempotency_key=key,
                    reason="The customer no longer wants this high-risk action.",
                    trace_id=f"trace-{prefix}-rollback",
                )
                assert accepted.withdrawal_id
                await session.rollback()

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            run = await session.get(AgentRun, run_id)
            turn = await session.get(ConversationTurn, turn_id or "")
            ticket = await session.get(SupportTicket, ticket_id)
            assert approval is not None
            assert approval.status == baseline["approval_status"] == "pending"
            assert approval.status_version == baseline["approval_version"]
            assert proposal is not None and proposal.status == "bound"
            assert run is not None and run.status == "interrupted"
            assert turn is not None and turn.activity_state == "waiting_external"
            assert ticket is not None
            assert ticket.status == baseline["ticket_status"] == "awaiting_approval"
            assert ticket.version == baseline["ticket_version"]
            assert (
                int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                )
                == baseline["message_count"]
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(AgentEvent.ticket_id == ticket_id)
                    )
                    or 0
                )
                == baseline["event_count"]
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(IdempotencyRequest.id)).where(
                            IdempotencyRequest.idempotency_key == key
                        )
                    )
                    or 0
                )
                == 0
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(ProposalWithdrawal.id)).where(
                            ProposalWithdrawal.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            assert await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            ) == (0, 0)

        if operation == "approve":
            async with api_factory.request(_approver_scope(f"{prefix}-retry")) as session:
                recovered = await ApprovalCommandCoordinator(session).decide(
                    tenant_id="tenant_demo",
                    approval_id=approval_id,
                    decision="approve",
                    actor_id="user_approver_demo",
                    idempotency_key=key,
                    reason="The persisted evidence and policy binding were reviewed.",
                    approver_note="Rollback-before-commit PostgreSQL proof.",
                    trace_id=f"trace-{prefix}-retry",
                )
                await session.commit()
            assert recovered.reused is False and recovered.job_id is not None
        else:
            async with api_factory.request(_customer_scope(f"{prefix}-retry")) as session:
                recovered = await ProposalWithdrawalCoordinator(session).withdraw(
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    principal_id="cust_demo",
                    approval_id=approval_id,
                    idempotency_key=key,
                    reason="The customer no longer wants this high-risk action.",
                    trace_id=f"trace-{prefix}-retry",
                )
                await session.commit()
            assert recovered.reused is False

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            assert approval.status == ("approved" if operation == "approve" else "withdrawn")
            assert (
                int(
                    await session.scalar(
                        select(func.count(IdempotencyRequest.id)).where(
                            IdempotencyRequest.idempotency_key == key
                        )
                    )
                    or 0
                )
                == 1
            )
            assert await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            ) == ((1, 1) if operation == "approve" else (0, 0))
            assert int(
                await session.scalar(
                    select(func.count(HumanDecision.id)).where(
                        HumanDecision.approval_id == approval_id
                    )
                )
                or 0
            ) == (1 if operation == "approve" else 0)
            assert int(
                await session.scalar(
                    select(func.count(ProposalWithdrawal.id)).where(
                        ProposalWithdrawal.approval_id == approval_id
                    )
                )
                or 0
            ) == (1 if operation == "withdraw" else 0)
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_operation", ["approve", "withdraw"])
async def test_worker_finalizer_and_action_command_share_ticket_barrier_without_deadlock(
    action_operation: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_finalizer_barrier_{action_operation}_{uuid4().hex[:8]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    pid_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        followup = await _accept_followup(
            api_factory,
            prefix=f"{prefix}-followup",
            ticket_id=ticket_id,
        )
        assert followup.job_id is not None
        async with factory() as session, session.begin():
            lease = await RuntimeJobRepository(session).claim(
                job_id=followup.job_id,
                owner=f"worker-{prefix}",
            )

        async def finish_job() -> dict[str, object]:
            async with worker.begin() as connection:
                backend_pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
                await pid_queue.put(("finalizer", backend_pid))
                value = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": lease.job_id,
                        "owner": lease.owner,
                        "fencing_token": lease.fencing_token,
                        "outcome": "failed:barrier_retryable_fixture",
                    },
                )
            assert isinstance(value, dict)
            return value

        async def accept_action():
            scope = (
                _approver_scope(prefix)
                if action_operation == "approve"
                else _customer_scope(prefix)
            )
            async with api_factory.request(scope) as session:
                backend_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
                await pid_queue.put(("action", backend_pid))
                if action_operation == "approve":
                    value = await ApprovalCommandCoordinator(session).decide(
                        tenant_id="tenant_demo",
                        approval_id=approval_id,
                        decision="approve",
                        actor_id="user_approver_demo",
                        idempotency_key=f"barrier-{prefix}",
                        reason="The persisted evidence and policy binding were reviewed.",
                        approver_note="Worker finalizer barrier proof.",
                        trace_id=f"trace-{prefix}-approve",
                    )
                else:
                    value = await ProposalWithdrawalCoordinator(session).withdraw(
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        principal_id="cust_demo",
                        approval_id=approval_id,
                        idempotency_key=f"barrier-{prefix}",
                        reason="The customer no longer wants this high-risk action.",
                        trace_id=f"trace-{prefix}-withdraw",
                    )
                await session.commit()
                return value

        async with admin.connect() as barrier:
            transaction = await barrier.begin()
            await barrier.execute(
                text(
                    "SELECT id FROM support_tickets "
                    "WHERE tenant_id='tenant_demo' AND id=:ticket_id FOR UPDATE"
                ),
                {"ticket_id": ticket_id},
            )
            finalizer_task = asyncio.create_task(finish_job())
            action_task = asyncio.create_task(accept_action())
            pids = dict([await pid_queue.get(), await pid_queue.get()])
            assert set(pids) == {"finalizer", "action"}
            for backend_pid in pids.values():
                assert await _wait_until_blocked(
                    admin,
                    backend_pid=backend_pid,
                )
            await transaction.rollback()
            finalizer_result, action_result = await asyncio.wait_for(
                asyncio.gather(finalizer_task, action_task),
                timeout=8,
            )

        assert finalizer_result["status"] == "retry_wait"
        assert action_result.reused is False
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, lease.job_id)
            assert approval is not None
            assert job is not None and job.status == "retry_wait"
            if action_operation == "approve":
                assert approval.status == "approved"
                assert action_result.job_id is not None
                assert (
                    int(
                        await session.scalar(
                            select(func.count(HumanDecision.id)).where(
                                HumanDecision.approval_id == approval_id
                            )
                        )
                        or 0
                    )
                    == 1
                )
            else:
                assert approval.status == "withdrawn"
                assert (
                    int(
                        await session.scalar(
                            select(func.count(ProposalWithdrawal.id)).where(
                                ProposalWithdrawal.approval_id == approval_id
                            )
                        )
                        or 0
                    )
                    == 1
                )
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


async def _seed_dead_approval_resume(
    factory,
    api_factory,
    worker,
    *,
    prefix: str,
) -> tuple[str, str, str, str, int, int]:
    approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
    accepted = await _approve(
        api_factory,
        prefix=prefix,
        approval_id=approval_id,
    )
    terminal_fence: int | None = None
    for attempt in range(1, 10):
        async with factory() as session, session.begin():
            await session.execute(
                update(RuntimeJob)
                .where(RuntimeJob.id == accepted.job_id)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=5))
            )
            lease = await RuntimeJobRepository(session).claim(
                job_id=accepted.job_id,
                owner=f"worker-{prefix}",
            )
        async with worker.begin() as connection:
            result = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"
                ),
                {
                    "job_id": accepted.job_id,
                    "owner": f"worker-{prefix}",
                    "fencing_token": lease.fencing_token,
                    "outcome": f"failed:barrier_fixture_{attempt}",
                },
            )
        assert isinstance(result, dict)
        if result.get("status") == "dead":
            terminal_fence = lease.fencing_token
            break
        assert result.get("status") == "retry_wait"
    assert terminal_fence is not None
    async with factory() as session:
        job = await session.get(RuntimeJob, accepted.job_id)
        assert job is not None and job.status == "dead"
        return (
            approval_id,
            ticket_id,
            accepted.run_id,
            accepted.job_id,
            terminal_fence,
            job.status_version,
        )


async def _dead_replay_aggregate_snapshot(
    factory,
    *,
    approval_id: str,
    ticket_id: str,
    run_id: str,
    job_id: str,
    event_id: str,
) -> dict[str, object]:
    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        run = await session.get(AgentRun, run_id)
        ticket = await session.get(SupportTicket, ticket_id)
        job = await session.get(RuntimeJob, job_id)
        event = await session.get(AgentEvent, event_id)
        assert approval is not None
        assert run is not None
        assert ticket is not None
        assert job is not None
        assert event is not None
        proposal = await session.get(ProposalRecord, approval.proposal_id)
        turn = await session.get(ConversationTurn, run.turn_id)
        assert proposal is not None
        assert turn is not None
        return {
            "approval": (approval.status, approval.status_version),
            "proposal": (proposal.status, proposal.status_version),
            "run": (
                run.status,
                run.status_version,
                run.active_job_id,
                run.active_fencing_token,
                run.agent_finish_reason,
                run.next_run_sequence,
            ),
            "turn": (
                turn.activity_state,
                turn.result_state,
                turn.completed_at,
            ),
            "ticket": (
                ticket.status,
                ticket.version,
                ticket.final_response,
                ticket.next_event_sequence,
            ),
            "job": (
                job.status,
                job.status_version,
                job.outcome,
                job.last_error,
            ),
            "event": (
                event.customer_id,
                event.ticket_sequence,
                event.run_sequence,
                event.previous_event_id,
                event.parent_event_hash,
                event.event_hash,
                event.payload_hash,
            ),
            "event_count": int(
                await session.scalar(
                    select(func.count(AgentEvent.id)).where(
                        AgentEvent.tenant_id == "tenant_demo",
                        AgentEvent.id == event_id,
                    )
                )
                or 0
            ),
            "failure_message_count": int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.tenant_id == "tenant_demo",
                        TicketMessage.publication_key == f"runtime-failure:{job_id}",
                    )
                )
                or 0
            ),
            "business_action_count": int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            ),
        }


@pytest.mark.asyncio
async def test_reconciler_and_worker_replay_share_ticket_barrier_without_deadlock() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_reconciler_barrier_{uuid4().hex[:8]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    reconciler = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    pid_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
    try:
        (
            approval_id,
            ticket_id,
            run_id,
            job_id,
            terminal_fence,
            status_version,
        ) = await _seed_dead_approval_resume(
            factory,
            api_factory,
            worker,
            prefix=prefix,
        )

        async def reconcile() -> dict[str, object]:
            async with reconciler.begin() as connection:
                backend_pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
                await pid_queue.put(("reconciler", backend_pid))
                value = await connection.scalar(
                    text(
                        "SELECT supportguard_reconciler_prepare("
                        ":job_id,:status_version,'delivery_recovery')"
                    ),
                    {
                        "job_id": job_id,
                        "status_version": status_version,
                    },
                )
            assert isinstance(value, dict)
            return value

        async def replay_worker() -> dict[str, object]:
            async with worker.begin() as connection:
                backend_pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
                await pid_queue.put(("worker", backend_pid))
                value = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": job_id,
                        "owner": f"worker-{prefix}",
                        "fencing_token": terminal_fence,
                        "outcome": "failed:barrier_terminal_replay",
                    },
                )
            assert isinstance(value, dict)
            return value

        async with admin.connect() as barrier:
            transaction = await barrier.begin()
            await barrier.execute(
                text(
                    "SELECT id FROM support_tickets "
                    "WHERE tenant_id='tenant_demo' AND id=:ticket_id FOR UPDATE"
                ),
                {"ticket_id": ticket_id},
            )
            reconciler_task = asyncio.create_task(reconcile())
            worker_task = asyncio.create_task(replay_worker())
            pids = dict([await pid_queue.get(), await pid_queue.get()])
            assert set(pids) == {"reconciler", "worker"}
            for backend_pid in pids.values():
                assert await _wait_until_blocked(
                    admin,
                    backend_pid=backend_pid,
                )
            await transaction.rollback()
            reconciler_result, worker_result = await asyncio.wait_for(
                asyncio.gather(reconciler_task, worker_task),
                timeout=8,
            )

        assert reconciler_result["result"] == "terminal_reconciled"
        assert worker_result["status"] == "dead"
        assert worker_result["outcome"] == "infrastructure_exhausted"
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, job_id)
            run = await session.get(AgentRun, run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            assert approval is not None and approval.status == "failed"
            assert job is not None and job.status == "dead"
            assert run is not None and run.status == "failed"
            assert ticket is not None and ticket.status == "failed"
            assert action_count == 0
    finally:
        await reconciler.dispose()
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_dead_approval_resume_converges_all_aggregates_and_publishes_once() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_dead_resume_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        accepted = await _approve(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
        )

        terminal: dict[str, object] | None = None
        terminal_fence: int | None = None
        for attempt in range(1, 10):
            async with factory() as session, session.begin():
                await session.execute(
                    update(RuntimeJob)
                    .where(RuntimeJob.id == accepted.job_id)
                    .values(available_at=datetime.now(UTC) - timedelta(seconds=5))
                )
                lease = await RuntimeJobRepository(session).claim(
                    job_id=accepted.job_id,
                    owner=f"worker-{prefix}",
                )
            async with worker.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": accepted.job_id,
                        "owner": f"worker-{prefix}",
                        "fencing_token": lease.fencing_token,
                        "outcome": f"failed:phase1_fixture_{attempt}",
                    },
                )
            assert isinstance(result, dict)
            if result.get("status") == "dead":
                terminal = result
                terminal_fence = lease.fencing_token
                break
            assert result.get("status") == "retry_wait"

        assert terminal is not None
        assert terminal_fence is not None
        publication_key = f"runtime-failure:{accepted.job_id}"
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            job = await session.get(RuntimeJob, accepted.job_id)
            run = await session.get(AgentRun, accepted.run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            turn = await session.get(
                ConversationTurn,
                run.turn_id if run is not None and run.turn_id is not None else "",
            )
            failure_messages = (
                await session.scalars(
                    select(TicketMessage).where(
                        TicketMessage.tenant_id == "tenant_demo",
                        TicketMessage.publication_key == publication_key,
                    )
                )
            ).all()
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )

            assert job is not None
            assert job.status == "dead"
            assert job.outcome == "infrastructure_exhausted"
            assert job.lease_owner is None
            assert job.lease_expires_at is None
            assert job.heartbeat_at is None
            assert run is not None
            assert run.status == "failed"
            assert run.active_job_id is None
            assert run.active_fencing_token is None
            assert turn is not None
            assert turn.activity_state == "failed"
            assert turn.result_state == "failed"
            assert approval.status == "failed"
            assert proposal is not None
            proposal_status = proposal.status
            assert ticket is not None
            assert ticket.status == "failed"
            assert ticket.lifecycle == "active"
            assert ticket.automation_mode == "agent"
            assert action_count == 0
            assert len(failure_messages) == 1
            assert failure_messages[0].role == "action"
            assert failure_messages[0].message_kind == "action_update"
            assert failure_messages[0].run_id == accepted.run_id
            assert failure_messages[0].turn_id == run.turn_id
            assert failure_messages[0].approval_id == approval_id
            failure_lines = failure_messages[0].content.splitlines()
            assert len(failure_lines) == 5
            assert failure_lines[0].startswith("已检查：")
            assert failure_lines[1].startswith("已确认：")
            assert failure_lines[2].startswith("仍未知（公开类别：runtime）：")
            assert failure_lines[3].startswith("审批与业务副作用状态：")
            assert "未记录与其绑定的成功业务动作" in failure_lines[3]
            assert failure_lines[4].startswith("可执行下一步：")
            assert "phase1_fixture" not in failure_messages[0].content
            failure_source_refs = failure_messages[0].source_refs

        replay: dict[str, object] | None = None
        replay_error: str | None = None
        try:
            async with worker.begin() as connection:
                replay_value = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": accepted.job_id,
                        "owner": f"worker-{prefix}",
                        "fencing_token": terminal_fence,
                        "outcome": "failed:phase1_replay",
                    },
                )
            if isinstance(replay_value, dict):
                replay = replay_value
            else:
                replay_error = f"unexpected replay result: {replay_value!r}"
        except DBAPIError as exc:
            replay_error = str(exc.orig)

        async with factory() as session:
            assert (
                int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.tenant_id == "tenant_demo",
                            TicketMessage.publication_key == publication_key,
                        )
                    )
                    or 0
                )
                == 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            failures: list[str] = []
            if replay is None:
                failures.append(f"terminal replay did not converge: {replay_error}")
            elif replay.get("job_id") != accepted.job_id or replay.get("status") != "dead":
                failures.append(f"terminal replay changed identity/state: {replay!r}")
            expected_source_refs = [
                {
                    "failure_category": "runtime",
                }
            ]
            if failure_source_refs != expected_source_refs:
                failures.append(
                    "runtime failure source_refs exposed a non-canonical reason: "
                    f"{failure_source_refs!r}"
                )
            if proposal_status != "stale":
                failures.append(f"dead approval left proposal non-terminal: {proposal_status!r}")
            assert failures == []
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered_identity",
    (
        "customer_id",
        "ticket_sequence",
        "run_sequence",
        "sequence",
        "previous_event_id",
        "event_hash",
        "parent_event_hash",
        "event_schema_version",
        "canonicalization_version",
        "event_hash_schema_version",
        "correlation_id",
        "causation_id",
        "idempotency_id",
        "fencing_token",
        "step_index",
        "tool_round",
        "delivery_generation",
    ),
)
async def test_dead_event_replay_rejects_tampered_chain_identity_without_aggregate_delta(
    tampered_identity: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    # Several capability-owned identifiers add their own prefixes.  Keep the
    # fixture identity below the narrowest varchar(64) boundary; the parameter
    # itself remains the explicit adversarial case identity.
    prefix = f"v1512_dead_event_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        accepted = await _approve(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
        )
        assert accepted.job_id is not None
        job_id = accepted.job_id
        async with factory() as session:
            ticket = await session.get(SupportTicket, ticket_id)
            run = await session.get(AgentRun, accepted.run_id)
            job = await session.get(RuntimeJob, job_id)
            parent = await session.scalar(
                select(AgentEvent)
                .where(
                    AgentEvent.tenant_id == "tenant_demo",
                    AgentEvent.ticket_id == ticket_id,
                )
                .order_by(AgentEvent.ticket_sequence.desc())
                .limit(1)
            )
            assert ticket is not None
            assert run is not None
            assert job is not None
            next_ticket_sequence = ticket.next_event_sequence + 1
            next_run_sequence = run.next_run_sequence + 1
            parent_hash = parent.event_hash if parent is not None else "0" * 64
            event_id = str(
                await session.scalar(
                    text(
                        "SELECT 'event_'||pg_catalog.md5('runtime-failure:tenant_demo:'||:job_id)"
                    ),
                    {"job_id": job_id},
                )
            )
            event = AgentEvent(
                id=event_id,
                tenant_id="tenant_demo",
                run_id=run.id,
                ticket_id=ticket.id,
                customer_id=ticket.customer_id,
                sequence=next_run_sequence,
                ticket_sequence=next_ticket_sequence,
                run_sequence=next_run_sequence,
                step_index=run.step_index,
                tool_round=run.tool_rounds,
                job_id=job_id,
                event_type="runtime_failed",
                status="completed",
                visibility="customer",
                payload={
                    "job_id": job_id,
                    "reason_code": "automatic_processing_failed",
                },
                payload_hash="",
                previous_event_id=(
                    parent.id if parent is not None and parent.run_id == run.id else None
                ),
                parent_event_hash=parent_hash,
                event_hash="",
                event_schema_version="support-ticket-event.v1",
                canonicalization_version="json-sort-keys.v1",
                event_hash_schema_version="event-hash.v1",
                correlation_id=run.id,
                causation_id=parent.id if parent is not None else None,
                idempotency_id=job_id,
                fencing_token=job.fencing_token,
                created_at=datetime.now(UTC),
            )
            if tampered_identity == "customer_id":
                event.customer_id = f"cust_tampered_{uuid4().hex[:8]}"
            elif tampered_identity == "ticket_sequence":
                event.ticket_sequence += 1
            elif tampered_identity == "run_sequence":
                event.run_sequence += 1
            elif tampered_identity == "sequence":
                event.sequence += 1
            elif tampered_identity == "previous_event_id":
                event.previous_event_id = f"event_tampered_{uuid4().hex[:8]}"
            elif tampered_identity == "parent_event_hash":
                event.parent_event_hash = "d" * 64
            elif tampered_identity == "event_schema_version":
                event.event_schema_version = "support-ticket-event.tampered"
            elif tampered_identity == "canonicalization_version":
                event.canonicalization_version = "json-tampered.v1"
            elif tampered_identity == "event_hash_schema_version":
                event.event_hash_schema_version = "event-hash.tampered"
            elif tampered_identity == "correlation_id":
                event.correlation_id = f"run_tampered_{uuid4().hex[:8]}"
            elif tampered_identity == "causation_id":
                event.causation_id = f"event_tampered_{uuid4().hex[:8]}"
            elif tampered_identity == "idempotency_id":
                event.idempotency_id = f"job_tampered_{uuid4().hex[:8]}"
            elif tampered_identity == "fencing_token":
                event.fencing_token = (job.fencing_token or 0) + 1
            elif tampered_identity == "step_index":
                event.step_index += 1
            elif tampered_identity == "tool_round":
                event.tool_round += 1
            elif tampered_identity == "delivery_generation":
                event.delivery_generation = 1
            event.payload_hash = _payload_hash(event.payload)
            event.event_hash = _event_hash(
                event=event,
                previous_event_hash=event.parent_event_hash,
            )
            if tampered_identity == "event_hash":
                event.event_hash = "e" * 64

        async with factory() as session, session.begin():
            # Controlled corruption simulates a row written by a faulty old
            # epoch.  Constraints are bypassed only while constructing the
            # adversarial state; the helper itself runs with normal guards.
            await session.execute(text("SET LOCAL session_replication_role='replica'"))
            session.add(event)
            await session.flush()
            await session.execute(
                update(SupportTicket)
                .where(
                    SupportTicket.tenant_id == "tenant_demo",
                    SupportTicket.id == ticket_id,
                )
                .values(next_event_sequence=event.ticket_sequence)
            )
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.tenant_id == "tenant_demo",
                    AgentRun.id == accepted.run_id,
                )
                .values(next_run_sequence=event.run_sequence)
            )
            await session.execute(
                update(RuntimeJob)
                .where(
                    RuntimeJob.tenant_id == "tenant_demo",
                    RuntimeJob.id == job_id,
                )
                .values(
                    status="dead",
                    outcome="infrastructure_exhausted",
                    last_error=f"controlled_chain_fault:{tampered_identity}",
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    status_version=RuntimeJob.status_version + 1,
                )
            )

        baseline = await _dead_replay_aggregate_snapshot(
            factory,
            approval_id=approval_id,
            ticket_id=ticket_id,
            run_id=accepted.run_id,
            job_id=job_id,
            event_id=event_id,
        )
        assert baseline["event_count"] == 1
        assert baseline["failure_message_count"] == 0
        assert baseline["business_action_count"] == 0

        async with admin.connect() as connection:
            transaction = await connection.begin()
            try:
                with pytest.raises(
                    DBAPIError,
                    match="dead_job_event_identity_conflict",
                ):
                    await connection.scalar(
                        text(
                            "SELECT public.supportguard_internal_converge_dead_job("
                            "'tenant_demo',:job_id)"
                        ),
                        {"job_id": job_id},
                    )
            finally:
                await transaction.rollback()

        assert (
            await _dead_replay_aggregate_snapshot(
                factory,
                approval_id=approval_id,
                ticket_id=ticket_id,
                run_id=accepted.run_id,
                job_id=job_id,
                event_id=event_id,
            )
            == baseline
        )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_agent_start_enqueued_before_approval_resume_owns_ticket_fifo_head() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_fifo_agent_first_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        followup = await _accept_followup(
            api_factory,
            prefix=prefix,
            ticket_id=ticket_id,
        )
        assert followup.job_id is not None
        decision = await _approve(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
        )
        await _assert_fifo_and_claim_head(
            factory,
            earlier_job_id=followup.job_id,
            later_job_id=decision.job_id,
            owner=f"worker-{prefix}",
        )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_approval_resume_enqueued_before_agent_start_owns_ticket_fifo_head() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_fifo_resume_first_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        decision = await _approve(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
        )
        followup = await _accept_followup(
            api_factory,
            prefix=prefix,
            ticket_id=ticket_id,
        )
        assert followup.job_id is not None
        await _assert_fifo_and_claim_head(
            factory,
            earlier_job_id=decision.job_id,
            later_job_id=followup.job_id,
            owner=f"worker-{prefix}",
        )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_reject_converges_original_aggregate_without_resume_and_activates_next_turn() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_pg_reject_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        followup_turn_id, followup_message_id = await _seed_accepted_followup(
            factory,
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
        )
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.run_id is not None
            original_run_id = approval.run_id
            original_turn_id = approval.origin_turn_id
            ticket_before = await session.get(SupportTicket, ticket_id)
            assert ticket_before is not None
            message_sequence_before = ticket_before.next_message_sequence
            jobs_before, outbox_before = await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            )
            assert (jobs_before, outbox_before) == (0, 0)

        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="reject",
                actor_id="user_approver_demo",
                idempotency_key=f"reject-{prefix}",
                reason="The evidence did not pass independent review.",
                approver_note="No business action is authorized.",
                trace_id=f"trace-{prefix}-reject",
            )
            await session.commit()
        assert accepted.job_id is None
        async with api_factory.request(_approver_scope(f"{prefix}-replay")) as session:
            replay = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="reject",
                actor_id="user_approver_demo",
                idempotency_key=f"reject-{prefix}",
                reason="The evidence did not pass independent review.",
                approver_note="No business action is authorized.",
                trace_id=f"trace-{prefix}-reject-replay",
            )
            await session.commit()
        assert replay.job_id is None
        assert replay.run_id == accepted.run_id
        assert replay.accepted_at == accepted.accepted_at
        assert replay.reused is True
        async with api_factory.request(_approver_scope(f"{prefix}-new-key")) as session:
            new_key_replay = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="reject",
                actor_id="user_approver_demo",
                idempotency_key=f"reject-{prefix}-new-key",
                reason="The evidence did not pass independent review.",
                approver_note="No business action is authorized.",
                trace_id=f"trace-{prefix}-reject-new-key",
            )
            await session.commit()
        assert new_key_replay.job_id is None
        assert new_key_replay.run_id == accepted.run_id
        assert new_key_replay.accepted_at == accepted.accepted_at
        assert new_key_replay.reused is True

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            original_run = await session.get(AgentRun, original_run_id)
            original_turn = await session.get(ConversationTurn, original_turn_id or "")
            followup_turn = await session.get(ConversationTurn, followup_turn_id)
            followup_message = await session.get(TicketMessage, followup_message_id)
            ticket = await session.get(SupportTicket, ticket_id)
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            resume_jobs, resume_outboxes = await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            )
            followup_jobs = (
                await session.scalars(
                    select(RuntimeJob).where(
                        RuntimeJob.run_id == followup_turn.run_id,
                        RuntimeJob.kind == "agent_start",
                    )
                )
            ).all()
            followup_outboxes = (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.job_id.in_([item.id for item in followup_jobs])
                    )
                )
            ).all()
            rejection_updates = (
                await session.scalars(
                    select(TicketMessage).where(
                        TicketMessage.publication_key == f"approval:{approval_id}:rejected"
                    )
                )
            ).all()

            assert approval.status == "rejected"
            assert proposal is not None and proposal.status == "stale"
            assert decision is not None and decision.decision == "reject"
            assert original_run is not None
            assert original_run.status == "completed"
            assert original_run.agent_finish_reason == "rejected"
            assert original_run.active_job_id is None
            assert original_run.active_fencing_token is None
            assert original_turn is not None
            assert original_turn.activity_state == "completed"
            assert original_turn.result_state == "rejected"
            assert followup_turn is not None
            assert followup_turn.activity_state == "queued"
            assert followup_turn.run_id is not None
            assert followup_message is not None
            assert followup_message.turn_id == followup_turn.id
            assert ticket is not None and ticket.status == "queued"
            assert (resume_jobs, resume_outboxes) == (0, 0)
            assert len(followup_jobs) == 1
            assert len(followup_outboxes) == 1
            assert len(rejection_updates) == 1
            assert rejection_updates[0].conversation_sequence == message_sequence_before + 1
            assert ticket.next_message_sequence == message_sequence_before + 1
            assert ticket.last_message_at >= rejection_updates[0].created_at
            await verify_ticket_event_chain(session, ticket_id)
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_approve_with_new_idempotency_key_replays_without_new_aggregate_delta() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_pg_approve_replay_{uuid4().hex[:8]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        reason = "The persisted evidence and policy binding were reviewed."
        note = "Approve replay must preserve the original aggregate."
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"approve-{prefix}",
                reason=reason,
                approver_note=note,
                trace_id=f"trace-{prefix}-approve",
            )
            await session.commit()
        assert accepted.job_id is not None

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            assert approval is not None and approval.run_id is not None
            assert decision is not None
            baseline = {
                "approval_status_version": approval.status_version,
                "decision_count": int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                ),
                "job_outbox_counts": await _approval_job_and_outbox_counts(
                    session,
                    approval_id=approval_id,
                ),
                "event_count": int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(
                            AgentEvent.run_id == approval.run_id
                        )
                    )
                    or 0
                ),
                "message_count": int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                ),
            }
            decision_created_at = decision.created_at

        new_key = f"approve-{prefix}-new-key"
        async with api_factory.request(_approver_scope(f"{prefix}-new-key")) as session:
            replay = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=new_key,
                reason=reason,
                approver_note=note,
                trace_id=f"trace-{prefix}-approve-new-key",
            )
            await session.commit()
        assert replay.reused is True
        assert replay.job_id == accepted.job_id
        assert replay.run_id == accepted.run_id
        assert replay.accepted_at == accepted.accepted_at

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            new_key_record = await session.scalar(
                select(IdempotencyRequest).where(
                    IdempotencyRequest.tenant_id == "tenant_demo",
                    IdempotencyRequest.principal_id == "user_approver_demo",
                    IdempotencyRequest.idempotency_key == new_key,
                )
            )
            assert approval is not None and approval.run_id is not None
            assert new_key_record is not None
            assert new_key_record.completed_at == decision_created_at
            assert new_key_record.response_snapshot["job_id"] == accepted.job_id
            assert approval.status_version == baseline["approval_status_version"]
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == baseline["decision_count"]
            )
            assert (
                await _approval_job_and_outbox_counts(
                    session,
                    approval_id=approval_id,
                )
                == baseline["job_outbox_counts"]
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(
                            AgentEvent.run_id == approval.run_id
                        )
                    )
                    or 0
                )
                == baseline["event_count"]
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                )
                == baseline["message_count"]
            )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_withdrawal_converges_every_aggregate_and_replays_without_new_writes() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_pg_withdraw_{uuid4().hex[:10]}"
    key = f"withdraw-{prefix}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.run_id is not None
            original_run_id = approval.run_id
            original_turn_id = approval.origin_turn_id
            status_version_before = approval.status_version
            ticket_before = await session.get(SupportTicket, ticket_id)
            assert ticket_before is not None
            message_sequence_before = ticket_before.next_message_sequence

        first = await _withdraw(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
            idempotency_key=key,
        )
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            run = await session.get(AgentRun, original_run_id)
            turn = await session.get(ConversationTurn, original_turn_id or "")
            ticket = await session.get(SupportTicket, ticket_id)
            withdrawal_count = int(
                await session.scalar(
                    select(func.count(ProposalWithdrawal.id)).where(
                        ProposalWithdrawal.approval_id == approval_id
                    )
                )
                or 0
            )
            message_count = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.publication_key == f"action:{approval_id}:withdrawn:1"
                    )
                )
                or 0
            )
            event_count = int(
                await session.scalar(
                    select(func.count(AgentEvent.id)).where(
                        AgentEvent.run_id == original_run_id,
                        AgentEvent.event_type == "proposal_withdrawn",
                    )
                )
                or 0
            )
            job_count, outbox_count = await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            )
            business_action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            human_decision_count = int(
                await session.scalar(
                    select(func.count(HumanDecision.id)).where(
                        HumanDecision.approval_id == approval_id
                    )
                )
                or 0
            )
            action_update = await session.scalar(
                select(TicketMessage).where(
                    TicketMessage.publication_key == f"action:{approval_id}:withdrawn:1"
                )
            )
            first_snapshot = {
                "approval_status_version": approval.status_version,
                "ticket_version": ticket.version if ticket is not None else None,
                "run_status_version": run.status_version if run is not None else None,
                "withdrawal_count": withdrawal_count,
                "message_count": message_count,
                "event_count": event_count,
            }

            assert approval.status == "withdrawn"
            assert approval.status_version == status_version_before + 1
            assert proposal is not None and proposal.status == "stale"
            assert run is not None
            assert run.status == "completed"
            assert run.agent_finish_reason == "withdrawn"
            assert run.active_job_id is None
            assert run.active_fencing_token is None
            assert turn is not None
            assert turn.activity_state == "completed"
            assert turn.result_state == "withdrawn"
            assert ticket is not None and ticket.status == "open"
            assert action_update is not None
            assert action_update.conversation_sequence == message_sequence_before + 1
            assert ticket.next_message_sequence == message_sequence_before + 1
            assert ticket.last_message_at >= action_update.created_at
            assert withdrawal_count == 1
            assert message_count == 1
            assert event_count == 1
            assert (job_count, outbox_count) == (0, 0)
            assert business_action_count == 0
            assert human_decision_count == 0
            await verify_ticket_event_chain(session, ticket_id)

        replay = await _withdraw(
            api_factory,
            prefix=f"{prefix}-replay",
            approval_id=approval_id,
            idempotency_key=key,
        )
        assert replay.withdrawal_id == first.withdrawal_id
        assert replay.accepted_at == first.accepted_at
        assert replay.reused is True
        new_key_replay = await _withdraw(
            api_factory,
            prefix=f"{prefix}-new-key",
            approval_id=approval_id,
            idempotency_key=f"{key}-new-key",
        )
        assert new_key_replay.withdrawal_id == first.withdrawal_id
        assert new_key_replay.accepted_at == first.accepted_at
        assert new_key_replay.reused is True

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            run = await session.get(AgentRun, original_run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            replay_snapshot = {
                "approval_status_version": approval.status_version if approval else None,
                "ticket_version": ticket.version if ticket else None,
                "run_status_version": run.status_version if run else None,
                "withdrawal_count": int(
                    await session.scalar(
                        select(func.count(ProposalWithdrawal.id)).where(
                            ProposalWithdrawal.approval_id == approval_id
                        )
                    )
                    or 0
                ),
                "message_count": int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.publication_key == f"action:{approval_id}:withdrawn:1"
                        )
                    )
                    or 0
                ),
                "event_count": int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(
                            AgentEvent.run_id == original_run_id,
                            AgentEvent.event_type == "proposal_withdrawn",
                        )
                    )
                    or 0
                ),
            }
            assert replay_snapshot == first_snapshot
    finally:
        await api.dispose()
        await admin.dispose()


async def _run_racing_approval(
    api_factory,
    *,
    prefix: str,
    approval_id: str,
    ready: asyncio.Future[int],
) -> tuple[str, str, object]:
    async with api_factory.request(_approver_scope(prefix)) as session:
        ready.set_result(int(await session.scalar(text("SELECT pg_backend_pid()")) or 0))
        try:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"approve-{prefix}",
                reason="The persisted evidence and policy binding were reviewed.",
                approver_note="Approval/withdrawal PostgreSQL barrier proof.",
                trace_id=f"trace-{prefix}-approval",
            )
            await session.commit()
            return "approve", "accepted", accepted
        except RuntimeConflict as exc:
            await session.rollback()
            return "approve", "conflict", exc.code


async def _run_racing_withdrawal(
    api_factory,
    *,
    prefix: str,
    approval_id: str,
    ready: asyncio.Future[int],
) -> tuple[str, str, object]:
    async with api_factory.request(_customer_scope(prefix)) as session:
        ready.set_result(int(await session.scalar(text("SELECT pg_backend_pid()")) or 0))
        try:
            accepted = await ProposalWithdrawalCoordinator(session).withdraw(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                principal_id="cust_demo",
                approval_id=approval_id,
                idempotency_key=f"withdraw-{prefix}",
                reason="The customer no longer wants this high-risk action.",
                trace_id=f"trace-{prefix}-withdrawal",
            )
            await session.commit()
            return "withdraw", "accepted", accepted
        except RuntimeConflict as exc:
            await session.rollback()
            return "withdraw", "conflict", exc.code


async def _run_racing_customer_message(
    api_factory,
    *,
    prefix: str,
    ticket_id: str,
    ready: asyncio.Future[int],
) -> tuple[str, str, object]:
    async with api_factory.request(_customer_scope(prefix)) as session:
        ready.set_result(int(await session.scalar(text("SELECT pg_backend_pid()")) or 0))
        try:
            accepted = await CommandCoordinator(
                session,
                provider_identity=("deterministic-fake", "fake", "native_fixture"),
            ).accept_message(
                ticket_id=ticket_id,
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=f"message-{prefix}",
                message=f"Please continue with the ordinary follow-up for {prefix}.",
                trace_id=f"trace-{prefix}-message",
            )
            await session.commit()
            return "message", "accepted", accepted
        except RuntimeConflict as exc:
            await session.rollback()
            return "message", "conflict", exc.code


@pytest.mark.asyncio
@pytest.mark.parametrize("first_entry", ["message", "approve"])
async def test_customer_message_and_approval_decision_share_ticket_barrier_and_converge(
    first_entry: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_msg_apr_{first_entry[:3]}_{uuid4().hex[:8]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        ),
        pool_size=4,
        max_overflow=0,
    )
    api_factory = create_scoped_session_factory(api)
    lock_connection = None
    lock_transaction = None
    tasks: list[asyncio.Task[tuple[str, str, object]]] = []
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        async with factory() as session:
            approval_before = await session.get(ApprovalRequest, approval_id)
            ticket_before = await session.get(SupportTicket, ticket_id)
            assert approval_before is not None
            assert ticket_before is not None
            assert approval_before.run_id is not None
            assert approval_before.origin_turn_id is not None
            original_run_id = approval_before.run_id
            original_turn_id = approval_before.origin_turn_id
            proposal_id = approval_before.proposal_id
            baseline = {
                "next_message_sequence": ticket_before.next_message_sequence,
                "next_dispatch_sequence": ticket_before.next_dispatch_sequence,
                "ticket_version": ticket_before.version,
                "next_event_sequence": ticket_before.next_event_sequence,
                "messages": int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                ),
                "turns": int(
                    await session.scalar(
                        select(func.count(ConversationTurn.id)).where(
                            ConversationTurn.ticket_id == ticket_id
                        )
                    )
                    or 0
                ),
                "runs": int(
                    await session.scalar(
                        select(func.count(AgentRun.id)).where(AgentRun.ticket_id == ticket_id)
                    )
                    or 0
                ),
                "jobs": int(
                    await session.scalar(
                        select(func.count(RuntimeJob.id)).where(RuntimeJob.ticket_id == ticket_id)
                    )
                    or 0
                ),
                "outbox": int(
                    await session.scalar(
                        select(func.count(OutboxEvent.id))
                        .join(RuntimeJob, RuntimeJob.id == OutboxEvent.job_id)
                        .where(RuntimeJob.ticket_id == ticket_id)
                    )
                    or 0
                ),
                "events": int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(AgentEvent.ticket_id == ticket_id)
                    )
                    or 0
                ),
            }

        lock_connection = await admin.connect()
        lock_transaction = await lock_connection.begin()
        await lock_connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        lock_pid = int(await lock_connection.scalar(text("SELECT pg_backend_pid()")) or 0)
        await lock_connection.execute(
            text(
                "SELECT id FROM support_tickets "
                "WHERE tenant_id='tenant_demo' AND id=:ticket_id FOR UPDATE"
            ),
            {"ticket_id": ticket_id},
        )

        loop = asyncio.get_running_loop()
        first_ready: asyncio.Future[int] = loop.create_future()
        second_ready: asyncio.Future[int] = loop.create_future()
        runners = {
            "message": lambda ready: _run_racing_customer_message(
                api_factory,
                prefix=f"{prefix}-message",
                ticket_id=ticket_id,
                ready=ready,
            ),
            "approve": lambda ready: _run_racing_approval(
                api_factory,
                prefix=f"{prefix}-approve",
                approval_id=approval_id,
                ready=ready,
            ),
        }
        second_entry = "approve" if first_entry == "message" else "message"
        first_task = asyncio.create_task(runners[first_entry](first_ready))
        tasks.append(first_task)
        first_pid = await asyncio.wait_for(first_ready, timeout=5)
        first_blockers = await _wait_until_blocked(admin, backend_pid=first_pid)
        assert lock_pid in first_blockers

        second_task = asyncio.create_task(runners[second_entry](second_ready))
        tasks.append(second_task)
        second_pid = await asyncio.wait_for(second_ready, timeout=5)
        second_blockers = await _wait_until_blocked(admin, backend_pid=second_pid)
        assert second_blockers
        assert not first_task.done()
        assert not second_task.done()

        await lock_transaction.commit()
        results = {
            item[0]: item[1:] for item in await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        }
        assert results["message"][0] == "accepted"
        assert results["approve"][0] == "accepted"
        message_result = results["message"][1]
        decision_result = results["approve"][1]
        assert message_result.reused is False
        assert message_result.run_id is not None
        assert message_result.job_id is not None
        assert decision_result.reused is False
        assert decision_result.job_id is not None

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            original_run = await session.get(AgentRun, original_run_id)
            original_turn = await session.get(ConversationTurn, original_turn_id)
            message_run = await session.get(AgentRun, message_result.run_id)
            message_job = await session.get(RuntimeJob, message_result.job_id)
            decision_job = await session.get(RuntimeJob, decision_result.job_id)
            ticket = await session.get(SupportTicket, ticket_id)
            assert approval is not None and approval.status == "approved"
            assert proposal is not None and proposal.status == "bound"
            assert original_run is not None and original_run.status == "queued"
            assert original_turn is not None
            assert original_turn.activity_state == "queued"
            assert message_run is not None and message_run.status == "queued"
            assert message_run.turn_id is not None
            message_turn = await session.get(ConversationTurn, message_run.turn_id)
            message_record = await session.get(TicketMessage, message_run.message_id)
            assert message_turn is not None
            assert message_record is not None
            assert message_turn.run_id == message_run.id
            assert message_turn.customer_message_id == message_record.id
            assert message_turn.activity_state == "queued"
            assert message_record.ticket_id == ticket_id
            assert message_record.turn_id == message_turn.id
            assert message_record.run_id == message_run.id
            assert message_record.message_kind == "customer"
            assert message_record.conversation_sequence == (baseline["next_message_sequence"] + 1)
            assert message_job is not None
            assert message_job.kind == "agent_start"
            assert message_job.approval_id is None
            assert message_job.run_id == message_run.id
            assert message_job.status == "queued"
            assert decision_job is not None
            assert decision_job.kind == "approval_resume"
            assert decision_job.approval_id == approval_id
            assert decision_job.run_id == original_run_id
            assert decision_job.status == "queued"
            assert message_job.ticket_id == decision_job.ticket_id == ticket_id
            ordered_jobs = (
                (message_job, decision_job)
                if first_entry == "message"
                else (decision_job, message_job)
            )
            assert ordered_jobs[0].dispatch_sequence == (baseline["next_dispatch_sequence"] + 1)
            assert ordered_jobs[1].dispatch_sequence == (baseline["next_dispatch_sequence"] + 2)
            assert ticket is not None
            assert ticket.lifecycle == "active"
            assert ticket.automation_mode == "agent"
            assert ticket.status == "queued"
            assert ticket.next_message_sequence == (baseline["next_message_sequence"] + 1)
            assert ticket.next_dispatch_sequence == (baseline["next_dispatch_sequence"] + 2)
            assert ticket.version >= baseline["ticket_version"] + 2
            assert ticket.next_event_sequence == baseline["next_event_sequence"] + 1
            assert (
                int(
                    await session.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == ticket_id
                        )
                    )
                    or 0
                )
                == baseline["messages"] + 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(ConversationTurn.id)).where(
                            ConversationTurn.ticket_id == ticket_id
                        )
                    )
                    or 0
                )
                == baseline["turns"] + 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(AgentRun.id)).where(AgentRun.ticket_id == ticket_id)
                    )
                    or 0
                )
                == baseline["runs"] + 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(RuntimeJob.id)).where(RuntimeJob.ticket_id == ticket_id)
                    )
                    or 0
                )
                == baseline["jobs"] + 2
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(OutboxEvent.id))
                        .join(RuntimeJob, RuntimeJob.id == OutboxEvent.job_id)
                        .where(RuntimeJob.ticket_id == ticket_id)
                    )
                    or 0
                )
                == baseline["outbox"] + 2
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(AgentEvent.id)).where(AgentEvent.ticket_id == ticket_id)
                    )
                    or 0
                )
                == baseline["events"] + 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id,
                            HumanDecision.decision == "approve",
                        )
                    )
                    or 0
                )
                == 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(ProposalWithdrawal.id)).where(
                            ProposalWithdrawal.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            idempotency_rows = list(
                (
                    await session.execute(
                        select(IdempotencyRequest).where(
                            IdempotencyRequest.tenant_id == "tenant_demo",
                            IdempotencyRequest.route.in_(
                                (
                                    f"POST /api/tickets/{ticket_id}/messages",
                                    f"POST /api/approvals/{approval_id}/decision",
                                )
                            ),
                            IdempotencyRequest.idempotency_key.in_(
                                (
                                    f"message-{prefix}-message",
                                    f"approve-{prefix}-approve",
                                )
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(idempotency_rows) == 2
            assert all(row.response_snapshot for row in idempotency_rows)
            assert all(row.completed_at is not None for row in idempotency_rows)
            await verify_ticket_event_chain(session, ticket_id)

        await _assert_fifo_and_claim_head(
            factory,
            earlier_job_id=ordered_jobs[0].id,
            later_job_id=ordered_jobs[1].id,
            owner=f"worker-{prefix}",
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if lock_transaction is not None and lock_transaction.is_active:
            await lock_transaction.rollback()
        if lock_connection is not None:
            await lock_connection.close()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["withdraw", "approve"])
async def test_approve_and_withdraw_barrier_race_has_one_winner_and_no_loser_writes(
    winner: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_race_{winner}_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        ),
        pool_size=4,
        max_overflow=0,
    )
    api_factory = create_scoped_session_factory(api)
    lock_connection = None
    lock_transaction = None
    tasks: list[asyncio.Task[tuple[str, str, object]]] = []
    try:
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        lock_connection = await admin.connect()
        lock_transaction = await lock_connection.begin()
        await lock_connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        lock_pid = int(await lock_connection.scalar(text("SELECT pg_backend_pid()")) or 0)
        await lock_connection.execute(
            text(
                "SELECT id FROM support_tickets "
                "WHERE tenant_id='tenant_demo' AND id=:ticket_id FOR UPDATE"
            ),
            {"ticket_id": ticket_id},
        )

        loop = asyncio.get_running_loop()
        first_ready: asyncio.Future[int] = loop.create_future()
        second_ready: asyncio.Future[int] = loop.create_future()
        runners = {
            "approve": _run_racing_approval,
            "withdraw": _run_racing_withdrawal,
        }
        loser = "approve" if winner == "withdraw" else "withdraw"
        first = asyncio.create_task(
            runners[winner](
                api_factory,
                prefix=f"{prefix}-{winner}",
                approval_id=approval_id,
                ready=first_ready,
            )
        )
        tasks.append(first)
        first_pid = await asyncio.wait_for(first_ready, timeout=5)
        first_blockers = await _wait_until_blocked(admin, backend_pid=first_pid)
        assert lock_pid in first_blockers

        second = asyncio.create_task(
            runners[loser](
                api_factory,
                prefix=f"{prefix}-{loser}",
                approval_id=approval_id,
                ready=second_ready,
            )
        )
        tasks.append(second)
        second_pid = await asyncio.wait_for(second_ready, timeout=5)
        assert await _wait_until_blocked(admin, backend_pid=second_pid)
        assert not first.done()
        assert not second.done()

        await lock_transaction.commit()
        results = {
            item[0]: item[1:] for item in await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        }
        assert results[winner][0] == "accepted"
        assert results[loser][0] == "conflict"
        if loser == "approve":
            assert results[loser][1] in {
                "approval_decision_conflict",
                "checkpoint_binding_conflict",
            }
        else:
            assert results[loser][1] == "proposal_withdrawal_conflict"

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            decision_count = int(
                await session.scalar(
                    select(func.count(HumanDecision.id)).where(
                        HumanDecision.approval_id == approval_id
                    )
                )
                or 0
            )
            withdrawal_count = int(
                await session.scalar(
                    select(func.count(ProposalWithdrawal.id)).where(
                        ProposalWithdrawal.approval_id == approval_id
                    )
                )
                or 0
            )
            job_count, outbox_count = await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            )
            business_action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            loser_key = f"{loser}-{prefix}-{loser}"
            loser_idempotency_count = int(
                await session.scalar(
                    select(func.count(IdempotencyRequest.id)).where(
                        IdempotencyRequest.idempotency_key == loser_key
                    )
                )
                or 0
            )

            assert business_action_count == 0
            assert loser_idempotency_count == 0
            if winner == "withdraw":
                assert approval.status == "withdrawn"
                assert proposal is not None and proposal.status == "stale"
                assert decision_count == 0
                assert withdrawal_count == 1
                assert (job_count, outbox_count) == (0, 0)
            else:
                assert approval.status == "approved"
                assert proposal is not None and proposal.status == "bound"
                assert decision_count == 1
                assert withdrawal_count == 0
                assert (job_count, outbox_count) == (1, 1)
    finally:
        if lock_transaction is not None and lock_transaction.is_active:
            await lock_transaction.rollback()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if lock_connection is not None:
            await lock_connection.close()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_edit_and_approve_commits_one_new_revision_and_binds_decision_job_outbox() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_pg_edit_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, _ = await _seed_pending_approval(factory, prefix)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.selected_revision_id is not None
            base_revision = await session.get(
                ApprovalActionRevision,
                approval.selected_revision_id,
            )
            assert base_revision is not None
            base_revision_id = base_revision.id
            base_payload = dict(base_revision.action_payload)
            base_hash = base_revision.action_hash

        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="edit_and_approve",
                actor_id="user_approver_demo",
                idempotency_key=f"edit-{prefix}",
                reason="The refund reason was clarified without changing authorization.",
                approver_note="Only refund_reason is editable.",
                trace_id=f"trace-{prefix}-edit",
                edited_payload={"refund_reason": "Duplicate charge confirmed by billing lineage."},
            )
            await session.commit()

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            revisions = (
                await session.scalars(
                    select(ApprovalActionRevision)
                    .where(ApprovalActionRevision.approval_id == approval_id)
                    .order_by(ApprovalActionRevision.revision_number)
                )
            ).all()
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            job = await session.get(RuntimeJob, accepted.job_id or "")
            outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.job_id == accepted.job_id)
            )

            assert accepted.decision == "edit_and_approve"
            assert approval.status == "approved"
            assert approval.selected_revision_number == 1
            assert approval.selected_revision_id != base_revision_id
            assert len(revisions) == 2
            assert revisions[0].id == base_revision_id
            assert revisions[0].action_payload == base_payload
            assert revisions[0].action_hash == base_hash
            assert revisions[1].id == approval.selected_revision_id
            assert revisions[1].resource_version == approval.business_version
            assert revisions[1].action_payload["refund_reason"] == (
                "Duplicate charge confirmed by billing lineage."
            )
            assert {
                key: value
                for key, value in revisions[1].action_payload.items()
                if key != "refund_reason"
            } == {key: value for key, value in base_payload.items() if key != "refund_reason"}
            assert decision is not None
            assert decision.decision == "edit_and_approve"
            assert decision.action_revision_id == revisions[1].id
            assert decision.action_hash == revisions[1].action_hash
            assert job is not None
            assert job.kind == "approval_resume"
            assert job.approval_id == approval_id
            assert outbox is not None
            assert outbox.run_id == approval.run_id
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain_error", "expected_error"),
    [
        ("checkpoint", "checkpoint_binding_conflict"),
        ("revision", "approval_revision_binding_conflict"),
        ("event", "event_chain_conflict"),
        ("edit", "approval_edit_not_allowed"),
    ],
)
async def test_domain_error_does_not_poison_idempotency_and_same_key_retry_recovers(
    domain_error: str,
    expected_error: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_domain_{domain_error}_{uuid4().hex[:8]}"
    key = f"domain-{prefix}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    original: object | None = None
    try:
        approval_id, _ = await _seed_pending_approval(factory, prefix)
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            approval = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update()
            )
            assert approval is not None and approval.run_id is not None
            if domain_error == "checkpoint":
                run = await session.get(AgentRun, approval.run_id, with_for_update=True)
                assert run is not None
                original = run.canonical_checkpoint_hash
                run.canonical_checkpoint_hash = "f" * 64
            elif domain_error == "revision":
                original = (
                    approval.selected_revision_id,
                    approval.selected_revision_number,
                )
                await session.execute(
                    text(
                        "UPDATE approval_requests "
                        "SET selected_revision_id=NULL,selected_revision_number=NULL "
                        "WHERE tenant_id=:tenant_id AND id=:approval_id"
                    ),
                    {
                        "tenant_id": approval.tenant_id,
                        "approval_id": approval.id,
                    },
                )
            elif domain_error == "event":
                ticket = await session.get(
                    SupportTicket,
                    approval.ticket_id,
                    with_for_update=True,
                )
                assert ticket is not None
                original = ticket.next_event_sequence
                ticket.next_event_sequence += 1

        decision = "edit_and_approve" if domain_error == "edit" else "approve"
        invalid_edit = {"unexpected": "not allowed"} if domain_error == "edit" else None
        failed = await _raw_decide_and_commit(
            api_factory,
            prefix=f"{prefix}-failed",
            approval_id=approval_id,
            decision=decision,
            idempotency_key=key,
            edited_payload=invalid_edit,
        )
        assert failed == {"error_code": expected_error}

        async with factory() as session:
            assert (
                int(
                    await session.scalar(
                        select(func.count(IdempotencyRequest.id)).where(
                            IdempotencyRequest.tenant_id == "tenant_demo",
                            IdempotencyRequest.principal_id == "user_approver_demo",
                            IdempotencyRequest.idempotency_key == key,
                        )
                    )
                    or 0
                )
                == 0
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            assert await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            ) == (0, 0)

        if domain_error != "edit":
            async with factory() as session, session.begin():
                await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
                approval = await session.scalar(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.id == approval_id)
                    .with_for_update()
                )
                assert approval is not None and approval.run_id is not None
                if domain_error == "checkpoint":
                    run = await session.get(
                        AgentRun,
                        approval.run_id,
                        with_for_update=True,
                    )
                    assert run is not None
                    run.canonical_checkpoint_hash = str(original)
                elif domain_error == "revision":
                    revision_id, revision_number = original  # type: ignore[misc]
                    await session.execute(
                        text(
                            "UPDATE approval_requests "
                            "SET selected_revision_id=:revision_id,"
                            "selected_revision_number=:revision_number "
                            "WHERE tenant_id=:tenant_id AND id=:approval_id"
                        ),
                        {
                            "revision_id": str(revision_id),
                            "revision_number": int(revision_number),
                            "tenant_id": approval.tenant_id,
                            "approval_id": approval.id,
                        },
                    )
                else:
                    ticket = await session.get(
                        SupportTicket,
                        approval.ticket_id,
                        with_for_update=True,
                    )
                    assert ticket is not None
                    ticket.next_event_sequence = int(original)

        valid_edit = (
            {"refund_reason": "Duplicate charge confirmed by billing lineage."}
            if domain_error == "edit"
            else None
        )
        async with api_factory.request(_approver_scope(f"{prefix}-retry")) as session:
            recovered = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision=decision,
                actor_id="user_approver_demo",
                idempotency_key=key,
                reason=f"Phase 1 PostgreSQL {decision} contract proof.",
                approver_note=("The persisted evidence and policy binding were reviewed."),
                trace_id=f"trace-{prefix}-recovered",
                edited_payload=valid_edit,
            )
            await session.commit()
        assert recovered.reused is False

        async with factory() as session:
            idempotency = await session.scalar(
                select(IdempotencyRequest).where(
                    IdempotencyRequest.tenant_id == "tenant_demo",
                    IdempotencyRequest.principal_id == "user_approver_demo",
                    IdempotencyRequest.idempotency_key == key,
                )
            )
            assert idempotency is not None
            assert idempotency.response_snapshot
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 1
            )
            assert await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            ) == (1, 1)
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_legacy_accept_message_capability_is_not_executable_by_api_role() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(database_url)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    try:
        async with admin.connect() as connection:
            has_execute = await connection.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'supportguard_api',"
                    "'public.supportguard_api_accept_message(text,jsonb)',"
                    "'EXECUTE')"
                )
            )
        assert has_execute is False
        with pytest.raises(DBAPIError, match="permission denied"):
            async with api.begin() as connection:
                await connection.scalar(
                    text(
                        "SELECT supportguard_api_accept_message("
                        "'ticket_capability_negative','{}'::jsonb)"
                    )
                )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_database_capability_rejects_new_manual_takeover_without_writes() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_manual_takeover_{uuid4().hex[:10]}"
    key = f"manual-takeover-{prefix}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    try:
        approval_id, _ = await _seed_pending_approval(factory, prefix)
        rejected = await _raw_decide_and_commit(
            api_factory,
            prefix=prefix,
            approval_id=approval_id,
            decision="manual_takeover",
            idempotency_key=key,
        )
        assert rejected == {"error_code": "manual_takeover_public_unsupported"}
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status == "pending"
            assert (
                int(
                    await session.scalar(
                        select(func.count(HumanDecision.id)).where(
                            HumanDecision.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
            assert await _approval_job_and_outbox_counts(
                session,
                approval_id=approval_id,
            ) == (0, 0)
            assert (
                int(
                    await session.scalar(
                        select(func.count(IdempotencyRequest.id)).where(
                            IdempotencyRequest.idempotency_key == key
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_approval_transition_core_trigger_acl_and_cas_matrix() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_transition_matrix_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    restricted_roles = (
        "supportguard_api",
        "supportguard_worker",
        "supportguard_reconciler",
        "supportguard_action_mcp",
    )
    role_engines = {
        role: create_async_engine(_role_url(database_url, username=role, password=role))
        for role in restricted_roles
    }
    try:
        approval_id, _ = await _seed_pending_approval(factory, prefix)
        manual_approval_id, _ = await _seed_pending_approval(
            factory,
            f"{prefix}_manual",
        )

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            manual_approval = await session.get(
                ApprovalRequest,
                manual_approval_id,
            )
            assert approval is not None
            assert manual_approval is not None
            # These rows traversed the real BEFORE INSERT trigger.
            assert (approval.status, approval.status_version) == ("pending", 1)
            assert (
                manual_approval.status,
                manual_approval.status_version,
            ) == ("pending", 1)

        clone_ids: list[str] = []
        for clone_status, clone_version in (
            ("approved", 1),
            ("pending", 2),
            ("manual_takeover", 1),
        ):
            clone_id = f"approval_{prefix}_{clone_status}_{clone_version}"
            clone_ids.append(clone_id)
            with pytest.raises(DBAPIError, match="approval_insert_state_invalid"):
                async with admin.begin() as connection:
                    await connection.execute(
                        text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO public.approval_requests
                            SELECT (
                              jsonb_populate_record(
                                NULL::public.approval_requests,
                                to_jsonb(source)||jsonb_build_object(
                                  'id',CAST(:clone_id AS text),
                                  'idempotency_key',CAST(:key AS text),
                                  'status',CAST(:status AS text),
                                  'status_version',CAST(:version AS bigint)
                                )
                              )
                            ).*
                            FROM public.approval_requests source
                            WHERE source.id=:source_id
                            """
                        ),
                        {
                            "clone_id": clone_id,
                            "key": f"key-{clone_id}",
                            "status": clone_status,
                            "version": clone_version,
                            "source_id": approval_id,
                        },
                    )

        with pytest.raises(DBAPIError, match="approval_same_state_version_invalid"):
            async with admin.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                )
                await connection.execute(
                    text(
                        "UPDATE public.approval_requests "
                        "SET status_version=status_version+1 "
                        "WHERE id=:approval_id"
                    ),
                    {"approval_id": approval_id},
                )

        with pytest.raises(DBAPIError, match="approval_status_transition_invalid"):
            async with admin.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                )
                await connection.execute(
                    text(
                        "UPDATE public.approval_requests "
                        "SET status='manual_takeover',status_version=status_version+1 "
                        "WHERE id=:approval_id"
                    ),
                    {"approval_id": manual_approval_id},
                )

        async with admin.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            won = (
                await connection.execute(
                    text(
                        "UPDATE public.approval_requests "
                        "SET status='rejected',status_version=status_version+1 "
                        "WHERE id=:approval_id AND status='pending' AND status_version=1 "
                        "RETURNING status,status_version"
                    ),
                    {"approval_id": approval_id},
                )
            ).one()
            assert tuple(won) == ("rejected", 2)

        async with admin.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            stale = (
                await connection.execute(
                    text(
                        "UPDATE public.approval_requests "
                        "SET status='approved',status_version=status_version+1 "
                        "WHERE id=:approval_id AND status='pending' AND status_version=1 "
                        "RETURNING status,status_version"
                    ),
                    {"approval_id": approval_id},
                )
            ).one_or_none()
            assert stale is None

        with pytest.raises(DBAPIError, match="approval_status_transition_invalid"):
            async with admin.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                )
                await connection.execute(
                    text(
                        "UPDATE public.approval_requests "
                        "SET status='pending',status_version=status_version+1 "
                        "WHERE id=:approval_id"
                    ),
                    {"approval_id": approval_id},
                )

        signature = (
            "public.supportguard_internal_validate_approval_transition("
            "text,text,text,bigint,bigint)"
        )
        async with admin.connect() as connection:
            privileges = {
                role: bool(
                    await connection.scalar(
                        text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                        {"role": role, "signature": signature},
                    )
                )
                for role in restricted_roles
            }
        assert privileges == {role: False for role in restricted_roles}
        for _role, engine in role_engines.items():
            with pytest.raises(DBAPIError, match="permission denied"):
                async with engine.begin() as connection:
                    await connection.scalar(
                        text(
                            "SELECT public."
                            "supportguard_internal_validate_approval_transition("
                            "'pending','pending','UPDATE',1,1)"
                        )
                    )

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            manual_approval = await session.get(
                ApprovalRequest,
                manual_approval_id,
            )
            clone_count = int(
                await session.scalar(
                    select(func.count(ApprovalRequest.id)).where(ApprovalRequest.id.in_(clone_ids))
                )
                or 0
            )
            assert approval is not None
            assert (approval.status, approval.status_version) == ("rejected", 2)
            assert manual_approval is not None
            assert (
                manual_approval.status,
                manual_approval.status_version,
            ) == ("pending", 1)
            assert clone_count == 0
    finally:
        for engine in role_engines.values():
            await engine.dispose()
        await admin.dispose()


async def _seed_reconciler_lane(
    connection,
    *,
    prefix: str,
    count: int,
    effect_lane: bool,
    available_at: datetime,
) -> tuple[str, str]:
    """Persist a real trigger-bound ticket/run lane and its bounded backlog."""
    await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
    ticket_id = f"ticket_{prefix}"
    message_id = f"message_{prefix}"
    run_id = f"run_{prefix}"
    status = "succeeded" if effect_lane else "queued"
    outcome = "verification_pending" if effect_lane else None
    await connection.execute(
        text(
            """
            INSERT INTO public.support_tickets(
              id,tenant_id,customer_id,status,issue_type,risk,version,
              next_event_sequence,created_at,updated_at
            ) VALUES (
              :ticket_id,'tenant_demo','cust_demo','queued','unknown','low',1,0,
              CAST(:available_at AS timestamptz),CAST(:available_at AS timestamptz)
            )
            """
        ),
        {"ticket_id": ticket_id, "available_at": available_at},
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.ticket_messages(
              id,tenant_id,ticket_id,role,content,source_refs,created_at,updated_at
            ) VALUES (
              :message_id,'tenant_demo',:ticket_id,'user',
              'v1.5.12 reconciler fairness fixture','[]'::jsonb,
              CAST(:available_at AS timestamptz),CAST(:available_at AS timestamptz)
            )
            """
        ),
        {
            "message_id": message_id,
            "ticket_id": ticket_id,
            "available_at": available_at,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.agent_runs(
              id,tenant_id,ticket_id,customer_id,message_id,status,model,
              provider_mode,tool_call_mode,prompt_version,schema_version,
              context_version,checkpoint_stage,status_version,next_run_sequence,
              canonical_checkpoint_version,step_index,tool_rounds,tool_attempts,
              llm_calls,created_at,updated_at
            ) VALUES (
              :run_id,'tenant_demo',:ticket_id,'cust_demo',:message_id,'queued',
              'fake','fake','native_fixture','v1512','agent.v1','context.v1.5.12',
              'request_created',1,0,0,0,0,0,0,
              CAST(:available_at AS timestamptz),CAST(:available_at AS timestamptz)
            )
            """
        ),
        {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "message_id": message_id,
            "available_at": available_at,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.runtime_jobs(
              id,tenant_id,run_id,kind,status,attempt,available_at,fencing_token,
              timing_version,status_version,outcome,terminal_at,created_at,updated_at
            )
            SELECT
              'job_'||CAST(:prefix AS text)||'_'||lpad(series.value::text,4,'0'),
              'tenant_demo',:run_id,'agent_start',CAST(:status AS text),0,
              CAST(:available_at AS timestamptz),0,1,1,
              CAST(:outcome AS text),
              CASE WHEN CAST(:status AS text)='succeeded'
                   THEN CAST(:available_at AS timestamptz) ELSE NULL END,
              CAST(:available_at AS timestamptz),CAST(:available_at AS timestamptz)
            FROM generate_series(1,CAST(:count AS integer)) AS series(value)
            """
        ),
        {
            "prefix": prefix,
            "run_id": run_id,
            "status": status,
            "outcome": outcome,
            "available_at": available_at,
            "count": count,
        },
    )
    if not effect_lane:
        await connection.execute(
            text(
                """
                INSERT INTO public.outbox_events(
                  id,delivery_id,tenant_id,job_id,run_id,delivery_generation,
                  event_type,schema_version,payload,available_at,publish_attempts,
                  delivery_state_version,created_at,updated_at
                )
                SELECT
                  'outbox_'||CAST(:prefix AS text)||'_'||lpad(series.value::text,4,'0'),
                  'delivery_'||CAST(:prefix AS text)||'_'||lpad(series.value::text,4,'0'),
                  'tenant_demo',
                  'job_'||CAST(:prefix AS text)||'_'||lpad(series.value::text,4,'0'),
                  :run_id,1,'runtime_job_available','runtime-job.v1','{}'::jsonb,
                  CAST(:available_at AS timestamptz),0,1,
                  CAST(:available_at AS timestamptz),CAST(:available_at AS timestamptz)
                FROM generate_series(1,CAST(:count AS integer)) AS series(value)
                """
            ),
            {
                "prefix": prefix,
                "run_id": run_id,
                "available_at": available_at,
                "count": count,
            },
        )
    return ticket_id, run_id


async def _neutralize_reconciler_lane(
    connection,
    *,
    ticket_id: str,
    run_id: str,
    prefix: str,
    effect_lane: bool,
) -> None:
    if effect_lane:
        await connection.execute(
            text(
                """
                UPDATE public.runtime_jobs
                SET available_at=CAST('2200-01-01T00:00:00Z' AS timestamptz),
                    status_version=status_version+1,
                    updated_at=clock_timestamp()
                WHERE id LIKE :job_pattern
                  AND status='succeeded' AND outcome='verification_pending'
                """
            ),
            {"job_pattern": f"job_{prefix}_%"},
        )
        return
    await connection.execute(
        text(
            """
            UPDATE public.agent_runs
            SET status='completed',agent_finish_reason='completed',
                completed_at=clock_timestamp(),status_version=status_version+1,
                updated_at=clock_timestamp()
            WHERE id=:run_id
            """
        ),
        {"run_id": run_id},
    )
    await connection.execute(
        text(
            """
            UPDATE public.support_tickets
            SET status='resolved',final_response='Fairness fixture converged.',
                version=version+1,updated_at=clock_timestamp()
            WHERE id=:ticket_id
            """
        ),
        {"ticket_id": ticket_id},
    )
    await connection.execute(
        text(
            """
            UPDATE public.runtime_jobs
            SET status='succeeded',outcome='completed',terminal_at=clock_timestamp(),
                status_version=status_version+1,updated_at=clock_timestamp()
            WHERE id LIKE :job_pattern AND status='queued'
            """
        ),
        {"job_pattern": f"job_{prefix}_%"},
    )


@pytest.mark.asyncio
async def test_reconciler_lane_fairness_and_effect_retry_backoff_contract() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    suffix = uuid4().hex[:8]
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    reconciler = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
    )
    first_effect = f"v1512_fe_old_{suffix}"
    first_recovery = f"v1512_fr_new_{suffix}"
    second_recovery = f"v1512_fr_old_{suffix}"
    second_effect = f"v1512_fe_new_{suffix}"
    try:
        async with admin.begin() as connection:
            first_effect_ticket, first_effect_run = await _seed_reconciler_lane(
                connection,
                prefix=first_effect,
                count=501,
                effect_lane=True,
                available_at=datetime(1800, 1, 1, tzinfo=UTC),
            )
            first_recovery_ticket, first_recovery_run = await _seed_reconciler_lane(
                connection,
                prefix=first_recovery,
                count=1,
                effect_lane=False,
                available_at=datetime(1801, 1, 1, tzinfo=UTC),
            )

        async with reconciler.connect() as connection:
            first_batch = (
                (
                    await connection.execute(
                        text("SELECT * FROM supportguard_reconciler_candidates(2)")
                    )
                )
                .mappings()
                .all()
            )
        assert len(first_batch) == 2
        assert sum(row["job_id"].startswith(f"job_{first_effect}_") for row in first_batch) == 1
        assert sum(row["job_id"].startswith(f"job_{first_recovery}_") for row in first_batch) == 1
        first_delivery = next(
            row for row in first_batch if row["job_id"].startswith(f"job_{first_recovery}_")
        )
        assert first_delivery["outbox_id"] is not None
        assert first_delivery["delivery_id"] is not None

        async with admin.begin() as connection:
            await _neutralize_reconciler_lane(
                connection,
                ticket_id=first_effect_ticket,
                run_id=first_effect_run,
                prefix=first_effect,
                effect_lane=True,
            )
            await _neutralize_reconciler_lane(
                connection,
                ticket_id=first_recovery_ticket,
                run_id=first_recovery_run,
                prefix=first_recovery,
                effect_lane=False,
            )
            second_recovery_ticket, second_recovery_run = await _seed_reconciler_lane(
                connection,
                prefix=second_recovery,
                count=501,
                effect_lane=False,
                available_at=datetime(1700, 1, 1, tzinfo=UTC),
            )
            second_effect_ticket, second_effect_run = await _seed_reconciler_lane(
                connection,
                prefix=second_effect,
                count=1,
                effect_lane=True,
                available_at=datetime(1701, 1, 1, tzinfo=UTC),
            )

        async with reconciler.connect() as connection:
            second_batch = (
                (
                    await connection.execute(
                        text("SELECT * FROM supportguard_reconciler_candidates(2)")
                    )
                )
                .mappings()
                .all()
            )
        assert len(second_batch) == 2
        assert sum(row["job_id"].startswith(f"job_{second_recovery}_") for row in second_batch) == 1
        assert sum(row["job_id"].startswith(f"job_{second_effect}_") for row in second_batch) == 1

        async with admin.begin() as connection:
            await _neutralize_reconciler_lane(
                connection,
                ticket_id=second_recovery_ticket,
                run_id=second_recovery_run,
                prefix=second_recovery,
                effect_lane=False,
            )
            await _neutralize_reconciler_lane(
                connection,
                ticket_id=second_effect_ticket,
                run_id=second_effect_run,
                prefix=second_effect,
                effect_lane=True,
            )

        fixture = await _prepare_unknown_action_effect(
            database_url,
            prefix=f"v1512_backoff_{suffix}",
            action_type="refund",
            evidence="business_action_only",
        )
        async with reconciler.begin() as connection:
            due_before = {
                row["job_id"]
                for row in (
                    (
                        await connection.execute(
                            text("SELECT * FROM supportguard_reconciler_candidates(500)")
                        )
                    )
                    .mappings()
                    .all()
                )
            }
            clock_before = await connection.scalar(text("SELECT clock_timestamp()"))
            prepared = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:status_version,'action_effect_reconciliation')"
                ),
                {
                    "job_id": fixture.job_id,
                    "status_version": fixture.job_status_version,
                },
            )
            clock_after = await connection.scalar(text("SELECT clock_timestamp()"))
            due_after = {
                row["job_id"]
                for row in (
                    (
                        await connection.execute(
                            text("SELECT * FROM supportguard_reconciler_candidates(500)")
                        )
                    )
                    .mappings()
                    .all()
                )
            }
        assert fixture.job_id in due_before
        assert isinstance(prepared, dict)
        assert prepared["result"] == "verification_pending"
        assert prepared["job_id"] == fixture.job_id
        assert fixture.job_id not in due_after

        async with admin_factory() as session:
            job = await session.get(RuntimeJob, fixture.job_id)
            assert job is not None
            assert job.status == "succeeded"
            assert job.outcome == "verification_pending"
            assert job.status_version == fixture.job_status_version + 1
            assert clock_before is not None
            assert clock_after is not None
            assert job.available_at >= clock_before + timedelta(seconds=29)
            assert job.available_at <= clock_after + timedelta(seconds=31)
    finally:
        await reconciler.dispose()
        await admin.dispose()


def _postgres_plan_root(raw_plan: object) -> dict[str, object]:
    if isinstance(raw_plan, str):
        raw_plan = json.loads(raw_plan)
    assert isinstance(raw_plan, list)
    assert len(raw_plan) == 1
    envelope = raw_plan[0]
    assert isinstance(envelope, dict)
    root = envelope.get("Plan")
    assert isinstance(root, dict)
    return root


def _postgres_plan_nodes(root: dict[str, object]) -> list[dict[str, object]]:
    nodes = [root]
    children = root.get("Plans", [])
    assert isinstance(children, list)
    for child in children:
        assert isinstance(child, dict)
        nodes.extend(_postgres_plan_nodes(child))
    return nodes


def _assert_natural_indexed_limit_plan(
    raw_plan: object,
    *,
    expected_indexes: str | frozenset[str],
    upper_bound: int,
) -> list[dict[str, object]]:
    root = _postgres_plan_root(raw_plan)
    nodes = _postgres_plan_nodes(root)
    eligible_indexes = (
        frozenset({expected_indexes}) if isinstance(expected_indexes, str) else expected_indexes
    )
    assert root.get("Node Type") == "Limit"
    assert int(root.get("Actual Loops", 0)) == 1
    assert int(root.get("Actual Rows", upper_bound + 1)) <= upper_bound
    assert root.get("Plan Rows") is None or int(root["Plan Rows"]) <= upper_bound
    assert all(node.get("Node Type") != "Seq Scan" for node in nodes)
    matching_nodes = [
        node
        for node in nodes
        if node.get("Index Name") in eligible_indexes
        and node.get("Node Type") in {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}
    ]
    assert matching_nodes, {
        "eligible_indexes": sorted(eligible_indexes),
        "plan_nodes": [
            {
                "node_type": node.get("Node Type"),
                "index_name": node.get("Index Name"),
                "index_cond": node.get("Index Cond"),
            }
            for node in nodes
        ],
    }
    return nodes


@pytest.mark.asyncio
async def test_projector_queries_naturally_use_existing_indexes_and_are_bounded() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    suffix = uuid4().hex[:8]
    target_lane = f"v1512_ix_target_{suffix}"
    background_lane = f"v1512_ix_background_{suffix}"
    target_job_count = 64
    background_job_count = 2048
    history_count = 2048
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    fixture = await _prepare_unknown_action_effect(
        database_url,
        prefix=f"v1512_ix_action_{suffix}",
        action_type="refund",
        evidence="business_action_only",
    )
    try:
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, fixture.approval_id)
            action = await session.scalar(
                select(BusinessAction).where(BusinessAction.approval_id == fixture.approval_id)
            )
            assert approval is not None
            assert action is not None
            approval_identity = {
                "tenant_id": approval.tenant_id,
                "customer_id": approval.customer_id,
                "action_type": approval.action_type,
                "resource_type": approval.resource_type,
                "resource_id": approval.resource_id,
            }
            action_id = action.id

        async with admin.connect() as connection:
            transaction = await connection.begin()
            try:
                # Planner-only history rows preserve every declarative CHECK and
                # uniqueness boundary.  Replica mode suppresses lifecycle/FK row
                # triggers while this transaction builds and later rolls back a
                # representative historical distribution for EXPLAIN.
                await connection.execute(text("SET LOCAL session_replication_role='replica'"))
                approval_history = await connection.execute(
                    text(
                        """
                        INSERT INTO public.approval_requests
                        SELECT (
                          jsonb_populate_record(
                            NULL::public.approval_requests,
                            to_jsonb(source)||jsonb_build_object(
                              'id','approval_ix_'||CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'proposal_id',NULL,
                              'resource_id','billing_ix_'||CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'status','failed',
                              'status_version',2,
                              'idempotency_key','approval-ix-'||
                                   CAST(:suffix AS text)||'-'||
                                   lpad(series.value::text,4,'0'),
                              'created_at',source.created_at-
                                   series.value*interval '1 second',
                              'updated_at',source.updated_at-
                                   series.value*interval '1 second'
                            )
                          )
                        ).*
                        FROM public.approval_requests source
                        CROSS JOIN generate_series(
                          1,CAST(:history_count AS integer)
                        ) AS series(value)
                        WHERE source.id=:approval_id
                        """
                    ),
                    {
                        "suffix": suffix,
                        "history_count": history_count,
                        "approval_id": fixture.approval_id,
                    },
                )
                assert approval_history.rowcount == history_count
                action_history = await connection.execute(
                    text(
                        """
                        INSERT INTO public.business_actions
                        SELECT (
                          jsonb_populate_record(
                            NULL::public.business_actions,
                            to_jsonb(source)||jsonb_build_object(
                              'id','action_ix_'||CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'approval_id','approval_ix_'||
                                   CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'resource_id','billing_ix_'||
                                   CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'effect_identity','effect_ix_'||
                                   CAST(:suffix AS text)||'_'||
                                   lpad(series.value::text,4,'0'),
                              'status','failed',
                              'idempotency_key','action-ix-'||
                                   CAST(:suffix AS text)||'-'||
                                   lpad(series.value::text,4,'0'),
                              'created_at',source.created_at-
                                   series.value*interval '1 second',
                              'updated_at',source.updated_at-
                                   series.value*interval '1 second'
                            )
                          )
                        ).*
                        FROM public.business_actions source
                        CROSS JOIN generate_series(
                          1,CAST(:history_count AS integer)
                        ) AS series(value)
                        WHERE source.id=:action_id
                        """
                    ),
                    {
                        "suffix": suffix,
                        "history_count": history_count,
                        "action_id": action_id,
                    },
                )
                assert action_history.rowcount == history_count
                await connection.execute(text("SET LOCAL session_replication_role='origin'"))

                target_ticket, _ = await _seed_reconciler_lane(
                    connection,
                    prefix=target_lane,
                    count=target_job_count,
                    effect_lane=False,
                    available_at=datetime.now(UTC) - timedelta(days=2),
                )
                await _seed_reconciler_lane(
                    connection,
                    prefix=background_lane,
                    count=background_job_count,
                    effect_lane=False,
                    available_at=datetime.now(UTC) - timedelta(days=3),
                )
                await connection.execute(
                    text(
                        "ANALYZE public.approval_requests,"
                        "public.business_actions,public.runtime_jobs"
                    )
                )

                index_rows = (
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT indexname,indexdef
                                FROM pg_catalog.pg_indexes
                                WHERE schemaname='public'
                                  AND tablename IN (
                                    'approval_requests',
                                    'business_actions',
                                    'runtime_jobs'
                                  )
                                """
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                indexes = {row["indexname"]: row["indexdef"] for row in index_rows}
                expected_indexes = {
                    "uq_approval_active_resource",
                    "ix_business_action_approval_created",
                    "ix_v1512_runtime_job_ticket_dispatch_lookup",
                    "uq_runtime_jobs_ticket_dispatch_sequence",
                    "uq_runtime_jobs_approval_id",
                }
                assert expected_indexes <= indexes.keys()
                assert "WHERE" in indexes["uq_approval_active_resource"]
                assert "status" in indexes["uq_approval_active_resource"]

                distribution = (
                    await connection.execute(
                        text(
                            """
                            SELECT
                              (
                                SELECT count(*) FROM public.approval_requests
                                WHERE id LIKE :approval_pattern
                              ) AS approval_history,
                              (
                                SELECT count(*) FROM public.business_actions
                                WHERE id LIKE :action_pattern
                              ) AS action_history,
                              (
                                SELECT count(*) FROM public.runtime_jobs
                                WHERE id LIKE :target_job_pattern
                              ) AS target_jobs,
                              (
                                SELECT count(*) FROM public.runtime_jobs
                                WHERE id LIKE :background_job_pattern
                              ) AS background_jobs
                            """
                        ),
                        {
                            "approval_pattern": f"approval_ix_{suffix}_%",
                            "action_pattern": f"action_ix_{suffix}_%",
                            "target_job_pattern": f"job_{target_lane}_%",
                            "background_job_pattern": f"job_{background_lane}_%",
                        },
                    )
                ).one()
                assert tuple(distribution) == (
                    history_count,
                    history_count,
                    target_job_count,
                    background_job_count,
                )

                approval_query = """
                    SELECT id
                    FROM public.approval_requests
                    WHERE tenant_id=:tenant_id
                      AND customer_id=:customer_id
                      AND action_type=:action_type
                      AND resource_type=:resource_type
                      AND resource_id=:resource_id
                      AND status IN ('pending','approved')
                    LIMIT 1
                """
                approval_plan = await connection.scalar(
                    text(
                        "EXPLAIN (ANALYZE,FORMAT JSON,COSTS ON,"
                        f"TIMING OFF,SUMMARY OFF) {approval_query}"
                    ),
                    approval_identity,
                )
                _assert_natural_indexed_limit_plan(
                    approval_plan,
                    expected_indexes="uq_approval_active_resource",
                    upper_bound=1,
                )
                approval_rows = (
                    (
                        await connection.execute(
                            text(approval_query),
                            approval_identity,
                        )
                    )
                    .scalars()
                    .all()
                )
                assert approval_rows == [fixture.approval_id]

                action_query = """
                    SELECT id
                    FROM public.business_actions
                    WHERE tenant_id=:tenant_id AND approval_id=:approval_id
                    ORDER BY created_at DESC,id DESC
                    LIMIT 1
                """
                action_parameters = {
                    "tenant_id": approval_identity["tenant_id"],
                    "approval_id": fixture.approval_id,
                }
                action_plan = await connection.scalar(
                    text(
                        "EXPLAIN (ANALYZE,FORMAT JSON,COSTS ON,"
                        f"TIMING OFF,SUMMARY OFF) {action_query}"
                    ),
                    action_parameters,
                )
                action_nodes = _assert_natural_indexed_limit_plan(
                    action_plan,
                    expected_indexes="ix_business_action_approval_created",
                    upper_bound=1,
                )
                assert any(
                    node.get("Scan Direction") == "Backward"
                    for node in action_nodes
                    if node.get("Index Name") == "ix_business_action_approval_created"
                )
                action_rows = (
                    (
                        await connection.execute(
                            text(action_query),
                            action_parameters,
                        )
                    )
                    .scalars()
                    .all()
                )
                assert action_rows == [action_id]

                ticket_query = """
                    SELECT id
                    FROM public.runtime_jobs
                    WHERE tenant_id=:tenant_id
                      AND ticket_id=:ticket_id
                      AND status IN ('queued','retry_wait','leased')
                      AND available_at<=clock_timestamp()
                    ORDER BY dispatch_sequence,id
                    LIMIT 8
                """
                ticket_parameters = {
                    "tenant_id": approval_identity["tenant_id"],
                    "ticket_id": target_ticket,
                }
                ticket_plan = await connection.scalar(
                    text(
                        "EXPLAIN (ANALYZE,FORMAT JSON,COSTS ON,"
                        f"TIMING OFF,SUMMARY OFF) {ticket_query}"
                    ),
                    ticket_parameters,
                )
                ticket_indexes = frozenset(
                    {
                        "ix_v1512_runtime_job_ticket_dispatch_lookup",
                        "uq_runtime_jobs_ticket_dispatch_sequence",
                    }
                )
                ticket_nodes = _assert_natural_indexed_limit_plan(
                    ticket_plan,
                    expected_indexes=ticket_indexes,
                    upper_bound=8,
                )
                assert all(node.get("Node Type") != "Sort" for node in ticket_nodes)
                ticket_index_nodes = [
                    node for node in ticket_nodes if node.get("Index Name") in ticket_indexes
                ]
                assert len(ticket_index_nodes) == 1
                ticket_index_node = ticket_index_nodes[0]
                assert ticket_index_node.get("Scan Direction") == "Forward"
                assert int(ticket_index_node.get("Actual Loops", 0)) == 1
                # Incremental Sort may read one row beyond LIMIT to close the
                # final pre-sorted dispatch_sequence group.
                assert int(ticket_index_node.get("Actual Rows", 10)) <= 9
                assert (
                    sum(int(node.get("Rows Removed by Filter", 0)) for node in ticket_index_nodes)
                    <= 8
                )
                assert "tenant_id" in str(ticket_index_node.get("Index Cond", ""))
                assert "ticket_id" in str(ticket_index_node.get("Index Cond", ""))
                incremental_sorts = [
                    node for node in ticket_nodes if node.get("Node Type") == "Incremental Sort"
                ]
                if (
                    ticket_index_node.get("Index Name")
                    == "ix_v1512_runtime_job_ticket_dispatch_lookup"
                ):
                    assert incremental_sorts == []
                else:
                    assert len(incremental_sorts) <= 1
                    for incremental_sort in incremental_sorts:
                        assert "dispatch_sequence" in str(incremental_sort.get("Presorted Key", ""))
                        assert int(incremental_sort.get("Actual Loops", 0)) == 1
                        assert int(incremental_sort.get("Actual Rows", 9)) <= 8
                ticket_rows = (
                    (
                        await connection.execute(
                            text(ticket_query),
                            ticket_parameters,
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(ticket_rows) == 8
                assert all(job_id.startswith(f"job_{target_lane}_") for job_id in ticket_rows)

                resume_query = """
                    SELECT id
                    FROM public.runtime_jobs
                    WHERE approval_id=:approval_id
                      AND kind='approval_resume'
                    ORDER BY created_at DESC,id DESC
                    LIMIT 1
                """
                resume_parameters = {"approval_id": fixture.approval_id}
                resume_plan = await connection.scalar(
                    text(
                        "EXPLAIN (ANALYZE,FORMAT JSON,COSTS ON,"
                        f"TIMING OFF,SUMMARY OFF) {resume_query}"
                    ),
                    resume_parameters,
                )
                _assert_natural_indexed_limit_plan(
                    resume_plan,
                    expected_indexes="uq_runtime_jobs_approval_id",
                    upper_bound=1,
                )
                resume_rows = (
                    (
                        await connection.execute(
                            text(resume_query),
                            resume_parameters,
                        )
                    )
                    .scalars()
                    .all()
                )
                assert resume_rows == [fixture.job_id]
            finally:
                await transaction.rollback()

        async with admin.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.runtime_jobs
                    SET available_at=CAST('2200-01-01T00:00:00Z' AS timestamptz),
                        status_version=status_version+1,
                        updated_at=clock_timestamp()
                    WHERE id=:job_id
                      AND status='succeeded' AND outcome='verification_pending'
                    """
                ),
                {"job_id": fixture.job_id},
            )
    finally:
        await admin.dispose()
