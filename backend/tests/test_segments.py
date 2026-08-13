from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts, seed_closed_refund_observation_binding
from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import AgentRunStore, verify_ticket_event_chain
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import EscalationCausalDecisionV2
from supportguard.contracts.finalizer import StateDelta
from supportguard.contracts.tools import RefundProposalInput, ToolCallContext
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApprovalRequest,
    AuditEvent,
    CheckpointCommitMarker,
    FinalizerPayload,
    ProposalRecord,
    RawProviderDecisionEnvelope,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    TicketSummary,
)
from supportguard.services.attempts import AttemptLedger
from supportguard.services.business import BusinessService
from supportguard.services.capability_ledger import PolicyCapabilityLedger
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.runtime_jobs import (
    FinalizerRestartRequired,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.segment_common import (
    _final_message_source_refs,
    _restartable_pre_effect_head_paths,
    _validated_finalizer_terminal,
)
from supportguard.services.segments import SegmentRepository


def test_final_message_sources_are_bounded_to_the_published_final() -> None:
    state = {
        "final": {
            "business_source_ids": ["billing-source"],
            "knowledge_chunk_ids": ["knowledge-source"],
        },
        "tool_observations": [
            {
                "source_refs": [
                    {
                        "source_type": "business_record",
                        "source_id": "billing-source",
                        "observed_at": "2026-07-27T08:00:00Z",
                    },
                    {
                        "source_type": "business_record",
                        "source_id": "unpublished-source",
                        "observed_at": "2026-07-27T08:00:00Z",
                    },
                ]
            },
            {
                "source_refs": [
                    {
                        "source_type": "knowledge_chunk",
                        "source_id": "knowledge-source",
                        "observed_at": "2026-07-27T08:00:00Z",
                    },
                    {
                        "source_type": "business_record",
                        "source_id": "billing-source",
                        "observed_at": "2026-07-27T08:00:00Z",
                    },
                ]
            },
        ],
    }

    refs = _final_message_source_refs(state)

    assert [item["source_id"] for item in refs] == [
        "billing-source",
        "knowledge-source",
    ]


def test_only_aggregate_resource_versions_allow_a_pre_effect_restart() -> None:
    assert _restartable_pre_effect_head_paths(
        (
            "expected_heads.expected_domain_resource_versions.ticket:ticket_demo",
            "expected_heads.expected_domain_resource_versions.run:run_demo",
            "expected_heads.expected_domain_resource_versions.job:job_demo",
        )
    )
    for forbidden in (
        "expected_heads.expected_capability_ledger_head",
        "expected_heads.expected_context_ledger_hash",
        "expected_heads.expected_budget_ledger_head",
        "expected_heads.expected_marker_status_version",
        "state_delta.state.final.answer",
    ):
        assert not _restartable_pre_effect_head_paths((forbidden,))
    assert not _restartable_pre_effect_head_paths(())


def _effect_free_finalizer_state(run: AgentRun) -> dict[str, object]:
    return {
        "ticket_id": run.ticket_id,
        "customer_id": run.customer_id,
        "run_id": run.id,
        "trace_id": f"trace:{run.id}",
        "classification": {"issue_type": "product_knowledge", "risk": "low"},
        "agent_finish_reason": "answered",
        "segment_events": [],
        "tool_observations": [],
        "evidence": [],
        "final": {
            "answer": "Current state was read safely.",
            "terminal_state": "resolved",
            "policy_route": "answer",
        },
    }


@pytest.mark.asyncio
async def test_effect_free_resource_head_drift_restarts_once_without_publication(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert run is not None and ticket is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-pre-effect-restart")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": run.message_id},
    )
    reserved = await AttemptLedger(db_session).reserve(lease, kind="llm")
    envelope = RawProviderDecisionEnvelope(
        tenant_id=lease.tenant_id,
        run_id=run.id,
        job_id=job.id,
        segment_id=marker.id,
        fencing_token=lease.fencing_token,
        provider_attempt_id=reserved.id,
        finish_reason="stop",
        response_hash="a" * 64,
        content_hash="b" * 64,
        call_count=0,
        call_manifest=[],
        intake_status="parsed",
    )
    db_session.add(envelope)
    await AttemptLedger(db_session).finish(lease, reserved, status="succeeded")
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-pre-effect-restart",
        checkpoint_hash="c" * 64,
        outcome="completed",
        state=_effect_free_finalizer_state(run),
    )

    ticket.version += 1
    with pytest.raises(
        FinalizerRestartRequired,
        match="pre_effect_finalizer_restart_required",
    ):
        await segments.finalize(lease, marker_id=marker.id)

    assert marker.status == "aborted"
    assert run.status == "running"
    assert run.active_job_id == job.id
    assert job.status == "leased"
    assert envelope.intake_status == "rejected"
    assert envelope.rejection_code == "finalizer_head_changed_before_publication"
    restart = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.run_id == run.id,
            AuditEvent.event_type == "pre_effect_finalizer_restart",
        )
    )
    assert restart is not None
    assert restart.payload["mismatch_paths"] == [
        "expected_heads.expected_domain_resource_versions.ticket:ticket_demo"
    ]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(
                TicketMessage.run_id == run.id,
                TicketMessage.message_kind == "assistant",
            )
        )
        == 0
    )

    retry_status = await jobs.fail(
        lease,
        error_code="pre_effect_finalizer_restart_required",
    )
    assert retry_status == "retry_wait"
    assert run.status == "queued"
    job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    replacement_lease = await jobs.claim(
        job_id=job.id,
        owner="worker-pre-effect-restart-successor",
    )
    replacement = await segments.prepare(
        replacement_lease,
        delivery_generation=2,
        segment_kind="agent_start",
        segment_input={"message_id": run.message_id},
    )
    assert replacement_lease.fencing_token == lease.fencing_token + 1
    assert replacement.id != marker.id
    assert replacement.status == "prepared"
    assert replacement.canonical_parent_id == marker.canonical_parent_id
    assert marker.status == "aborted"
    assert envelope.intake_status == "rejected"


