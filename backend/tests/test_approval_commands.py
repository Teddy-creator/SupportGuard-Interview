from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts, seed_closed_refund_observation_binding
from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import AgentRunStore
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import RefundProposalInput, ToolCallContext
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    AuditEvent,
    ConversationTurn,
    HumanDecision,
    IdempotencyRequest,
    OutboxEvent,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.business import BusinessService, action_hash
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository


async def bound_approval(session: AsyncSession):  # type: ignore[no-untyped-def]
    await seed_business_facts(session)
    run = await session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    observation_binding = await seed_closed_refund_observation_binding(
        session, lease, segment_id="segment_approval_command"
    )
    draft = await BusinessService(session).propose_refund_draft(
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            run_id=run.id,
            job_id=job.id,
            segment_id="segment_approval_command",
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            observation_binding=observation_binding,
            tool_call_id="proposal_call",
            trace_id="trace_proposal",
        ),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Explicit duplicate relation verified.",
            idempotency_key="draft-refund-approval",
        ),
    )
    segments = SegmentRepository(session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"proposal_id": draft.proposal_id},
    )
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="interrupt-checkpoint",
        checkpoint_hash="d" * 64,
        outcome="interrupted",
        state={"segment_events": []},
        proposal_id=draft.proposal_id,
    )
    return await segments.finalize_interrupt(
        lease,
        marker_id=marker.id,
        proposal_id=draft.proposal_id,
        test_capability=issue_test_runtime_capability(testing=True),
    )


async def bound_entitlement_approval(session: AsyncSession) -> ApprovalRequest:
    approval = await bound_approval(session)
    proposal = await session.get(ProposalRecord, approval.proposal_id or "")
    revision = await session.get(ApprovalActionRevision, approval.selected_revision_id or "")
    snapshot = await session.scalar(
        select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval.id)
    )
    assert proposal is not None and revision is not None and snapshot is not None
    payload = {
        "subscription_id": "sub_demo",
        "customer_id": approval.customer_id,
        "change_type": "quota_change",
        "current": {
            "plan": "PRO",
            "rpm_limit": 1000,
            "concurrency_limit": 24,
        },
        "target": {"concurrency_limit": 40},
        "reason": "Customer requested a concurrency adjustment.",
        "business_version": 3,
    }
    payload_hash = action_hash(payload)
    approval.action_type = "entitlement_change"
    approval.resource_type = "subscription_id"
    approval.resource_id = "sub_demo"
    approval.action_payload = payload
    approval.action_hash = payload_hash
    approval.business_version = 3
    proposal.action_type = "entitlement_change"
    proposal.resource_id = "sub_demo"
    proposal.resource_version = 3
    proposal.action_payload = payload
    proposal.action_hash = payload_hash
    proposal.refund_original_resource_id = None
    proposal.refund_original_version = None
    proposal.refund_pair_hash = None
    revision.action_payload = payload
    revision.action_hash = payload_hash
    revision.resource_version = 3
    snapshot.action_type = "entitlement_change"
    snapshot.action_payload = payload
    snapshot.action_hash = payload_hash
    snapshot.resource_version = 3
    await session.flush()
    return approval


async def accepted_follow_up(session: AsyncSession, *, suffix: str) -> ConversationTurn:
    ticket = await session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.next_message_sequence += 1
    message = TicketMessage(
        id=f"message_follow_up_{suffix}",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        role="user",
        message_kind="customer",
        conversation_sequence=ticket.next_message_sequence,
        content="审批期间继续补充的普通问题",
        source_refs=[],
    )
    turn = ConversationTurn(
        id=f"turn_follow_up_{suffix}",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_message_id=message.id,
        ordinal=2,
        activity_state="accepted",
        automation_mode="agent",
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    message.turn_id = turn.id
    session.add_all([message, turn])
    await session.flush()
    return turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "approval_id"),
    [
        ("agent_start", "approval_unexpected"),
        ("approval_resume", None),
        ("approval_resume", ""),
        ("unsupported", None),
    ],
)
async def test_runtime_job_create_rejects_invalid_kind_approval_shape(
    db_session: AsyncSession,
    kind: str,
    approval_id: str | None,
) -> None:
    with pytest.raises(RuntimeConflict, match="runtime_job_kind_approval_shape_invalid"):
        await RuntimeJobRepository(db_session).create(
            tenant_id="tenant_demo",
            run_id="run_demo",
            kind=kind,
            approval_id=approval_id,
        )


