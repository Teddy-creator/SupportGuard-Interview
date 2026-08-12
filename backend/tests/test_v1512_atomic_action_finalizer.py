from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.finalizer import ActionIntentApprovalResumeDelta
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    FinalizerPayload,
    HumanDecision,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.approval_lifecycle import ActionLifecycleService
from supportguard.services.runtime_jobs import JobLease, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_approval_commands import bound_approval


def _intent_state(
    *,
    run_id: str,
    approval: ApprovalRequest,
) -> dict[str, object]:
    return {
        "ticket_id": approval.ticket_id,
        "customer_id": approval.customer_id,
        "run_id": run_id,
        "trace_id": f"trace:{approval.id}:atomic-finalizer",
        "classification": {"issue_type": "billing_refund", "risk": "high"},
        "agent_finish_reason": "proposed",
        "human_decision": {
            "approval_id": approval.id,
            "action": "approve",
        },
        "execution_result": {
            "approval_id": approval.id,
            "action_type": approval.action_type,
            "resource_id": approval.resource_id,
            "action_hash": approval.action_hash,
            "idempotency_key": approval.idempotency_key,
            "status": "execution_pending",
            "execution_intent": "execute_runtime_action",
            "expected_approval_status": "approved",
        },
        "tool_observations": [],
        "evidence": [],
        "segment_events": [],
        "final": {
            "answer": "The effect is not committed yet.",
            "terminal_state": "verification_pending",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "material_claims": [],
            "policy_route": "await_approval",
        },
    }


def test_actionful_finalizer_contract_cannot_claim_a_preexisting_effect() -> None:
    with pytest.raises(ValidationError):
        ActionIntentApprovalResumeDelta.model_validate(
            {
                "approval_id": "approval_1",
                "human_decision_id": "decision_1",
                "decision": "approve",
                "action_hash": "a" * 64,
                "execution_intent": "execute_runtime_action",
                "expected_approval_status": "approved",
                "business_action_id": "action_forged_before_execution",
                "effect_hash": "b" * 64,
            }
        )


async def _prepared_action_resume(
    db_session: AsyncSession,
) -> tuple[
    ApprovalRequest,
    AgentRun,
    RuntimeJob,
    JobLease,
    CheckpointCommitMarker,
]:
    approval = await bound_approval(db_session)
    run = await db_session.get(AgentRun, approval.run_id, with_for_update=True)
    assert run is not None and approval.selected_revision_id
    await ActionLifecycleService(db_session).transition(
        approval,
        to_status="approved",
        expected_status="pending",
        expected_version=approval.status_version,
        decided_at=datetime.now(UTC),
    )
    decision = HumanDecision(
        tenant_id=approval.tenant_id,
        approval_id=approval.id,
        action_revision_id=approval.selected_revision_id,
        actor_id="user_approver_demo",
        decision="approve",
        reason="Atomic finalizer regression fixture.",
        action_hash=approval.action_hash,
        decision_hash="d" * 64,
    )
    db_session.add(decision)
    await db_session.flush()
    event = await AgentRunStore(db_session).append_event(
        run,
        event_type="human_decision_accepted",
        payload={
            "approval_id": approval.id,
            "human_decision_id": decision.id,
            "decision": decision.decision,
            "decision_hash": decision.decision_hash,
            "action_hash": decision.action_hash,
        },
        visibility="approver",
        expected_ticket_head_event_id=approval.expected_ticket_head_event_id,
        expected_ticket_sequence=approval.expected_ticket_sequence,
        expected_ticket_event_hash=approval.expected_ticket_event_hash,
    )
    decision.canonical_event_id = event.id
    decision.canonical_event_hash = event.event_hash
    run.status = "queued"
    job = await RuntimeJobRepository(db_session).create(
        tenant_id=approval.tenant_id,
        run_id=run.id,
        kind="approval_resume",
        approval_id=approval.id,
    )
    lease = await RuntimeJobRepository(db_session).claim(
        job_id=job.id,
        owner="worker-atomic-finalizer",
        now=datetime.now(UTC),
    )
    marker = await SegmentRepository(db_session).prepare(
        lease,
        delivery_generation=1,
        segment_kind="approval_resume",
        segment_input={"approval_id": approval.id, "decision_id": decision.id},
    )
    await SegmentRepository(db_session).checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-atomic-finalizer",
        checkpoint_hash="c" * 64,
        outcome="completed",
        state=_intent_state(run_id=run.id, approval=approval),
        approval_id=approval.id,
    )
    return approval, run, job, lease, marker