@pytest.mark.asyncio
async def test_second_effect_free_resource_head_drift_fails_closed(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert run is not None and ticket is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-restart-exhausted")
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
        checkpoint_id="checkpoint-restart-exhausted",
        checkpoint_hash="d" * 64,
        outcome="completed",
        state=_effect_free_finalizer_state(run),
    )
    db_session.add(
        AuditEvent(
            tenant_id=lease.tenant_id,
            ticket_id=run.ticket_id,
            customer_id=run.customer_id,
            event_type="pre_effect_finalizer_restart",
            actor_type="runtime",
            actor_id="worker-prior",
            payload={"reason": "prior_bounded_restart"},
            trace_id="trace-prior-restart",
            run_id=run.id,
        )
    )
    ticket.version += 1

    with pytest.raises(
        RuntimeConflict,
        match="pre_effect_finalizer_restart_exhausted",
    ):
        await segments.finalize(lease, marker_id=marker.id)

    assert marker.status == "aborted"
    assert run.status == "failed"
    assert job.status == "dead"


@pytest.mark.parametrize("terminal", [None, "unknown", "await_human_approval"])
def test_finalizer_contract_rejects_missing_or_unknown_embedded_terminal(
    terminal: str | None,
) -> None:
    final = {"answer": "must not publish"}
    if terminal is not None:
        final["terminal_state"] = terminal

    with pytest.raises(ValueError, match="terminal state is missing or unsupported"):
        StateDelta.model_validate({"state": {"final": final}})