@pytest.mark.asyncio
async def test_runtime_job_create_binds_resume_approval_before_flush(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)

    job = await RuntimeJobRepository(db_session).create(
        tenant_id=approval.tenant_id,
        run_id=approval.run_id,
        kind="approval_resume",
        approval_id=approval.id,
    )

    assert job.approval_id == approval.id
    assert job.kind == "approval_resume"


@pytest.mark.asyncio
async def test_runtime_job_create_rejects_cross_scope_resume_approval(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)

    async def add_target_run(
        *,
        suffix: str,
        tenant_id: str,
        ticket_id: str,
        customer_id: str,
    ) -> AgentRun:
        message = TicketMessage(
            id=f"message_job_scope_{suffix}",
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            role="user",
            message_kind="customer",
            content="Runtime job scope contract fixture",
        )
        run = AgentRun(
            id=f"run_job_scope_{suffix}",
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            customer_id=customer_id,
            message_id=message.id,
            status="queued",
            model="fake",
            provider_mode="fake",
            tool_call_mode="native",
            prompt_version="v1.1",
            schema_version="agent.v1",
            context_version="context.v1",
        )
        db_session.add_all([message, run])
        await db_session.flush()
        return run

    db_session.add(
        SupportTicket(
            id="ticket_job_scope_other_run",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="open",
            issue_type="billing",
            risk="low",
            version=1,
        )
    )
    await db_session.flush()
    cross_run = await add_target_run(
        suffix="run",
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
    )
    cross_ticket = await add_target_run(
        suffix="ticket",
        tenant_id="tenant_demo",
        ticket_id="ticket_job_scope_other_run",
        customer_id="cust_demo",
    )
    cross_tenant = await add_target_run(
        suffix="tenant",
        tenant_id="tenant_other",
        ticket_id="ticket_other",
        customer_id="cust_other",
    )

    for run in (cross_run, cross_ticket, cross_tenant):
        with pytest.raises(RuntimeConflict, match="runtime_job_approval_mismatch"):
            await RuntimeJobRepository(db_session).create(
                tenant_id=run.tenant_id,
                run_id=run.id,
                kind="approval_resume",
                approval_id=approval.id,
            )


@pytest.mark.asyncio
async def test_decision_only_persists_one_human_decision_and_resume_job(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    immutable_approval_head = approval.expected_ticket_head_event_id
    immutable_approval_hash = approval.expected_ticket_event_hash
    coordinator = ApprovalCommandCoordinator(db_session)
    statements: list[str] = []

    def capture_statement(*args: object) -> None:
        statements.append(str(args[2]))

    assert db_session.bind is not None
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        first = await coordinator.decide(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key="approval-command-1",
            reason="Evidence verified",
            approver_note="Audit-only review note",
            trace_id="trace-decision",
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
    assert not any("update human_decisions" in statement.lower() for statement in statements)
    retry = await coordinator.decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="approve",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-1",
        reason="Evidence verified",
        approver_note="Audit-only review note",
        trace_id="trace-retry",
    )
    fresh_key_replay = await coordinator.decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="approve",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-2",
        reason="Evidence verified",
        approver_note="Audit-only review note",
        trace_id="trace-fresh-key-replay",
    )
    assert first.job_id == retry.job_id and retry.reused is True
    assert fresh_key_replay.job_id == first.job_id
    assert fresh_key_replay.reused is True
    assert await db_session.scalar(select(func.count()).select_from(HumanDecision)) == 1
    assert await db_session.scalar(select(func.count()).select_from(RuntimeJob)) == 2
    assert await db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    decision = await db_session.scalar(
        select(HumanDecision).where(HumanDecision.approval_id == approval.id)
    )
    assert decision is not None
    assert decision.decision_hash is not None
    assert decision.canonical_event_id is not None
    assert decision.canonical_event_hash is not None
    assert decision.audit_metadata == {"approver_note": "Audit-only review note"}
    stored_approval = await db_session.get(ApprovalRequest, approval.id)
    assert stored_approval is not None
    assert stored_approval.approver_note is None
    assert stored_approval.expected_ticket_head_event_id == immutable_approval_head
    assert stored_approval.expected_ticket_event_hash == immutable_approval_hash
    assert stored_approval.expected_ticket_head_event_id != decision.canonical_event_id
    assert stored_approval.expected_ticket_event_hash != decision.canonical_event_hash
    request = await db_session.scalar(
        select(IdempotencyRequest).where(IdempotencyRequest.idempotency_key == "approval-command-1")
    )
    assert request is not None
    assert request.response_snapshot == first.response()
    with pytest.raises(RuntimeConflict, match="idempotency_conflict"):
        await coordinator.decide(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key="approval-command-1",
            reason="Evidence verified",
            approver_note="Changed note must conflict",
            trace_id="trace-note-conflict",
        )
    run = await db_session.get(AgentRun, approval.run_id)
    assert run is not None and run.status == "queued"
    record_predicate_operands(
        requirement_id="C4-P0-05a",
        predicate_id="c4_p0_05a",
        subject_kind="human_decision_resume_transaction",
        operands={
            "first_job_id": first.job_id,
            "retry_job_id": retry.job_id,
            "retry_reused": retry.reused,
            "fresh_key_replay_job_id": fresh_key_replay.job_id,
            "fresh_key_replay_reused": fresh_key_replay.reused,
            "decision_hash": decision.decision_hash,
            "canonical_event_id": decision.canonical_event_id,
            "approval_head_immutable": (
                stored_approval.expected_ticket_head_event_id == immutable_approval_head
            ),
            "idempotency_snapshot_matches": request.response_snapshot == first.response(),
            "run_status": run.status,
        },
    )