@pytest.mark.asyncio
async def test_action_effect_and_terminal_aggregate_commit_in_segment_finalizer(
    db_session: AsyncSession,
) -> None:
    approval, run, job, lease, marker = await _prepared_action_resume(db_session)

    assert (
        await db_session.scalar(
            select(func.count(BusinessAction.id)).where(
                BusinessAction.approval_id == approval.id
            )
        )
        == 0
    )
    persisted = await db_session.scalar(
        select(FinalizerPayload).where(FinalizerPayload.marker_id == marker.id)
    )
    assert persisted is not None
    assert persisted.domain_delta["execution_intent"] == "execute_runtime_action"
    assert "business_action_id" not in persisted.domain_delta

    approval_id = approval.id
    marker_id = marker.id
    savepoint = await db_session.begin_nested()

    def fail_after_effect_sql(
        session: object,
        _flush_context: object,
    ) -> None:
        new_rows = getattr(session, "new", ())
        if any(isinstance(row, BusinessAction) for row in new_rows):
            raise RuntimeError("fault_after_business_effect_sql")

    sqlalchemy_event.listen(
        db_session.sync_session,
        "after_flush",
        fail_after_effect_sql,
    )
    try:
        with pytest.raises(RuntimeError, match="fault_after_business_effect_sql"):
            await SegmentRepository(db_session).finalize(lease, marker_id=marker.id)
        assert not savepoint.is_active
    finally:
        sqlalchemy_event.remove(
            db_session.sync_session,
            "after_flush",
            fail_after_effect_sql,
        )
    db_session.expire_all()
    stored_approval = await db_session.get(ApprovalRequest, approval_id)
    stored_marker = await db_session.get(type(marker), marker_id)
    billing = await db_session.get(BillingRecord, "bill_duplicate")
    assert stored_approval is not None and stored_approval.status == "approved"
    assert stored_marker is not None and stored_marker.status == "checkpoint_written"
    assert billing is not None and billing.status == "charged"
    assert (
        await db_session.scalar(
            select(func.count(BusinessAction.id)).where(
                BusinessAction.approval_id == stored_approval.id
            )
        )
        == 0
    )

    await SegmentRepository(db_session).finalize(lease, marker_id=stored_marker.id)

    await db_session.refresh(stored_approval)
    await db_session.refresh(run)
    stored_job = await db_session.get(RuntimeJob, job.id)
    ticket = await db_session.get(SupportTicket, stored_approval.ticket_id)
    billing = await db_session.get(BillingRecord, stored_approval.resource_id)
    action = await db_session.scalar(
        select(BusinessAction).where(
            BusinessAction.approval_id == stored_approval.id
        )
    )
    update = await db_session.scalar(
        select(TicketMessage).where(
            TicketMessage.approval_id == stored_approval.id,
            TicketMessage.message_kind == "action_update",
        )
    )
    assert stored_approval.status == "executed"
    assert action is not None and action.status == "succeeded"
    assert billing is not None and billing.status == "refunded"
    assert run.status == "completed" and run.agent_finish_reason == "executed"
    assert stored_job is not None and stored_job.status == "succeeded"
    assert ticket is not None and ticket.status == "resolved"
    assert update is not None and "已经安全执行完成" in update.content


@pytest.mark.asyncio
async def test_stale_effect_and_terminal_aggregate_converge_in_segment_finalizer(
    db_session: AsyncSession,
) -> None:
    approval, run, job, lease, marker = await _prepared_action_resume(db_session)
    billing = await db_session.get(
        BillingRecord,
        approval.resource_id,
        with_for_update=True,
    )
    assert billing is not None
    billing.status = "voided"
    billing.version += 1
    await db_session.flush()

    await SegmentRepository(db_session).finalize(lease, marker_id=marker.id)

    await db_session.refresh(approval)
    await db_session.refresh(run)
    stored_job = await db_session.get(RuntimeJob, job.id)
    ticket = await db_session.get(SupportTicket, approval.ticket_id)
    action_count = await db_session.scalar(
        select(func.count(BusinessAction.id)).where(
            BusinessAction.approval_id == approval.id
        )
    )
    update = await db_session.scalar(
        select(TicketMessage).where(
            TicketMessage.approval_id == approval.id,
            TicketMessage.message_kind == "action_update",
        )
    )
    assert approval.status == "stale"
    assert action_count == 0
    assert billing.status == "voided"
    assert run.status == "completed" and run.agent_finish_reason == "binding_stale"
    assert stored_job is not None and stored_job.status == "succeeded"
    assert ticket is not None and ticket.status == "failed"
    assert update is not None and "没有执行任何业务变更" in update.content