def test_manual_takeover_terminal_requires_legal_approval_resume_decision() -> None:
    state = {
        "final": {"terminal_state": "manual_takeover"},
        "human_decision": {"action": "manual_takeover"},
    }

    assert (
        _validated_finalizer_terminal(
            segment_kind="approval_resume",
            outcome="completed",
            state=state,
        )
        == "manual_takeover"
    )
    with pytest.raises(RuntimeConflict, match="segment_manual_takeover_forbidden"):
        _validated_finalizer_terminal(
            segment_kind="agent_start",
            outcome="completed",
            state=state,
        )


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"final": {}},
        {"final": {"terminal_state": "unknown"}},
        {
            "final": {"terminal_state": "manual_takeover"},
            "human_decision": {"action": "manual_takeover"},
        },
    ],
)
@pytest.mark.asyncio
async def test_completed_checkpoint_rejects_corrupt_terminal_before_payload_persistence(
    db_session: AsyncSession,
    state: dict[str, object],
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-terminal-contract")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": run.message_id},
    )

    with pytest.raises(
        RuntimeConflict,
        match=(
            "segment_final_state_missing"
            "|segment_terminal_state_invalid"
            "|segment_manual_takeover_forbidden"
        ),
    ):
        await segments.checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id="checkpoint-invalid-terminal",
            checkpoint_hash="9" * 64,
            outcome="completed",
            state=state,
        )

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(FinalizerPayload)
            .where(FinalizerPayload.marker_id == marker.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_only_current_fence_can_finalize_private_checkpoint(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": run.message_id},
    )
    assert marker.private_namespace.endswith(f"/{lease.fencing_token}")
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-final",
        checkpoint_hash="a" * 64,
        outcome="completed",
        state={
            "ticket_id": "ticket_demo",
            "customer_id": "cust_demo",
            "run_id": run.id,
            "trace_id": "trace-finalizer",
            "classification": {"issue_type": "product_knowledge", "risk": "low"},
            "agent_finish_reason": "answered",
            "segment_events": [],
            "tool_observations": [],
            "evidence": [],
            "final": {
                "answer": "Finalized safely.",
                "terminal_state": "resolved",
                "policy_route": "answer",
            },
        },
    )
    finalized = await segments.finalize(lease, marker_id=marker.id)
    assert finalized.status == "finalized"
    assert run.canonical_checkpoint_id == "checkpoint-final"