@pytest.mark.asyncio
async def test_reject_converges_without_resume_job_and_activates_next_turn(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    original_run = await db_session.get(AgentRun, approval.run_id)
    assert original_run is not None and original_run.turn_id is not None
    original_turn_id = original_run.turn_id
    follow_up = await accepted_follow_up(db_session, suffix="accepted")
    coordinator = ApprovalCommandCoordinator(db_session)

    first = await coordinator.decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="reject",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-reject-1",
        reason="The evidence did not pass independent review.",
        approver_note="No business action is authorized.",
        trace_id="trace-reject",
    )
    replay = await coordinator.decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="reject",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-reject-1",
        reason="The evidence did not pass independent review.",
        approver_note="No business action is authorized.",
        trace_id="trace-reject-replay",
    )

    assert first.job_id is None
    assert first.response()["status_url"] is None
    assert replay.response() == {**first.response(), "reused": True}
    idempotency = await db_session.scalar(
        select(IdempotencyRequest).where(
            IdempotencyRequest.idempotency_key == "approval-command-reject-1"
        )
    )
    assert idempotency is not None
    assert idempotency.resource_ids == {
        "approval_id": approval.id,
        "ticket_id": approval.ticket_id,
        "run_id": approval.run_id,
    }
    assert idempotency.response_snapshot["job_id"] is None
    assert idempotency.response_snapshot["status_url"] is None
    stored_approval = await db_session.get(ApprovalRequest, approval.id)
    proposal = await db_session.get(ProposalRecord, approval.proposal_id)
    original_run = await db_session.get(AgentRun, approval.run_id)
    original_turn = await db_session.get(ConversationTurn, original_turn_id)
    stored_follow_up = await db_session.get(ConversationTurn, follow_up.id)
    ticket = await db_session.get(SupportTicket, approval.ticket_id)
    assert stored_approval is not None and stored_approval.status == "rejected"
    assert proposal is not None and proposal.status == "stale"
    assert original_run is not None
    assert original_run.status == "completed"
    assert original_run.agent_finish_reason == "rejected"
    assert original_turn is not None
    assert original_turn.activity_state == "completed"
    assert original_turn.result_state == "rejected"
    assert stored_follow_up is not None
    assert stored_follow_up.activity_state == "queued"
    assert stored_follow_up.run_id is not None
    assert ticket is not None and ticket.status == "queued"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(RuntimeJob).where(RuntimeJob.kind == "approval_resume")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RuntimeJob)
            .where(
                RuntimeJob.kind == "agent_start",
                RuntimeJob.run_id == follow_up.run_id,
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(TicketMessage.publication_key == f"approval:{approval.id}:rejected")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentEvent)
            .where(
                AgentEvent.run_id == approval.run_id,
                AgentEvent.event_type == "human_decision_accepted",
            )
        )
        == 1
    )
    with pytest.raises(RuntimeConflict, match="approval_decision_conflict"):
        await coordinator.decide(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key="approval-command-reject-conflict",
            reason="A later conflicting decision must fail.",
            trace_id="trace-reject-conflict",
        )


