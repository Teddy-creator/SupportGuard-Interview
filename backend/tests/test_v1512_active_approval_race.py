from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    BillingRecord,
    BusinessAction,
    ConversationTurn,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.business import action_hash
from supportguard.services.runtime_jobs import JobLease, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@dataclass(frozen=True)
class InterruptFixture:
    lease: JobLease
    marker_id: str
    proposal_id: str
    run_id: str
    turn_id: str
    ticket_id: str


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _worker_url(database_url: str) -> str:
    return (
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )


async def _prepare_interrupt(
    session: AsyncSession,
    *,
    prefix: str,
    ordinal: int,
    resource_id: str,
) -> InterruptFixture:
    ticket_id = f"ticket_{prefix}_{ordinal}"
    message_id = f"message_{prefix}_{ordinal}"
    run_id = f"run_{prefix}_{ordinal}"
    proposal_id = f"proposal_{prefix}_{ordinal}"
    ticket = SupportTicket(
        id=ticket_id,
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="queued",
        next_message_sequence=1,
    )
    message = TicketMessage(
        id=message_id,
        tenant_id="tenant_demo",
        ticket_id=ticket_id,
        role="user",
        message_kind="customer",
        conversation_sequence=1,
        content="Please inspect the same duplicate billing record.",
    )
    session.add_all([ticket, message])
    await session.flush()
    turn = await session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.customer_message_id == message_id
        )
    )
    if turn is None:
        turn = ConversationTurn(
            id=f"turn_{prefix}_{ordinal}",
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_message_id=message_id,
            run_id=None,
            ordinal=1,
            activity_state="accepted",
            automation_mode="agent",
        )
        session.add(turn)
        await session.flush()
    run = AgentRun(
        id=run_id,
        tenant_id="tenant_demo",
        ticket_id=ticket_id,
        customer_id="cust_demo",
        message_id=message_id,
        status="queued",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        prompt_version="v1.5.12-race",
        schema_version="agent.v1",
        context_version="context.v1.5",
    )
    session.add(run)
    await session.flush()
    run.turn_id = turn.id
    turn.run_id = run_id
    turn.activity_state = "queued"
    turn.model = run.model
    turn.provider_mode = run.provider_mode
    turn.tool_call_mode = run.tool_call_mode
    turn.context_version = run.context_version
    message.turn_id = turn.id
    await session.flush()

    jobs = RuntimeJobRepository(session)
    job = await jobs.create(
        tenant_id="tenant_demo",
        ticket_id=ticket_id,
        run_id=run_id,
        kind="agent_start",
    )
    lease = await jobs.claim(job_id=job.id, owner=f"worker-{prefix}-{ordinal}")
    payload: dict[str, str | int] = {
        "billing_record_id": resource_id,
        "customer_id": "cust_demo",
        "amount": "49.00",
        "currency": "USD",
        "refund_reason": "The persisted billing relationship identifies a duplicate charge.",
        "business_version": 2,
    }
    proposal = ProposalRecord(
        id=proposal_id,
        tenant_id="tenant_demo",
        run_id=run_id,
        proposal_identity=f"identity:{prefix}:{ordinal}",
        action_type="refund",
        resource_id=resource_id,
        resource_version=2,
        action_payload=payload,
        observation_binding=[],
        action_hash=action_hash(payload),
        status="draft",
    )
    session.add(proposal)
    segments = SegmentRepository(session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"proposal_id": proposal_id},
    )
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id=f"checkpoint_{prefix}_{ordinal}",
        checkpoint_hash=(str(ordinal) * 64),
        outcome="interrupted",
        state={"segment_events": []},
        proposal_id=proposal_id,
    )
    return InterruptFixture(
        lease=lease,
        marker_id=marker.id,
        proposal_id=proposal_id,
        run_id=run_id,
        turn_id=turn.id,
        ticket_id=ticket_id,
    )