@pytest.mark.asyncio
async def test_prepared_old_fence_never_becomes_canonical(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    old_lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    marker = await SegmentRepository(db_session).prepare(
        old_lease, delivery_generation=1, segment_kind="agent_start", segment_input={}
    )
    job.status = "queued"
    run.status = "queued"
    await jobs.claim(job_id=job.id, owner="worker-b", now=datetime.now(UTC))
    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await SegmentRepository(db_session).checkpoint_written(
            old_lease,
            marker_id=marker.id,
            checkpoint_id="orphan",
            checkpoint_hash="b" * 64,
            outcome="completed",
            state={},
        )
    stored = await db_session.get(CheckpointCommitMarker, marker.id)
    assert stored is not None and stored.status == "prepared"
    assert run.canonical_checkpoint_id is None


@pytest.mark.asyncio
async def test_prepare_materializes_a_same_run_boundary_after_another_run(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    prior_run = await db_session.get(AgentRun, "run_demo")
    assert prior_run is not None
    prior_event = await AgentRunStore(db_session).append_event(
        prior_run,
        event_type="prior_run_terminal",
        payload={"terminal_state": "resolved"},
    )
    db_session.add(
        TicketMessage(
            id="message_followup",
            tenant_id="tenant_demo",
            ticket_id="ticket_demo",
            role="user",
            content="Follow-up request",
        )
    )
    await db_session.flush()
    current_run = AgentRun(
        id="run_followup",
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        message_id="message_followup",
        status="queued",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        prompt_version="agent_decide.v3",
        schema_version="agent.v1",
        context_version="context.v1.2",
    )
    db_session.add(current_run)
    await db_session.flush()
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=current_run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-followup")

    marker = await SegmentRepository(db_session).prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": "message_followup"},
    )
    boundary = await db_session.get(AgentEvent, marker.expected_ticket_head_event_id)

    assert boundary is not None
    assert boundary.run_id == current_run.id
    assert boundary.event_type == "run_started"
    assert boundary.previous_event_id is None
    assert boundary.parent_event_hash == prior_event.event_hash
    assert boundary.ticket_sequence == prior_event.ticket_sequence + 1
    assert marker.expected_ticket_sequence == boundary.ticket_sequence
    assert await verify_ticket_event_chain(db_session, "ticket_demo") == boundary.event_hash


@pytest.mark.asyncio
async def test_checkpoint_written_recovery_runs_finalizer_only_without_new_attempts(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        old_lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"kind": "agent_start"},
    )
    state = {
        "ticket_id": "ticket_demo",
        "customer_id": "cust_demo",
        "run_id": run.id,
        "trace_id": "trace_k4",
        "classification": {"issue_type": "product_knowledge", "risk": "low"},
        "agent_finish_reason": "answered",
        "tool_observations": [],
        "evidence": [],
        "segment_events": [],
        "final": {
            "answer": "Recovered without external calls.",
            "terminal_state": "resolved",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "policy_route": "answer",
        },
    }
    await segments.checkpoint_written(
        old_lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-k4",
        checkpoint_hash="f" * 64,
        outcome="completed",
        state=state,
    )
    attempts_before = await db_session.scalar(select(func.count(AgentCallAttempt.id)))
    # Exercise the real retry transition rather than manufacturing the CAS
    # versions expected by the finalizer-only takeover contract.
    retry_status = await jobs.fail(
        old_lease,
        error_code="failed:checkpoint_written_finalizer_interrupted",
    )
    assert retry_status == "retry_wait"
    job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
    new_lease = await jobs.claim(job_id=job.id, owner="worker-new")
    replacement = await segments.takeover_finalizer(new_lease, source_marker_id=marker.id)
    await segments.finalize(new_lease, marker_id=replacement.id)
    attempts_after = await db_session.scalar(select(func.count(AgentCallAttempt.id)))
    assert attempts_after == attempts_before
    assert marker.status == "aborted"
    assert replacement.status == "finalized"
    assert run.canonical_checkpoint_id == "checkpoint-k4"
    record_predicate_operands(
        requirement_id="C5-P0-05",
        predicate_id="checkpoint_written_finalizer_only",
        subject_kind="checkpoint_finalizer_takeover",
        operands={
            "attempts_before": int(attempts_before or 0),
            "attempts_after": int(attempts_after or 0),
            "source_marker_status": marker.status,
            "replacement_marker_status": replacement.status,
            "canonical_checkpoint_id": run.canonical_checkpoint_id,
        },
    )
    record_predicate_operands(
        requirement_id="C4-P0-03c",
        predicate_id="c4_p0_03c",
        subject_kind="checkpoint_finalizer_takeover",
        operands={
            "attempts_before": int(attempts_before or 0),
            "attempts_after": int(attempts_after or 0),
            "source_marker_status": marker.status,
            "replacement_marker_status": replacement.status,
            "canonical_checkpoint_id": run.canonical_checkpoint_id,
        },
    )


@pytest.mark.asyncio
async def test_draft_proposal_without_citation_lineage_never_becomes_actionable(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    observation_binding = await seed_closed_refund_observation_binding(
        db_session, lease, segment_id="segment_draft"
    )
    draft = await BusinessService(db_session).propose_refund_draft(
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            run_id=run.id,
            job_id=job.id,
            segment_id="segment_draft",
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            observation_binding=observation_binding,
            tool_call_id="proposal_call",
            trace_id="trace_proposal",
        ),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Explicit duplicate relation verified.",
            idempotency_key="draft-refund-001",
        ),
    )
    assert await db_session.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    segments = SegmentRepository(db_session)
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
        checkpoint_hash="c" * 64,
        outcome="interrupted",
        state={"segment_events": []},
        proposal_id=draft.proposal_id,
    )
    with pytest.raises(RuntimeConflict, match="approval_citation_lineage_missing"):
        await segments.finalize_interrupt(
            lease,
            marker_id=marker.id,
            proposal_id=draft.proposal_id,
        )
    assert await db_session.scalar(select(func.count()).select_from(ApprovalRequest)) == 0