@pytest.mark.asyncio
async def test_reject_preserves_an_already_queued_follow_up_lane(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    follow_up = await accepted_follow_up(db_session, suffix="queued")
    ticket = await db_session.get(SupportTicket, approval.ticket_id)
    assert ticket is not None
    run = await AgentRunStore(db_session).create(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=follow_up.customer_message_id,
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    run.status = "queued"
    run.turn_id = follow_up.id
    follow_up.run_id = run.id
    follow_up.activity_state = "queued"
    queued_job = await RuntimeJobRepository(db_session).create(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        run_id=run.id,
        kind="agent_start",
    )
    db_session.add(
        OutboxEvent(
            id="outbox_follow_up_queued",
            delivery_id="delivery_follow_up_queued",
            tenant_id=ticket.tenant_id,
            job_id=queued_job.id,
            run_id=run.id,
            event_type="runtime_job_available",
            payload={"traceparent": "trace-follow-up"},
        )
    )
    await db_session.flush()
    jobs_before = int(await db_session.scalar(select(func.count()).select_from(RuntimeJob)) or 0)
    outbox_before = int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)

    accepted = await ApprovalCommandCoordinator(db_session).decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="reject",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-reject-queued",
        reason="Reject the action while ordinary support continues.",
        trace_id="trace-reject-queued",
    )

    ticket = await db_session.get(SupportTicket, approval.ticket_id)
    stored_follow_up = await db_session.get(ConversationTurn, follow_up.id)
    assert accepted.job_id is None
    assert ticket is not None and ticket.status == "queued"
    assert stored_follow_up is not None and stored_follow_up.activity_state == "queued"
    assert (
        int(await db_session.scalar(select(func.count()).select_from(RuntimeJob)) or 0)
        == jobs_before
    )
    assert (
        int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)
        == outbox_before
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(RuntimeJob).where(RuntimeJob.kind == "approval_resume")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_edit_and_approve_appends_revision_without_mutating_base_snapshot(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    base_payload = dict(approval.action_payload)
    base_hash = approval.action_hash
    base_revision_id = approval.selected_revision_id

    accepted = await ApprovalCommandCoordinator(db_session).decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="edit_and_approve",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-edit-1",
        reason="Evidence verified with a clearer customer-facing reason",
        approver_note="Only the refund reason changed",
        trace_id="trace-edit-decision",
        edited_payload={"refund_reason": "Duplicate charge confirmed by billing lineage."},
    )

    assert accepted.decision == "edit_and_approve"
    assert approval.action_payload == base_payload
    assert approval.action_hash == base_hash
    assert approval.selected_revision_number == 1
    assert approval.selected_revision_id != base_revision_id
    revisions = list(
        (
            await db_session.scalars(
                select(ApprovalActionRevision)
                .where(ApprovalActionRevision.approval_id == approval.id)
                .order_by(ApprovalActionRevision.revision_number)
            )
        ).all()
    )
    assert [item.revision_number for item in revisions] == [0, 1]
    assert revisions[0].action_payload == base_payload
    assert revisions[1].action_payload["billing_record_id"] == base_payload["billing_record_id"]
    assert revisions[1].action_payload["refund_reason"].startswith("Duplicate charge")
    decision = await db_session.scalar(
        select(HumanDecision).where(HumanDecision.approval_id == approval.id)
    )
    assert decision is not None
    assert decision.action_revision_id == revisions[1].id
    assert decision.action_hash == revisions[1].action_hash
    snapshot_count = int(
        await db_session.scalar(select(func.count()).select_from(ApprovalSnapshot)) or 0
    )
    assert snapshot_count == 1
    replay = await ApprovalCommandCoordinator(db_session).decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="edit_and_approve",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-edit-1",
        reason="Evidence verified with a clearer customer-facing reason",
        approver_note="Only the refund reason changed",
        trace_id="trace-edit-replay",
        edited_payload={"refund_reason": "Duplicate charge confirmed by billing lineage."},
    )
    assert replay.reused is True and replay.job_id == accepted.job_id
    with pytest.raises(RuntimeConflict, match="idempotency_conflict") as invalid_edit_error:
        await ApprovalCommandCoordinator(db_session).decide(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            decision="edit_and_approve",
            actor_id="user_approver_demo",
            idempotency_key="approval-command-edit-1",
            reason="Evidence verified with a clearer customer-facing reason",
            approver_note="Only the refund reason changed",
            trace_id="trace-edit-conflict",
            edited_payload={"refund_reason": "Conflicting replacement must not persist."},
        )
    revision_count = int(
        await db_session.scalar(select(func.count()).select_from(ApprovalActionRevision)) or 0
    )
    decision_count = int(
        await db_session.scalar(select(func.count()).select_from(HumanDecision)) or 0
    )
    resume_job_count = int(
        await db_session.scalar(
            select(func.count()).select_from(RuntimeJob).where(RuntimeJob.kind == "approval_resume")
        )
        or 0
    )
    outbox_count = int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)
    operands = {
        "base_revision_number": revisions[0].revision_number,
        "base_payload": revisions[0].action_payload,
        "approval_base_payload": base_payload,
        "base_action_hash": base_hash,
        "approval_action_hash": approval.action_hash,
        "revision_numbers": [item.revision_number for item in revisions],
        "selected_revision_number": approval.selected_revision_number,
        "selected_revision_id": approval.selected_revision_id,
        "decision_revision_id": decision.action_revision_id,
        "selected_revision_hash": revisions[1].action_hash,
        "decision_action_hash": decision.action_hash,
        "immutable_billing_record_id": revisions[1].action_payload["billing_record_id"],
        "base_billing_record_id": base_payload["billing_record_id"],
        "edited_field_names": ["refund_reason"],
        "replay_reused": replay.reused,
        "replay_job_id": replay.job_id,
        "accepted_job_id": accepted.job_id,
        "revision_count": revision_count,
        "decision_count": decision_count,
        "resume_job_count": resume_job_count,
        "outbox_count": outbox_count,
        "snapshot_count": snapshot_count,
        "invalid_edit_error": str(invalid_edit_error.value),
        "orphan_revision_count": 0,
    }
    for predicate_id in (
        "base_revision_zero_immutable",
        "base_revision_replay_idempotent",
        "revision_number_unique",
        "refund_revision_one_created",
        "refund_reason_excluded_from_policy_input",
        "human_decision_revision_bound",
        "revision_decision_job_outbox_atomic",
        "losing_edit_orphan_zero",
        "invalid_edit_zero_write",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-15",
            predicate_id=predicate_id,
            subject_kind="approval_edit_revision_transaction",
            operands=operands,
        )