async def test_two_transactions_atomically_reuse_one_active_approval() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"active_approval_race_{uuid4().hex[:10]}"
    resource_id = f"bill_{prefix}"
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    worker = create_async_engine(_worker_url(database_url), pool_size=2, max_overflow=0)
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        async with admin_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add(
                BillingRecord(
                    id=resource_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    version=2,
                )
            )
            fixtures = [
                await _prepare_interrupt(
                    session,
                    prefix=prefix,
                    ordinal=ordinal,
                    resource_id=resource_id,
                )
                for ordinal in (1, 2)
            ]

        async def finalize(fixture: InterruptFixture) -> str:
            async with worker_factory() as session, session.begin():
                await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
                approval = await SegmentRepository(session).finalize_interrupt(
                    fixture.lease,
                    marker_id=fixture.marker_id,
                    proposal_id=fixture.proposal_id,
                    test_capability=issue_test_runtime_capability(testing=True),
                )
                return approval.id

        returned_ids = await asyncio.wait_for(
            asyncio.gather(*(finalize(fixture) for fixture in fixtures)),
            timeout=20,
        )
        assert returned_ids[0] == returned_ids[1]

        async with admin_factory() as session:
            active_approvals = list(
                await session.scalars(
                    select(ApprovalRequest).where(
                        ApprovalRequest.tenant_id == "tenant_demo",
                        ApprovalRequest.customer_id == "cust_demo",
                        ApprovalRequest.action_type == "refund",
                        ApprovalRequest.resource_id == resource_id,
                        ApprovalRequest.status.in_(("pending", "approved")),
                    )
                )
            )
            assert len(active_approvals) == 1
            assert active_approvals[0].id == returned_ids[0]
            proposals = list(
                await session.scalars(
                    select(ProposalRecord).where(
                        ProposalRecord.id.in_([fixture.proposal_id for fixture in fixtures])
                    )
                )
            )
            assert sorted(proposal.status for proposal in proposals) == ["bound", "stale"]
            runs = list(
                await session.scalars(
                    select(AgentRun).where(
                        AgentRun.id.in_([fixture.run_id for fixture in fixtures])
                    )
                )
            )
            assert sorted(run.status for run in runs) == ["completed", "interrupted"]
            turns = list(
                await session.scalars(
                    select(ConversationTurn).where(
                        ConversationTurn.id.in_([fixture.turn_id for fixture in fixtures])
                    )
                )
            )
            assert sorted(turn.activity_state for turn in turns) == [
                "completed",
                "waiting_external",
            ]
            tickets = list(
                await session.scalars(
                    select(SupportTicket).where(
                        SupportTicket.id.in_([fixture.ticket_id for fixture in fixtures])
                    )
                )
            )
            assert sorted(ticket.status for ticket in tickets) == [
                "awaiting_approval",
                "resolved",
            ]
            reused_messages = list(
                await session.scalars(
                    select(TicketMessage).where(
                        TicketMessage.ticket_id.in_(
                            [fixture.ticket_id for fixture in fixtures]
                        ),
                        TicketMessage.approval_id == returned_ids[0],
                    )
                )
            )
            # The losing conversation remains linked to the canonical Approval
            # through a durable read-model alias without pretending that it
            # owns an approval workflow.
            assert {item.ticket_id for item in reused_messages} == {
                fixture.ticket_id for fixture in fixtures
            }
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ApprovalSnapshot)
                    .where(ApprovalSnapshot.approval_id == returned_ids[0])
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ApprovalActionRevision)
                    .where(ApprovalActionRevision.approval_id == returned_ids[0])
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BusinessAction)
                    .where(BusinessAction.resource_id == resource_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentEvent)
                    .where(
                        AgentEvent.event_type == "approval_interrupted",
                        AgentEvent.run_id.in_([fixture.run_id for fixture in fixtures]),
                        AgentEvent.payload["approval_id"].as_string() != returned_ids[0],
                    )
                )
                == 0
            )
            jobs = list(
                await session.scalars(
                    select(RuntimeJob).where(
                        RuntimeJob.id.in_([fixture.lease.job_id for fixture in fixtures])
                    )
                )
            )
            assert [job.status for job in jobs].count("succeeded") == 2
    finally:
        await worker.dispose()
        await admin.dispose()