@pytest.mark.asyncio
async def test_refund_draft_uses_active_fence_not_ticket_projection_status(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert run is not None and ticket is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    observation_binding = await seed_closed_refund_observation_binding(
        db_session, lease, segment_id="segment_projection_lag"
    )
    # Ticket status is a customer projection that may converge independently.
    # The current run/job/fence and closed evidence ledger remain authoritative.
    ticket.status = "resolved"
    await db_session.flush()

    draft = await BusinessService(db_session).propose_refund_draft(
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            run_id=run.id,
            job_id=job.id,
            segment_id="segment_projection_lag",
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            observation_binding=observation_binding,
            tool_call_id="proposal_projection_lag",
            trace_id="trace_projection_lag",
        ),
        RefundProposalInput(
            billing_record_id="bill_duplicate",
            refund_reason="Explicit duplicate relation verified.",
            idempotency_key="draft-refund-projection-lag",
        ),
    )

    assert draft.status == "draft"
    assert draft.resource_id == "bill_duplicate"
    assert await db_session.scalar(select(func.count()).select_from(ApprovalRequest)) == 0


@pytest.mark.asyncio
async def test_fenced_proposal_without_current_observation_binding_is_rejected(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    with pytest.raises(DomainError) as error:
        await BusinessService(db_session).propose_refund_draft(
            ToolCallContext(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id="ticket_demo",
                run_id=run.id,
                job_id=job.id,
                segment_id="segment_unbound",
                delivery_generation=1,
                fencing_token=lease.fencing_token,
                tool_call_id="unbound_proposal",
                trace_id="trace_unbound",
            ),
            RefundProposalInput(
                billing_record_id="bill_duplicate",
                refund_reason="No trusted read observations exist.",
                idempotency_key="unbound-proposal",
            ),
        )

    assert error.value.code is ErrorCode.TICKET_STATE_CONFLICT


@pytest.mark.asyncio
async def test_fenced_proposal_rejects_tampered_durable_observation_hash(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    binding = await seed_closed_refund_observation_binding(
        db_session, lease, segment_id="segment_tampered"
    )
    binding[0]["observation_content_hash"] = "0" * 64

    with pytest.raises(DomainError) as error:
        await BusinessService(db_session).propose_refund_draft(
            ToolCallContext(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id="ticket_demo",
                run_id=run.id,
                job_id=job.id,
                segment_id="segment_tampered",
                delivery_generation=1,
                fencing_token=lease.fencing_token,
                observation_binding=binding,
                tool_call_id="tampered_proposal",
                trace_id="trace_tampered",
            ),
            RefundProposalInput(
                billing_record_id="bill_duplicate",
                refund_reason="Tampered binding must fail closed.",
                idempotency_key="tampered-proposal",
            ),
        )

    assert error.value.code is ErrorCode.TICKET_STATE_CONFLICT
    record_predicate_operands(
        requirement_id="C4-P0-06b",
        predicate_id="c4_p0_06b",
        subject_kind="proposal_observation_binding_revalidation",
        operands={
            "error_code": error.value.code.value,
            "tampered_observation_hash": binding[0]["observation_content_hash"],
            "proposal_count": int(
                await db_session.scalar(select(func.count()).select_from(ProposalRecord)) or 0
            ),
        },
    )


@pytest.mark.asyncio
async def test_segment_finalize_atomically_persists_domain_job_event_and_memory(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"kind": "agent_start"},
    )
    final_state = {
        "ticket_id": "ticket_demo",
        "customer_id": "cust_demo",
        "run_id": run.id,
        "trace_id": "trace_atomic_finalize",
        "classification": {"issue_type": "product_knowledge", "risk": "low"},
        "agent_finish_reason": "answered",
        "tool_rounds": 1,
        "tool_attempts": 1,
        "llm_calls": 2,
        "tool_observations": [],
        "evidence": [],
        "segment_events": [
            {
                "event_type": "agent_decision",
                "payload": {"decision_type": "final_candidate"},
                "visibility": "customer",
                "status": "completed",
                "step_index": 1,
                "tool_round": 1,
            }
        ],
        "final": {
            "answer": "Grounded answer.",
            "terminal_state": "resolved",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "policy_route": "answer",
        },
    }
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-atomic-final",
        checkpoint_hash="e" * 64,
        outcome="completed",
        state=final_state,
    )
    await segments.finalize(lease, marker_id=marker.id)

    stored_job = await db_session.get(RuntimeJob, job.id)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    summary = await db_session.scalar(
        select(TicketSummary).where(TicketSummary.ticket_id == "ticket_demo")
    )
    assert marker.status == "finalized"
    assert run.status == "completed" and run.canonical_checkpoint_id == "checkpoint-atomic-final"
    assert stored_job is not None and stored_job.status == "succeeded"
    assert stored_job.lease_owner is None and stored_job.lease_expires_at is None
    assert ticket is not None and ticket.status == "resolved"
    assert summary is not None and summary.tenant_id == "tenant_demo"