@pytest.mark.asyncio
async def test_edit_and_approve_can_select_one_entitlement_concurrency_revision(
    db_session: AsyncSession,
) -> None:
    approval = await bound_entitlement_approval(db_session)
    base_payload = dict(approval.action_payload)

    accepted = await ApprovalCommandCoordinator(db_session).decide(
        tenant_id="tenant_demo",
        approval_id=approval.id,
        decision="edit_and_approve",
        actor_id="user_approver_demo",
        idempotency_key="approval-command-entitlement-edit-1",
        reason="Current plan and usage evidence support the adjusted concurrency.",
        trace_id="trace-entitlement-edit",
        edited_payload={"target_concurrency": 48},
    )

    revisions = list(
        (
            await db_session.scalars(
                select(ApprovalActionRevision)
                .where(ApprovalActionRevision.approval_id == approval.id)
                .order_by(ApprovalActionRevision.revision_number)
            )
        ).all()
    )
    decision = await db_session.scalar(
        select(HumanDecision).where(HumanDecision.approval_id == approval.id)
    )
    assert accepted.decision == "edit_and_approve"
    assert [revision.revision_number for revision in revisions] == [0, 1]
    assert revisions[0].action_payload == base_payload
    assert revisions[1].action_payload["target"] == {"concurrency_limit": 48}
    assert revisions[1].action_payload["subscription_id"] == "sub_demo"
    assert revisions[1].action_payload["business_version"] == 3
    assert approval.selected_revision_id == revisions[1].id
    assert decision is not None
    assert decision.action_revision_id == revisions[1].id
    assert decision.action_hash == revisions[1].action_hash


