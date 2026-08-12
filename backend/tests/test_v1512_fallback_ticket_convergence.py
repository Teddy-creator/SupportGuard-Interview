import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.approvals.service import ApprovalService, RefundRuntime
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import RefundProposalInput, ToolCallContext
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    ConversationTurn,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.business import BusinessService, action_hash
from supportguard.services.runtime_jobs import RuntimeJobRepository
from supportguard.services.segments import SegmentRepository

TEST_CAPABILITY = issue_test_runtime_capability(testing=True)


async def _propose_refund(session: AsyncSession) -> ApprovalRequest:
    result = await BusinessService(
        session,
        test_capability=TEST_CAPABILITY,
    ).propose_refund(
        ToolCallContext.fixture(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            run_id="run_demo",
            checkpoint_id="checkpoint_demo",
            tool_call_id="tool_fallback_refund",
            trace_id="trace_fallback_refund",
        ),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Duplicate charge confirmed by the billing relation.",
            idempotency_key="refund-fallback-convergence",
        ),
    )
    approval = await session.get(ApprovalRequest, result.approval_id)
    assert approval is not None
    return approval


async def _seed_sibling_active_approval(session: AsyncSession) -> ApprovalRequest:
    ticket = await session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.next_message_sequence += 1
    message = TicketMessage(
        id="message_sibling_action",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        conversation_sequence=ticket.next_message_sequence,
        content="Please revoke the exposed API key.",
    )
    session.add(message)
    await session.flush()
    run = AgentRun(
        id="run_sibling_action",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=message.id,
        status="interrupted",
        checkpoint_stage="awaiting_approval",
        checkpoint_id="checkpoint_sibling_action",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        prompt_version="agent_decide.v3",
        schema_version="agent.v1",
        context_version="context.v1.2",
    )
    session.add(run)
    await session.flush()
    turn = ConversationTurn(
        id="turn_sibling_action",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_message_id=message.id,
        run_id=run.id,
        ordinal=ticket.next_message_sequence,
        activity_state="waiting_external",
        result_state="proposal_created",
        automation_mode="agent",
        model=run.model,
        provider_mode=run.provider_mode,
        tool_call_mode=run.tool_call_mode,
        context_version=run.context_version,
    )
    session.add(turn)
    run.turn_id = turn.id
    message.turn_id = turn.id
    await session.flush()
    payload = {
        "api_key_id": "key_demo_leaked",
        "customer_id": ticket.customer_id,
        "fingerprint": "fp_demo_leaked",
        "business_version": 2,
    }
    proposal = ProposalRecord(
        id="proposal_sibling_action",
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        proposal_identity="api-key-revocation:key_demo_leaked:sibling",
        action_type="api_key_revocation",
        resource_id="key_demo_leaked",
        resource_version=2,
        action_payload=payload,
        observation_binding=[],
        action_hash=action_hash(payload),
        status="bound",
    )
    session.add(proposal)
    await session.flush()
    approval = ApprovalRequest(
        id="approval_sibling_action",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        proposal_id=proposal.id,
        run_id=run.id,
        checkpoint_id=run.checkpoint_id,
        action_type="api_key_revocation",
        resource_type="api_key_id",
        resource_id="key_demo_leaked",
        origin_turn_id=turn.id,
        action_payload=payload,
        review_context={},
        action_hash=action_hash(payload),
        business_version=2,
        status="pending",
        idempotency_key="approval-sibling-action",
    )
    session.add(approval)
    await session.flush()
    return approval


async def _seed_accepted_followup(session: AsyncSession) -> ConversationTurn:
    ticket = await session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.next_message_sequence += 1
    message = TicketMessage(
        id="message_fallback_followup",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        conversation_sequence=ticket.next_message_sequence,
        content="Can I continue asking a product question?",
    )
    turn = ConversationTurn(
        id="turn_fallback_followup",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_message_id=message.id,
        ordinal=ticket.next_message_sequence,
        activity_state="accepted",
        automation_mode="agent",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1.2",
    )
    session.add_all([message, turn])
    await session.flush()
    message.turn_id = turn.id
    return turn


@pytest.mark.asyncio
async def test_fallback_reject_preserves_sibling_and_dispatches_followup(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    target = await _propose_refund(db_session)
    sibling = await _seed_sibling_active_approval(db_session)
    followup = await _seed_accepted_followup(db_session)

    await ApprovalService(db_session).decide(
        target.id,
        decision="reject",
        approver_id="approver_demo",
        reason="The refund request was rejected.",
        trace_id="trace_fallback_reject",
    )

    ticket = await db_session.get(SupportTicket, target.ticket_id)
    target_run = await db_session.get(AgentRun, target.run_id)
    target_turn = await db_session.get(ConversationTurn, target.origin_turn_id)
    await db_session.refresh(followup)
    assert target.status == "rejected"
    assert target_run is not None and target_run.status == "completed"
    assert target_turn is not None and target_turn.result_state == "refused"
    assert sibling.status == "pending"
    assert followup.activity_state == "queued"
    assert followup.run_id is not None
    assert ticket is not None and ticket.status == "queued"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RuntimeJob)
            .where(
                RuntimeJob.ticket_id == ticket.id,
                RuntimeJob.run_id == followup.run_id,
                RuntimeJob.status == "queued",
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_fallback_refund_success_and_replay_preserve_sibling_projection(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    target = await _propose_refund(db_session)
    sibling = await _seed_sibling_active_approval(db_session)
    await ApprovalService(db_session).decide(
        target.id,
        decision="approve",
        approver_id="approver_demo",
        reason="The duplicate charge was verified.",
        trace_id="trace_fallback_approve",
    )

    runtime = RefundRuntime(db_session)
    first = await runtime.execute_refund(
        target.id,
        idempotency_key=target.idempotency_key,
        trace_id="trace_fallback_effect",
    )
    replay = await runtime.execute_refund(
        target.id,
        idempotency_key=target.idempotency_key,
        trace_id="trace_fallback_effect_replay",
    )

    ticket = await db_session.get(SupportTicket, target.ticket_id)
    target_turn = await db_session.get(ConversationTurn, target.origin_turn_id)
    assert first.business_action_id == replay.business_action_id
    assert replay.reused is True
    assert target.status == "executed"
    assert sibling.status == "pending"
    assert target_turn is not None and target_turn.result_state == "answered"
    assert ticket is not None and ticket.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_fallback_segment_terminal_does_not_hide_sibling_approval(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    sibling = await _seed_sibling_active_approval(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        run_id=run.id,
        kind="agent_start",
    )
    lease = await jobs.claim(job_id=job.id, owner="worker-fallback-finalizer")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": run.message_id},
    )
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-fallback-final",
        checkpoint_hash="f" * 64,
        outcome="completed",
        state={
            "ticket_id": "ticket_demo",
            "customer_id": "cust_demo",
            "run_id": run.id,
            "trace_id": "trace-fallback-finalizer",
            "classification": {"issue_type": "product_knowledge", "risk": "low"},
            "agent_finish_reason": "answered",
            "segment_events": [],
            "tool_observations": [],
            "evidence": [],
            "final": {
                "answer": "The product question was answered.",
                "terminal_state": "resolved",
                "policy_route": "answer",
            },
        },
    )

    await segments.finalize(lease, marker_id=marker.id)

    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert run.status == "completed"
    assert sibling.status == "pending"
    assert ticket is not None and ticket.status == "awaiting_approval"
    assert ticket.final_response is None