@pytest.mark.asyncio
async def test_invalid_finalizer_payload_commits_abort_and_cannot_be_reselected(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"kind": "agent_start"},
    )
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-tampered",
        checkpoint_hash="9" * 64,
        outcome="completed",
        state={
            "agent_finish_reason": "answered",
            "segment_events": [],
            "final": {
                "answer": "Valid before persisted payload tampering.",
                "terminal_state": "resolved",
                "knowledge_chunk_ids": [],
                "business_source_ids": [],
                "policy_route": "answer",
            },
        },
    )
    payload = await db_session.scalar(
        select(FinalizerPayload).where(FinalizerPayload.marker_id == marker.id)
    )
    assert payload is not None
    payload.full_payload = {**payload.full_payload, "payload_hash": "0" * 64}
    await db_session.flush()

    with pytest.raises(RuntimeConflict, match="finalizer_payload_hash_mismatch"):
        await segments.finalize(lease, marker_id=marker.id)

    stored_marker = await db_session.get(CheckpointCommitMarker, marker.id)
    stored_job = await db_session.get(RuntimeJob, job.id)
    assert stored_marker is not None and stored_marker.status == "aborted"
    assert stored_job is not None and stored_job.status == "dead"
    recoverable = await db_session.scalar(
        select(CheckpointCommitMarker.id).where(
            CheckpointCommitMarker.job_id == job.id,
            CheckpointCommitMarker.status == "checkpoint_written",
        )
    )
    assert recoverable is None


@pytest.mark.asyncio
async def test_finalizer_recomputes_capability_head_before_publication(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"kind": "agent_start"},
    )
    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-head-cas",
        checkpoint_hash="8" * 64,
        outcome="completed",
        state={
            "ticket_id": "ticket_demo",
            "customer_id": "cust_demo",
            "run_id": run.id,
            "trace_id": "trace-head-cas",
            "classification": {"issue_type": "product_knowledge", "risk": "low"},
            "agent_finish_reason": "answered",
            "segment_events": [],
            "tool_observations": [],
            "evidence": [],
            "final": {
                "answer": "Head is frozen.",
                "terminal_state": "resolved",
                "policy_route": "answer",
            },
        },
    )
    await PolicyCapabilityLedger(db_session).reserve(
        lease,
        segment_id=marker.id,
        capability_name="create_support_escalation",
        causal_decision=EscalationCausalDecisionV2(
            ticket_id="ticket_demo",
            ticket_version=1,
            customer_id="cust_demo",
            model_arguments={"reason": "manual takeover"},
            observation_binding_hash=canonical_json_hash([]),
            policy_version="supportguard-policy-gate.v1",
        ),
        observation_binding=[],
    )

    with pytest.raises(RuntimeConflict, match="finalizer_head_conflict"):
        await segments.finalize(lease, marker_id=marker.id)
    assert marker.status == "aborted"
    operands = {
        "marker_status": marker.status,
        "capability_count": 1,
        "checkpoint_id": "checkpoint-head-cas",
        "checkpoint_hash": "8" * 64,
        "tool_observation_count": 0,
        "proposal_count": 0,
        "budget_tool_attempts": run.tool_attempts,
        "finalizer_error": "finalizer_head_conflict",
    }
    for predicate_id in (
        "tool_head_exact",
        "capability_head_exact",
        "proposal_head_exact",
        "budget_context_head_exact",
    ):
        record_predicate_operands(
            requirement_id="C5-P0-10",
            predicate_id=predicate_id,
            subject_kind="finalizer_capability_head_cas",
            operands=operands,
        )