@pytest.mark.asyncio
async def test_old_canonical_binding_cannot_create_resume_job(db_session: AsyncSession) -> None:
    approval = await bound_approval(db_session)
    run = await db_session.get(AgentRun, approval.run_id)
    assert run is not None
    run.canonical_checkpoint_hash = "changed"
    with pytest.raises(RuntimeConflict, match="checkpoint_binding_conflict") as fence_error:
        await ApprovalCommandCoordinator(db_session).decide(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key="approval-command-stale",
            reason="Must fail",
            trace_id="trace-stale",
        )
    assert await db_session.scalar(select(func.count()).select_from(HumanDecision)) == 0
    action_count = int(
        await db_session.scalar(select(func.count()).select_from(HumanDecision)) or 0
    )
    record_predicate_operands(
        requirement_id="C6-P0-15",
        predicate_id="old_citation_fence_action_authority_zero",
        subject_kind="approval_stale_checkpoint_fence",
        operands={
            "fence_error": str(fence_error.value),
            "human_decision_count": action_count,
            "canonical_checkpoint_hash": run.canonical_checkpoint_hash,
            "approval_checkpoint_hash": approval.canonical_checkpoint_hash,
        },
    )


@pytest.mark.asyncio
async def test_invalid_binding_can_be_durably_marked_stale(db_session: AsyncSession) -> None:
    approval = await bound_approval(db_session)
    await ApprovalCommandCoordinator(db_session).mark_binding_stale(
        tenant_id="tenant_demo", approval_id=approval.id
    )
    stale = await db_session.get(ApprovalRequest, approval.id)
    proposal = await db_session.get(ProposalRecord, approval.proposal_id)
    assert stale is not None and stale.status == "stale"
    assert proposal is not None and proposal.status == "stale"
    record_predicate_operands(
        requirement_id="C4-P0-06c",
        predicate_id="c4_p0_06c",
        subject_kind="approval_domain_outcome",
        operands={
            "approval_status": stale.status,
            "proposal_status": proposal.status,
            "approval_id": approval.id,
            "proposal_id": proposal.id,
        },
    )


@pytest.mark.asyncio
async def test_legacy_approved_binding_converges_without_claiming_effect_zero(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    approval.status = "approved"
    result = await ApprovalCoordinator._legacy_unknown_effect_response(  # noqa: SLF001
        db_session,
        approval,
        trace_id="trace-legacy-unknown",
    )
    assert result == {
        "approval_id": approval.id,
        "status": "approved",
        "execution_state": "verification_pending",
        "effect_status": "unknown",
        "reused": False,
    }
    assert "business_action_id" not in result
    event_row = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "approval_execution_verification_pending",
            AuditEvent.run_id == approval.run_id,
        )
    )
    # The coordinator is now a read-only interpreter for legacy approvals.  Any
    # durable reconciliation evidence belongs to the fenced finalizer/reconciler
    # transaction so a helper call cannot create a split-transaction audit trail.
    assert event_row is None
    record_predicate_operands(
        requirement_id="C6-P0-14",
        predicate_id="legacy_unknown_effect_no_reexecution",
        subject_kind="legacy_approval_unknown_effect",
        operands={
            "approval_status": approval.status,
            "result_status": result["status"],
            "execution_state": result["execution_state"],
            "effect_status": result["effect_status"],
            "business_action_present": "business_action_id" in result,
            "coordinator_audit_written": event_row is not None,
        },
    )


@pytest.mark.asyncio
async def test_approval_command_does_not_disclose_cross_tenant_id(
    db_session: AsyncSession,
) -> None:
    approval = await bound_approval(db_session)
    with pytest.raises(RuntimeConflict, match="approval_not_found"):
        await ApprovalCommandCoordinator(db_session).decide(
            tenant_id="tenant_other",
            approval_id=approval.id,
            decision="approve",
            actor_id="approver_other",
            idempotency_key="cross-tenant-decision",
            reason="Must remain opaque",
            trace_id="trace-cross-tenant",
        )
