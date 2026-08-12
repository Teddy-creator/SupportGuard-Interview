import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.approvals.service import ApprovalService, RefundRuntime
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import RefundProposalInput, ToolCallContext
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    MutationKillSwitch,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.business import BusinessService, action_hash
from supportguard.services.errors import DomainError, ErrorCode

TEST_CAPABILITY = issue_test_runtime_capability(testing=True)


def context() -> ToolCallContext:
    return ToolCallContext.fixture(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        ticket_id="ticket_demo",
        run_id="run_demo",
        checkpoint_id="checkpoint_demo",
        tool_call_id="tool_refund_test",
        trace_id="trace_refund_test",
    )


async def proposed_approval(session: AsyncSession) -> ApprovalRequest:
    result = await BusinessService(session, test_capability=TEST_CAPABILITY).propose_refund(
        context(),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Explicit duplicate charge confirmed by billing relation.",
            idempotency_key="refund-runtime-key-001",
        ),
    )
    approval = await session.get(ApprovalRequest, result.approval_id)
    assert approval is not None
    return approval


@pytest.mark.asyncio
async def test_unapproved_refund_cannot_execute(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    with pytest.raises(DomainError) as error:
        await RefundRuntime(db_session).execute_refund(
            approval.id,
            idempotency_key=approval.idempotency_key,
            trace_id="trace_execute",
        )
    assert error.value.code is ErrorCode.APPROVAL_STATE_CONFLICT
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 0


@pytest.mark.asyncio
async def test_legacy_refund_preserves_disabled_automation_error_contract(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        approval.id,
        decision="approve",
        approver_id="approver_demo",
        reason="Approved before automation was disabled.",
        trace_id="trace_approve_disabled",
    )
    kill_switch = await db_session.get(
        MutationKillSwitch,
        {"tenant_id": "tenant_demo", "action_type": "refund"},
    )
    assert kill_switch is not None
    kill_switch.enabled = False
    await db_session.flush()

    with pytest.raises(DomainError) as error:
        await RefundRuntime(db_session).execute_refund(
            approval.id,
            idempotency_key=approval.idempotency_key,
            trace_id="trace_execute_disabled",
        )

    assert error.value.code is ErrorCode.APPROVAL_STATE_CONFLICT
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 0


@pytest.mark.asyncio
async def test_approved_refund_is_atomic_and_repeated_resume_is_idempotent(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        approval.id,
        decision="approve",
        approver_id="approver_demo",
        reason="Evidence and snapshot verified.",
        trace_id="trace_approve",
    )
    runtime = RefundRuntime(db_session)
    first = await runtime.execute_refund(
        approval.id,
        idempotency_key=approval.idempotency_key,
        trace_id="trace_execute",
    )
    second = await runtime.execute_refund(
        approval.id,
        idempotency_key=approval.idempotency_key,
        trace_id="trace_resume",
    )
    billing = await db_session.get(BillingRecord, "bill_duplicate")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert first.business_action_id == second.business_action_id
    assert first.reused is False and second.reused is True
    assert billing is not None and billing.status == "refunded"
    assert ticket is not None and ticket.status == "resolved"
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 1
    action = await db_session.get(BusinessAction, first.business_action_id)
    assert action is not None
    assert action.approval_id == approval.id
    assert action.human_decision_id is not None and action.decision_hash is not None
    assert action.effect_identity is not None
    assert action.canonical_event_id is not None and action.canonical_event_hash is not None
    record_predicate_operands(
        requirement_id="C4-P0-06a",
        predicate_id="c4_p0_06a",
        subject_kind="refund_effect_idempotency",
        operands={
            "first_action_id": first.business_action_id,
            "second_action_id": second.business_action_id,
            "first_reused": first.reused,
            "second_reused": second.reused,
            "billing_status": billing.status if billing is not None else None,
            "ticket_status": ticket.status if ticket is not None else None,
            "business_action_count": int(
                await db_session.scalar(select(func.count()).select_from(BusinessAction)) or 0
            ),
            "approval_id": action.approval_id,
            "expected_approval_id": approval.id,
            "decision_hash": action.decision_hash,
            "effect_identity": action.effect_identity,
        },
    )


@pytest.mark.asyncio
async def test_existing_effect_cannot_be_reused_by_a_different_approval_binding(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    first = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        first.id,
        decision="approve",
        approver_id="approver_demo",
        reason="First approval",
        trace_id="trace_first",
    )
    await RefundRuntime(db_session).execute_refund(
        first.id,
        idempotency_key=first.idempotency_key,
        trace_id="trace_first_effect",
    )
    payload = {**first.action_payload, "refund_reason": "Different approval snapshot"}
    sibling = ApprovalRequest(
        tenant_id=first.tenant_id,
        ticket_id=first.ticket_id,
        customer_id=first.customer_id,
        run_id=first.run_id,
        checkpoint_id=first.checkpoint_id,
        action_type="refund",
        resource_type=first.resource_type,
        resource_id=first.resource_id,
        origin_turn_id=first.origin_turn_id,
        action_payload=payload,
        review_context={},
        action_hash=action_hash(payload),
        business_version=first.business_version,
        status="approved",
        idempotency_key="refund-runtime-key-sibling",
        approver_id="approver_demo",
    )
    db_session.add(sibling)
    await db_session.flush()
    db_session.add(
        HumanDecision(
            tenant_id=sibling.tenant_id,
            approval_id=sibling.id,
            actor_id="approver_demo",
            decision="approve",
            reason="Sibling approval",
            action_hash=sibling.action_hash,
            audit_metadata={},
        )
    )
    await db_session.flush()
    with pytest.raises(DomainError) as error:
        await RefundRuntime(db_session).execute_refund(
            sibling.id,
            idempotency_key=sibling.idempotency_key,
            trace_id="trace_sibling_effect",
        )
    assert error.value.code is ErrorCode.APPROVAL_SNAPSHOT_MISMATCH
    action_count = await db_session.scalar(select(func.count()).select_from(BusinessAction))
    assert action_count == 1
    record_predicate_operands(
        requirement_id="C4-P0-05b",
        predicate_id="c4_p0_05b",
        subject_kind="business_action_binding_isolation",
        operands={
            "conflict_code": error.value.code.value,
            "business_action_count": int(action_count or 0),
            "first_approval_id": first.id,
            "sibling_approval_id": sibling.id,
            "binding_ids_differ": first.id != sibling.id,
        },
    )


@pytest.mark.asyncio
async def test_execution_revalidates_changed_billing_version(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        approval.id,
        decision="approve",
        approver_id="approver_demo",
        reason="Approved against version two.",
        trace_id="trace_approve",
    )
    billing = await db_session.get(BillingRecord, "bill_duplicate")
    assert billing is not None
    billing.version += 1
    await db_session.flush()
    with pytest.raises(DomainError) as error:
        await RefundRuntime(db_session).execute_refund(
            approval.id,
            idempotency_key=approval.idempotency_key,
            trace_id="trace_execute",
        )
    assert error.value.code is ErrorCode.APPROVAL_SNAPSHOT_MISMATCH


@pytest.mark.asyncio
async def test_rejected_approval_never_executes(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        approval.id,
        decision="reject",
        approver_id="approver_demo",
        reason="Customer evidence is insufficient.",
        trace_id="trace_reject",
    )
    with pytest.raises(DomainError) as error:
        await RefundRuntime(db_session).execute_refund(
            approval.id,
            idempotency_key=approval.idempotency_key,
            trace_id="trace_execute",
        )
    assert error.value.code is ErrorCode.APPROVAL_STATE_CONFLICT


@pytest.mark.asyncio
async def test_crash_before_transaction_commit_can_safely_retry(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    await ApprovalService(db_session).decide(
        approval.id,
        decision="approve",
        approver_id="approver_demo",
        reason="Approved before injected crash.",
        trace_id="trace_approve",
    )
    await db_session.commit()
    approval_id = approval.id
    idempotency_key = approval.idempotency_key

    attempted = await RefundRuntime(db_session).execute_refund(
        approval_id,
        idempotency_key=idempotency_key,
        trace_id="trace_before_crash",
    )
    await db_session.rollback()  # simulates process death before transaction commit

    retried = await RefundRuntime(db_session).execute_refund(
        approval_id,
        idempotency_key=idempotency_key,
        trace_id="trace_after_restart",
    )
    await db_session.commit()
    assert attempted.business_action_id != retried.business_action_id
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 1


@pytest.mark.asyncio
async def test_orphan_approval_becomes_stale_instead_of_resuming_or_500(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    approval.run_id = None
    approval.checkpoint_id = None
    await db_session.flush()

    with pytest.raises(DomainError) as error:
        await ApprovalService(db_session).decide(
            approval.id,
            decision="approve",
            approver_id="approver_demo",
            reason="Must not resume an orphan proposal.",
            trace_id="trace_orphan",
        )

    assert error.value.code is ErrorCode.APPROVAL_BINDING_INVALID
    assert approval.status == "stale"


@pytest.mark.asyncio
async def test_new_manual_takeover_decision_is_rejected_without_effect(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    with pytest.raises(DomainError) as error:
        await ApprovalService(db_session).decide(
            approval.id,
            decision="manual_takeover",
            approver_id="approver_demo",
            reason="Requires human investigation.",
            trace_id="trace_takeover",
        )
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")

    assert error.value.code is ErrorCode.APPROVAL_STATE_CONFLICT
    assert approval.status == "pending"
    assert run is not None and run.status == "interrupted"
    assert ticket is not None and ticket.status == "awaiting_approval"
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 0


@pytest.mark.asyncio
async def test_edit_and_approve_changes_only_refund_reason_and_action_hash(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = await proposed_approval(db_session)
    previous_hash = approval.action_hash
    previous_billing = approval.action_payload["billing_record_id"]
    await ApprovalService(db_session).edit_and_approve(
        approval.id,
        approver_id="approver_demo",
        refund_reason="Duplicate charge verified against the billing relation.",
        approver_note="Customer evidence reviewed.",
        trace_id="trace_edit",
    )

    assert approval.action_hash != previous_hash
    assert approval.action_payload["billing_record_id"] == previous_billing
    assert approval.action_payload["refund_reason"].startswith("Duplicate charge")
    assert approval.approver_note == "Customer evidence reviewed."


@pytest.mark.asyncio
async def test_duplicate_active_refund_reuses_canonical_approval(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    first = await proposed_approval(db_session)
    sibling_ticket = SupportTicket(
        id="ticket_sibling",
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="open",
        issue_type="billing",
    )
    sibling_message = TicketMessage(
        id="message_sibling",
        tenant_id="tenant_demo",
        ticket_id="ticket_sibling",
        role="user",
        message_kind="customer",
        conversation_sequence=1,
        content="duplicate charge",
    )
    sibling_ticket.next_message_sequence = 1
    db_session.add_all([sibling_ticket, sibling_message])
    await db_session.flush()
    sibling_run = AgentRun(
        id="run_sibling",
        tenant_id="tenant_demo",
        ticket_id=sibling_ticket.id,
        customer_id="cust_demo",
        message_id=sibling_message.id,
        status="interrupted",
        checkpoint_stage="awaiting_approval",
        checkpoint_id="checkpoint_sibling",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native",
        prompt_version="v1.1",
        schema_version="agent.v1",
        context_version="context.v1",
    )
    db_session.add(sibling_run)
    await db_session.flush()
    sibling_turn = ConversationTurn(
        id="turn_sibling",
        tenant_id="tenant_demo",
        ticket_id=sibling_ticket.id,
        customer_message_id=sibling_message.id,
        run_id=sibling_run.id,
        ordinal=1,
        activity_state="waiting_external",
        automation_mode="agent",
        model=sibling_run.model,
        provider_mode=sibling_run.provider_mode,
        tool_call_mode=sibling_run.tool_call_mode,
        context_version=sibling_run.context_version,
    )
    db_session.add(sibling_turn)
    sibling_run.turn_id = sibling_turn.id
    sibling_message.turn_id = sibling_turn.id
    await db_session.flush()
    sibling_result = await BusinessService(
        db_session, test_capability=TEST_CAPABILITY
    ).propose_refund(
        ToolCallContext.fixture(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id=sibling_ticket.id,
            run_id=sibling_run.id,
            checkpoint_id=sibling_run.checkpoint_id,
            tool_call_id="tool_sibling",
            trace_id="trace_sibling",
        ),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Duplicate charge requires separate review.",
            idempotency_key="refund-runtime-key-sibling",
        ),
    )
    assert sibling_result.approval_id == first.id
    await ApprovalService(db_session).decide(
        first.id,
        decision="approve",
        approver_id="approver_demo",
        reason="Approve canonical proposal.",
        trace_id="trace_first",
    )
    await RefundRuntime(db_session).execute_refund(
        first.id,
        idempotency_key=first.idempotency_key,
        trace_id="trace_execute",
    )

    assert first.status == "executed"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == first.tenant_id,
                ApprovalRequest.resource_id == first.resource_id,
            )
        )
        == 1
    )
