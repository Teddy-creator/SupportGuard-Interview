from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import AgentRunStore, CanonicalEventHeadConflict
from supportguard.approvals.snapshot import approval_snapshot_hash, approval_snapshot_payload
from supportguard.contracts.finalizer import (
    ActionfulApprovalResumeDelta,
    ActionIntentApprovalResumeDelta,
    AgentCompleteDelta,
    FailClosedApprovalResumeDelta,
    FinalizerHeadsV2,
    FinalizerPayloadV2,
    HitlInterruptDelta,
    NoActionApprovalResumeDelta,
    canonical_hash,
)
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    BusinessAction,
    CheckpointCommitMarker,
    CitationBinding,
    ContextLedger,
    ConversationTurn,
    FinalizerPayload,
    HumanDecision,
    PolicyCapabilityAttempt,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    ProposalRecord,
    RetrievalTrace,
    SupportTicket,
    ToolInvocation,
    ToolObservation,
    ToolTransportAttempt,
    TurnGroup,
    new_id,
)
from supportguard.db.scope import set_local_scope
from supportguard.rag.citations import (
    CitationPublicationConflict,
    CitationPublicationValidator,
)
from supportguard.services.approval_lifecycle import (
    ActionLifecycleService,
    canonical_approval_identity,
)
from supportguard.services.runtime_jobs import (
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.segment_common import (
    _activate_and_converge_application_fallback,
    _final_message_source_refs,
    _validated_finalizer_terminal,
    stable_hash,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:

    class _ApprovalDependencies:
        def _marker_lock_snapshot(self, marker: CheckpointCommitMarker) -> tuple[object, ...]:
            raise NotImplementedError

        async def _lock_interrupt_finalize_domain(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _lock_marker_after_domain(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _abort_finalizer(self, *args: Any, **kwargs: Any) -> Never:
            raise NotImplementedError

        async def _publish_message(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def _log_finalizer_payload_mismatch(self, *args: Any, **kwargs: Any) -> None:
            raise NotImplementedError

else:

    class _ApprovalDependencies:
        pass


class ApprovalResumeSegments(_ApprovalDependencies):
    session: AsyncSession

    async def _insert_active_approval(
        self,
        approval: ApprovalRequest,
    ) -> str | None:
        """Atomically claim one active resource identity on PostgreSQL.

        The partial unique index is the serialization point. ``DO NOTHING``
        keeps a losing transaction usable so it can roll back only its
        provisional event/state savepoint and converge on the committed winner.
        """

        statement = (
            postgresql_insert(ApprovalRequest)
            .values(
                id=approval.id,
                tenant_id=approval.tenant_id,
                ticket_id=approval.ticket_id,
                customer_id=approval.customer_id,
                proposal_id=approval.proposal_id,
                run_id=approval.run_id,
                checkpoint_id=approval.checkpoint_id,
                canonical_checkpoint_ns=approval.canonical_checkpoint_ns,
                canonical_checkpoint_hash=approval.canonical_checkpoint_hash,
                checkpoint_version=approval.checkpoint_version,
                marker_id=approval.marker_id,
                expected_ticket_head_event_id=approval.expected_ticket_head_event_id,
                expected_ticket_sequence=approval.expected_ticket_sequence,
                expected_ticket_event_hash=approval.expected_ticket_event_hash,
                action_type=approval.action_type,
                resource_type=approval.resource_type,
                resource_id=approval.resource_id,
                origin_turn_id=approval.origin_turn_id,
                action_payload=approval.action_payload,
                review_context=approval.review_context,
                action_hash=approval.action_hash,
                business_version=approval.business_version,
                status="pending",
                idempotency_key=approval.idempotency_key,
                selected_revision_id=approval.selected_revision_id,
                selected_revision_number=approval.selected_revision_number,
                status_version=1,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ApprovalRequest.tenant_id,
                    ApprovalRequest.customer_id,
                    ApprovalRequest.action_type,
                    ApprovalRequest.resource_id,
                ],
                index_where=text("status IN ('pending','approved')"),
            )
            .returning(ApprovalRequest.id)
        )
        return cast(str | None, await self.session.scalar(statement))

    async def _finalize_reused_approval(
        self,
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        run: AgentRun,
        proposal: ProposalRecord,
        ticket: SupportTicket,
        active_approval: ApprovalRequest,
        verified: FinalizerPayloadV2,
    ) -> ApprovalRequest:
        """Converge a duplicate proposal onto the canonical active Approval."""

        proposal.status = "stale"
        marker.status = "finalized"
        marker.status_version += 1
        run.canonical_checkpoint_ns = marker.private_namespace
        run.canonical_checkpoint_id = marker.final_checkpoint_id
        run.canonical_checkpoint_hash = marker.final_checkpoint_hash
        run.canonical_checkpoint_version = int(marker.final_checkpoint_version or 0)
        run.status = "completed"
        run.checkpoint_stage = "completed"
        run.checkpoint_id = marker.final_checkpoint_id
        run.agent_finish_reason = "active_approval_reused"
        run.active_job_id = None
        run.active_fencing_token = None
        run.completed_at = datetime.now(UTC)
        run.status_version += 1
        turn = (
            await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
            if run.turn_id
            else None
        )
        if turn is not None:
            turn.activity_state = "completed"
            turn.result_state = "answered"
            turn.completed_at = run.completed_at
        # This ticket does not own the reused Approval.  Leaving it in
        # ``awaiting_approval`` would create a false workflow state that can
        # never be converged by the winner's lifecycle.  The durable
        # TicketMessage.approval_id below is the read-model alias to the
        # canonical Approval; this duplicate turn itself is complete.
        ticket.status = "resolved"
        ticket.risk = "high"
        ticket.version += 1

        state = verified.state_delta.state or {}
        first_event = True
        for event in state.get("segment_events", []):
            try:
                await AgentRunStore(self.session).append_event(
                    run,
                    event_type=str(event["event_type"]),
                    payload=dict(event.get("payload", {})),
                    visibility=event.get("visibility", "internal"),
                    status=str(event.get("status", "completed")),
                    tool_call_id=event.get("tool_call_id"),
                    step_index=int(event.get("step_index", 0)),
                    tool_round=int(event.get("tool_round", 0)),
                    expected_ticket_head_event_id=(
                        verified.expected_heads.expected_ticket_head_event_id
                        if first_event
                        else ...
                    ),
                    expected_ticket_sequence=(
                        verified.expected_heads.expected_ticket_sequence if first_event else None
                    ),
                    expected_ticket_event_hash=(
                        verified.expected_heads.expected_ticket_event_hash if first_event else ...
                    ),
                )
            except CanonicalEventHeadConflict:
                await self._abort_finalizer(
                    lease,
                    marker=marker,
                    reason="finalizer_actual_head_conflict",
                    proposal=proposal,
                )
            first_event = False
        try:
            await AgentRunStore(self.session).append_event(
                run,
                event_type="approval_reused",
                payload={
                    "approval_id": active_approval.id,
                    "proposal_id": proposal.id,
                    "resource_type": active_approval.resource_type,
                    "resource_id": active_approval.resource_id,
                    "status": active_approval.status,
                },
                visibility="customer",
                expected_ticket_head_event_id=(
                    verified.expected_heads.expected_ticket_head_event_id if first_event else ...
                ),
                expected_ticket_sequence=(
                    verified.expected_heads.expected_ticket_sequence if first_event else None
                ),
                expected_ticket_event_hash=(
                    verified.expected_heads.expected_ticket_event_hash if first_event else ...
                ),
            )
        except CanonicalEventHeadConflict:
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="finalizer_actual_head_conflict",
                proposal=proposal,
            )
        await self._publish_message(
            ticket=ticket,
            run=run,
            kind="assistant",
            content=("同一资源已有一项正在处理的申请；我已复用该申请，没有创建第二项高风险操作。"),
            publication_key=f"assistant:{run.id}",
            approval_id=active_approval.id,
        )
        await self._publish_message(
            ticket=ticket,
            run=run,
            kind="action_update",
            content=("现有操作申请仍在处理中；本次请求未创建新的审批或执行新的业务动作。"),
            publication_key=f"approval:{active_approval.id}:reused:{run.id}",
            approval_id=active_approval.id,
        )
        await RuntimeJobRepository(self.session).finalize_control(
            lease,
            status="succeeded",
            outcome="completed",
        )
        await _activate_and_converge_application_fallback(
            self.session,
            ticket=ticket,
            trace_id=f"turn-dispatch:{run.id}",
            default_status="resolved",
        )
        await self.session.flush()
        return active_approval

    async def _bind_resume_canonical_head(
        self,
        marker: CheckpointCommitMarker,
        *,
        state: dict[str, Any],
        approval_id: str | None,
    ) -> None:
        resolved_approval_id = approval_id or str(
            state.get("human_decision", {}).get("approval_id", "")
        )
        run = await self.session.get(AgentRun, marker.run_id)
        approval = (
            await self.session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == resolved_approval_id,
                    ApprovalRequest.tenant_id == marker.tenant_id,
                    ApprovalRequest.run_id == marker.run_id,
                    ApprovalRequest.ticket_id == run.ticket_id,
                )
            )
            if run is not None
            else None
        )
        decision = await self.session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == resolved_approval_id)
        )
        if approval is None or decision is None or run is None:
            raise RuntimeConflict("approval_resume_binding_missing")
        ticket = await self.session.get(SupportTicket, run.ticket_id, with_for_update=True)
        head = await self.session.scalar(
            select(AgentEvent)
            .where(AgentEvent.ticket_id == run.ticket_id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        if ticket is None or head is None or ticket.next_event_sequence != head.ticket_sequence:
            raise RuntimeConflict("approval_resume_event_head_missing")
        execution = dict(state.get("execution_result", {}))
        expected_event_id: str | None
        expected_event_hash: str | None
        if decision.decision in {"reject", "manual_takeover"}:
            expected_event_id = decision.canonical_event_id
            expected_event_hash = decision.canonical_event_hash
        else:
            if execution.get("business_action_id") or execution.get("status") in {
                "succeeded",
                "stale",
            }:
                # The Graph may only prepare an execution intent.  Accepting a
                # pre-existing effect here would split the Business Resource
                # mutation from the fenced Segment finalizer transaction.
                raise RuntimeConflict("preexecuted_approval_resume_not_allowed")
            if execution.get("status") not in {
                "execution_pending",
                "execution_precondition_failed",
                "approved",
            }:
                raise RuntimeConflict("approval_resume_execution_intent_missing")
            # Before the finalizer executes the Runtime-only intent, the
            # accepted HumanDecision must still be the canonical ticket head.
            expected_event_id = decision.canonical_event_id
            expected_event_hash = decision.canonical_event_hash
        if (
            not expected_event_id
            or not expected_event_hash
            or head.id != expected_event_id
            or head.event_hash != expected_event_hash
        ):
            raise RuntimeConflict("approval_resume_actual_head_conflict")
        marker.expected_ticket_head_event_id = head.id
        marker.expected_ticket_sequence = head.ticket_sequence
        marker.expected_ticket_event_hash = head.event_hash

    async def _build_payload_v2(
        self,
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        checkpoint_id: str,
        checkpoint_hash: str,
        outcome: str,
        state: dict[str, Any],
        proposal_id: str | None,
        approval_id: str | None,
        legacy_action_delta: ActionfulApprovalResumeDelta | None = None,
    ) -> FinalizerPayloadV2:
        job = await RuntimeJobRepository(self.session).assert_fence(lease)
        run = await self.session.get(AgentRun, lease.run_id)
        if run is None:
            raise RuntimeConflict("run_not_found")
        ticket = await self.session.get(SupportTicket, run.ticket_id)
        if ticket is None:
            raise RuntimeConflict("ticket_not_found")
        persisted_terminal = _validated_finalizer_terminal(
            segment_kind=marker.segment_kind,
            outcome=outcome,
            state=state,
        )
        invocations = (
            await self.session.scalars(
                select(ToolInvocation)
                .where(ToolInvocation.segment_id == marker.id)
                .order_by(ToolInvocation.created_at, ToolInvocation.id)
            )
        ).all()
        invocation_ids = [item.id for item in invocations]
        observations = (
            await self.session.scalars(
                select(ToolObservation)
                .where(ToolObservation.invocation_id.in_(invocation_ids or ["<none>"]))
                .order_by(ToolObservation.created_at, ToolObservation.id)
            )
        ).all()
        attempts = (
            await self.session.scalars(
                select(AgentCallAttempt)
                .where(
                    AgentCallAttempt.job_id == lease.job_id,
                    AgentCallAttempt.fencing_token == lease.fencing_token,
                )
                .order_by(AgentCallAttempt.created_at, AgentCallAttempt.id)
            )
        ).all()
        contexts = (
            await self.session.scalars(
                select(ContextLedger)
                .where(
                    ContextLedger.job_id == lease.job_id,
                    ContextLedger.run_id == lease.run_id,
                )
                .order_by(ContextLedger.created_at, ContextLedger.id)
            )
        ).all()
        proposals = (
            await self.session.scalars(
                select(ProposalRecord)
                .where(ProposalRecord.run_id == lease.run_id)
                .order_by(ProposalRecord.created_at, ProposalRecord.id)
            )
        ).all()
        capability_invocations = (
            await self.session.scalars(
                select(PolicyCapabilityInvocation)
                .where(
                    PolicyCapabilityInvocation.run_id == lease.run_id,
                    PolicyCapabilityInvocation.job_id == lease.job_id,
                )
                .order_by(
                    PolicyCapabilityInvocation.created_at,
                    PolicyCapabilityInvocation.id,
                )
            )
        ).all()
        capability_ids = [item.id for item in capability_invocations]
        capability_attempts = (
            await self.session.scalars(
                select(PolicyCapabilityAttempt)
                .where(PolicyCapabilityAttempt.invocation_id.in_(capability_ids or ["<none>"]))
                .order_by(PolicyCapabilityAttempt.created_at, PolicyCapabilityAttempt.id)
            )
        ).all()
        capability_results = (
            await self.session.scalars(
                select(PolicyCapabilityResult)
                .where(PolicyCapabilityResult.invocation_id.in_(capability_ids or ["<none>"]))
                .order_by(PolicyCapabilityResult.created_at, PolicyCapabilityResult.id)
            )
        ).all()
        transports = (
            await self.session.scalars(
                select(ToolTransportAttempt)
                .where(ToolTransportAttempt.invocation_id.in_(invocation_ids or ["<none>"]))
                .order_by(ToolTransportAttempt.created_at, ToolTransportAttempt.id)
            )
        ).all()
        turns = (
            await self.session.scalars(
                select(TurnGroup)
                .where(
                    TurnGroup.run_id == lease.run_id,
                    TurnGroup.job_id == lease.job_id,
                )
                .order_by(TurnGroup.decision_ordinal, TurnGroup.id)
            )
        ).all()
        tool_head = canonical_hash(
            {
                "invocations": [
                    {
                        "id": item.id,
                        "lifecycle": item.lifecycle,
                        "outcome": item.outcome,
                        "arguments_hash": item.arguments_hash,
                    }
                    for item in invocations
                ],
                "observations": [
                    {
                        "id": item.id,
                        "invocation_id": item.invocation_id,
                        "status": item.status,
                        "content_hash": item.content_hash,
                    }
                    for item in observations
                ],
                "transports": [
                    {
                        "id": item.id,
                        "invocation_id": item.invocation_id,
                        "attempt_id": item.agent_call_attempt_id,
                        "ordinal": item.transport_ordinal,
                        "status": item.status,
                    }
                    for item in transports
                ],
            }
        )
        capability_head = canonical_hash(
            {
                "invocations": [
                    {
                        "id": item.id,
                        "name": item.capability_name,
                        "sequence": item.sequence,
                        "status": item.status,
                        "effect_identity": item.effect_identity,
                        "decision_hash": item.causal_decision_hash,
                        "observation_binding_hash": item.observation_binding_hash,
                    }
                    for item in capability_invocations
                ],
                "attempts": [
                    {
                        "id": item.id,
                        "invocation_id": item.invocation_id,
                        "ordinal": item.ordinal,
                        "status": item.status,
                    }
                    for item in capability_attempts
                ],
                "results": [
                    {
                        "id": item.id,
                        "invocation_id": item.invocation_id,
                        "status": item.status,
                        "effect_identity": item.effect_identity,
                        "payload_hash": item.payload_hash,
                    }
                    for item in capability_results
                ],
            }
        )
        proposal_head = canonical_hash(
            [
                {
                    "id": item.id,
                    "identity": item.proposal_identity,
                    "status": item.status,
                    "action_hash": item.action_hash,
                    "observation_binding_hash": canonical_hash(item.observation_binding),
                }
                for item in proposals
            ]
        )
        budget_head = canonical_hash(
            {
                "run": {
                    "llm_calls": run.llm_calls,
                    "tool_rounds": run.tool_rounds,
                    "tool_attempts": run.tool_attempts,
                },
                "turns": [
                    {
                        "id": item.id,
                        "decision_ordinal": item.decision_ordinal,
                        "tool_round": item.tool_round,
                        "status": item.status,
                    }
                    for item in turns
                ],
                "attempts": [
                    {
                        "id": item.id,
                        "kind": item.call_kind,
                        "ordinal": item.ordinal,
                        "status": item.status,
                    }
                    for item in attempts
                ],
            }
        )
        context_ledger_hash = canonical_hash(
            [
                {
                    "id": item.id,
                    "attempt_id": item.provider_attempt_id,
                    "request_hash": item.canonical_request_hash,
                }
                for item in contexts
            ]
        )
        resource_versions: dict[str, int] = {
            f"ticket:{ticket.id}": ticket.version,
            f"run:{run.id}": run.status_version,
            f"job:{job.id}": job.status_version,
        }
        domain_delta: Any
        if marker.segment_kind == "agent_start" and outcome == "completed":
            if state.get("agent_finish_reason") == "proposed":
                raise RuntimeConflict("proposal_not_durable")
            domain_delta = AgentCompleteDelta()
        elif marker.segment_kind == "agent_start" and outcome == "interrupted":
            proposal = await self.session.get(ProposalRecord, proposal_id or "")
            if proposal is None or proposal.run_id != run.id:
                raise RuntimeConflict("finalizer_proposal_missing")
            resource_versions[f"proposal:{proposal.id}"] = proposal.resource_version
            proposal_body = {
                "id": proposal.id,
                "action_type": proposal.action_type,
                "resource_id": proposal.resource_id,
                "resource_version": proposal.resource_version,
                "action_hash": proposal.action_hash,
            }
            domain_delta = HitlInterruptDelta(
                proposal_id=proposal.id,
                proposal_hash=canonical_hash(proposal_body),
                approval_snapshot_hash=canonical_hash(
                    {
                        "action_payload": proposal.action_payload,
                        "resource_version": proposal.resource_version,
                    }
                ),
                observation_binding_hash=canonical_hash(proposal.observation_binding),
            )
        elif marker.segment_kind == "approval_resume" and outcome == "completed":
            resolved_approval_id = approval_id or str(
                state.get("human_decision", {}).get("approval_id", "")
            )
            approval = await self.session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == resolved_approval_id,
                    ApprovalRequest.tenant_id == lease.tenant_id,
                    ApprovalRequest.run_id == lease.run_id,
                    ApprovalRequest.ticket_id == lease.ticket_id,
                )
            )
            decision = await self.session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == resolved_approval_id)
            )
            if approval is None or decision is None:
                raise RuntimeConflict("approval_resume_binding_missing")
            resource_versions[f"approval:{approval.id}"] = approval.business_version
            decision_name = str(decision.decision)
            execution = dict(state.get("execution_result", {}))
            if legacy_action_delta is not None:
                action = await self.session.get(
                    BusinessAction,
                    legacy_action_delta.business_action_id,
                )
                if (
                    legacy_action_delta.approval_id != approval.id
                    or legacy_action_delta.human_decision_id != decision.id
                    or legacy_action_delta.decision != decision_name
                    or legacy_action_delta.action_hash != approval.action_hash
                    or action is None
                    or action.approval_id != approval.id
                    or action.status != "succeeded"
                    or canonical_hash(action.result) != legacy_action_delta.effect_hash
                ):
                    raise RuntimeConflict("legacy_business_action_binding_missing")
                domain_delta = legacy_action_delta
            elif decision_name in {"reject", "manual_takeover"}:
                domain_delta = NoActionApprovalResumeDelta(
                    approval_id=approval.id,
                    human_decision_id=decision.id,
                    decision=cast(Any, decision_name),
                    no_action_reason=decision.reason,
                )
            elif execution.get("status") == "execution_pending":
                if (
                    execution.get("execution_intent") != "execute_runtime_action"
                    or execution.get("action_hash") != approval.action_hash
                    or execution.get("expected_approval_status") != approval.status
                    or approval.status not in {"approved", "executed"}
                ):
                    raise RuntimeConflict("approval_resume_execution_intent_invalid")
                domain_delta = ActionIntentApprovalResumeDelta(
                    approval_id=approval.id,
                    human_decision_id=decision.id,
                    decision=cast(Any, decision_name),
                    action_hash=approval.action_hash,
                    expected_approval_status=cast(Any, approval.status),
                )
            else:
                if execution.get("business_action_id") or execution.get("status") in {
                    "succeeded",
                    "stale",
                }:
                    raise RuntimeConflict("preexecuted_approval_resume_not_allowed")
                domain_delta = FailClosedApprovalResumeDelta(
                    approval_id=approval.id,
                    human_decision_id=decision.id,
                    decision=cast(Any, decision_name),
                    domain_outcome_reason=(
                        "binding_stale"
                        if execution.get("reason") == "publication_binding_stale"
                        else "logical_degradation"
                    ),
                    validation_result=str(execution.get("status", "missing_effect")),
                )
        else:
            raise RuntimeConflict("finalizer_variant_not_supported")
        if persisted_terminal == "manual_takeover" and not (
            isinstance(domain_delta, NoActionApprovalResumeDelta)
            and domain_delta.decision == "manual_takeover"
        ):
            raise RuntimeConflict("segment_manual_takeover_binding_invalid")
        heads = FinalizerHeadsV2(
            expected_ticket_head_event_id=marker.expected_ticket_head_event_id,
            expected_ticket_sequence=marker.expected_ticket_sequence,
            expected_ticket_event_hash=marker.expected_ticket_event_hash,
            expected_run_status=marker.expected_run_status,
            expected_run_status_version=marker.expected_run_version,
            parent_checkpoint_id=marker.canonical_parent_id,
            parent_checkpoint_hash=marker.canonical_parent_hash,
            parent_checkpoint_version=marker.parent_checkpoint_version,
            final_checkpoint_id=checkpoint_id,
            final_checkpoint_hash=checkpoint_hash,
            final_checkpoint_version=int(marker.final_checkpoint_version or 0),
            expected_marker_status_version=marker.status_version,
            expected_tool_ledger_head=tool_head,
            expected_capability_ledger_head=capability_head,
            expected_proposal_ledger_head=proposal_head,
            expected_budget_ledger_head=budget_head,
            expected_context_snapshot_hash=canonical_hash(state),
            expected_context_ledger_hash=context_ledger_hash,
            expected_domain_resource_versions=resource_versions,
        )
        return FinalizerPayloadV2.build(
            tenant_id=lease.tenant_id,
            ticket_id=ticket.id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=marker.id,
            delivery_generation=marker.delivery_generation,
            fencing_token=lease.fencing_token,
            parent_segment_id=marker.canonical_parent_id,
            marker_id=marker.id,
            segment_kind=cast(Any, marker.segment_kind),
            prepared_payload_hash=marker.prepared_payload_hash,
            expected_heads=heads,
            state=state,
            domain_delta=domain_delta,
        )

    async def finalize_interrupt(
        self,
        lease: JobLease,
        *,
        marker_id: str,
        proposal_id: str,
        test_capability: TestRuntimeCapability | None = None,
    ) -> ApprovalRequest:
        await set_local_scope(
            self.session,
            tenant_id=lease.tenant_id,
            principal_id=lease.owner,
            principal_role="system_worker",
        )
        marker_probe = await self.session.get(CheckpointCommitMarker, marker_id)
        if marker_probe is None:
            raise RuntimeConflict("interrupt_not_finalizable")
        marker_snapshot = self._marker_lock_snapshot(marker_probe)
        ticket, proposal, run, active_approval = await self._lock_interrupt_finalize_domain(
            lease,
            proposal_id=proposal_id,
        )
        marker = await self._lock_marker_after_domain(
            marker_id,
            expected_snapshot=marker_snapshot,
        )
        if (
            marker.status != "checkpoint_written"
            or marker.segment_outcome != "interrupted"
            or marker.tenant_id != lease.tenant_id
            or marker.run_id != lease.run_id
            or marker.job_id != lease.job_id
            or marker.fencing_token != lease.fencing_token
            or proposal.status != "draft"
            or proposal.run_id != run.id
        ):
            raise RuntimeConflict("interrupt_not_finalizable")
        payload = await self.session.scalar(
            select(FinalizerPayload).where(FinalizerPayload.marker_id == marker.id)
        )
        if payload is None or payload.fencing_token != lease.fencing_token:
            raise RuntimeConflict("finalizer_payload_missing")
        try:
            verified = FinalizerPayloadV2.model_validate(payload.full_payload)
            verified.verify()
        except ValueError as exc:
            try:
                await self._abort_finalizer(
                    lease,
                    marker=marker,
                    reason="finalizer_payload_hash_mismatch",
                    proposal=proposal,
                )
            except RuntimeConflict as conflict:
                raise conflict from exc
        if (
            not isinstance(verified.domain_delta, HitlInterruptDelta)
            or verified.domain_delta.proposal_id != proposal_id
        ):
            raise RuntimeConflict("finalizer_proposal_mismatch")
        rebuilt = await self._build_payload_v2(
            lease,
            marker=marker,
            checkpoint_id=str(marker.final_checkpoint_id or ""),
            checkpoint_hash=str(marker.final_checkpoint_hash or ""),
            outcome="interrupted",
            state=verified.state_delta.state,
            proposal_id=proposal_id,
            approval_id=None,
        )
        if rebuilt.payload_hash != verified.payload_hash:
            self._log_finalizer_payload_mismatch(
                lease,
                marker=marker,
                persisted=verified,
                rebuilt=rebuilt,
            )
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="finalizer_head_conflict",
                proposal=proposal,
            )
        if not verified.state_delta.state:
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="finalizer_empty_state_delta",
                proposal=proposal,
            )
        state = verified.state_delta.state or None
        if (
            state is not None
            and isinstance(state.get("final"), dict)
            and bool(state["final"].get("material_claims"))
        ):
            try:
                await CitationPublicationValidator(self.session).validate(
                    run_id=run.id, state=state
                )
            except CitationPublicationConflict as exc:
                await self._abort_finalizer(
                    lease,
                    marker=marker,
                    reason=str(exc),
                    proposal=proposal,
                )
        if (
            run.status_version != marker.expected_run_version
            or run.status != marker.expected_run_status
            or run.canonical_checkpoint_id != marker.canonical_parent_id
            or run.canonical_checkpoint_hash != marker.canonical_parent_hash
            or run.canonical_checkpoint_version != marker.parent_checkpoint_version
            or not marker.final_checkpoint_id
            or not marker.final_checkpoint_hash
            or marker.final_checkpoint_version != marker.parent_checkpoint_version + 1
        ):
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="canonical_parent_conflict",
                proposal=proposal,
            )
        try:
            await AgentRunStore(self.session).assert_ticket_head(
                run,
                expected_ticket_head_event_id=(
                    verified.expected_heads.expected_ticket_head_event_id
                ),
                expected_ticket_sequence=verified.expected_heads.expected_ticket_sequence,
                expected_ticket_event_hash=(verified.expected_heads.expected_ticket_event_hash),
            )
        except CanonicalEventHeadConflict:
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="finalizer_actual_head_conflict",
                proposal=proposal,
            )
        identity = canonical_approval_identity(
            proposal=proposal,
            run=run,
            customer_id=run.customer_id,
        )
        if active_approval is not None:
            return await self._finalize_reused_approval(
                lease,
                marker=marker,
                run=run,
                proposal=proposal,
                ticket=ticket,
                active_approval=active_approval,
                verified=verified,
            )
        approval = ApprovalRequest(
            id=new_id("approval"),
            tenant_id=lease.tenant_id,
            ticket_id=ticket.id,
            customer_id=run.customer_id,
            proposal_id=proposal.id,
            run_id=run.id,
            checkpoint_id=marker.final_checkpoint_id,
            canonical_checkpoint_ns=marker.private_namespace,
            canonical_checkpoint_hash=marker.final_checkpoint_hash,
            checkpoint_version=int(marker.final_checkpoint_version),
            marker_id=marker.id,
            action_type=proposal.action_type,
            resource_type=identity.resource_type,
            resource_id=identity.resource_id,
            origin_turn_id=identity.origin_turn_id,
            action_payload=proposal.action_payload,
            review_context={
                "original_ticket": state.get("user_message") if state else None,
                "redacted_ticket": state.get("redacted_message") if state else None,
                "evidence": state.get("evidence", []) if state else [],
                "tool_observations": state.get("tool_observations", []) if state else [],
                "observation_binding": proposal.observation_binding,
                "risk": state.get("classification", {}).get("risk", "high") if state else "high",
                "policy_route": state.get("policy_route") if state else None,
            },
            action_hash=proposal.action_hash,
            business_version=proposal.resource_version,
            status="pending",
            idempotency_key=f"approval:{proposal.id}",
        )
        snapshot_id = new_id("snapshot")
        revision_id = new_id("revision")
        citation_binding_refs = sorted(
            {
                str(binding_id)
                for binding in proposal.observation_binding
                for binding_id in binding.get("citation_binding_ids", [])
            }
        )
        if not citation_binding_refs and test_capability is not None:
            policy_binding: dict[str, Any] = {
                "schema_version": "test-only-unbound-policy.v1",
                "citation_lineage": [],
            }
            citation_lineage: list[dict[str, Any]] = []
        else:
            policy_binding = {}
            citation_lineage = []
        citation_rows = (
            await self.session.execute(
                select(CitationBinding, RetrievalTrace)
                .join(RetrievalTrace, RetrievalTrace.id == CitationBinding.retrieval_trace_id)
                .where(
                    CitationBinding.tenant_id == lease.tenant_id,
                    CitationBinding.run_id == run.id,
                    CitationBinding.id.in_(citation_binding_refs or ["<none>"]),
                    RetrievalTrace.trace_status == "terminal_ok",
                )
            )
        ).all()
        if test_capability is None and (
            not citation_binding_refs
            or {binding.id for binding, _trace in citation_rows} != set(citation_binding_refs)
        ):
            raise RuntimeConflict("approval_citation_lineage_missing")
        if citation_binding_refs:
            citation_lineage = [
                {
                    "citation_binding_id": binding.id,
                    "provider_attempt_id": binding.provider_attempt_id,
                    "context_ledger_id": binding.context_ledger_id,
                    "origin_job_id": binding.origin_job_id,
                    "tool_invocation_id": binding.tool_invocation_id,
                    "observation_id": binding.observation_id,
                    "retrieval_trace_id": trace.id,
                    "selected_candidate_ordinal": binding.selected_candidate_ordinal,
                    "locator_hash": binding.locator_hash,
                    "temporal_selector": binding.temporal_selector,
                    "trace_logical_time": (
                        trace.trace_logical_time.isoformat()
                        if trace.trace_logical_time is not None
                        else None
                    ),
                    "filter_contract_hash": stable_hash(trace.filter_contract),
                    "evidence_groups_hash": stable_hash({"groups": trace.evidence_groups}),
                    "pipeline_fingerprint": trace.pipeline_fingerprint,
                    "corpus_snapshot_id": trace.corpus_snapshot_id,
                    "index_version": trace.index_version,
                }
                for binding, trace in sorted(citation_rows, key=lambda row: row[0].id)
            ]
        expected_capability = {
            "refund": "propose_refund",
            "api_key_revocation": "propose_api_key_revocation",
            "entitlement_change": "propose_entitlement_change",
        }.get(proposal.action_type)
        capability_rows = (
            await self.session.execute(
                select(PolicyCapabilityInvocation, PolicyCapabilityResult)
                .join(
                    PolicyCapabilityResult,
                    PolicyCapabilityResult.invocation_id == PolicyCapabilityInvocation.id,
                )
                .where(
                    PolicyCapabilityInvocation.tenant_id == lease.tenant_id,
                    PolicyCapabilityInvocation.run_id == run.id,
                    PolicyCapabilityInvocation.job_id == lease.job_id,
                    PolicyCapabilityInvocation.segment_id == marker.id,
                    PolicyCapabilityInvocation.fencing_token == lease.fencing_token,
                    PolicyCapabilityInvocation.capability_name == expected_capability,
                    PolicyCapabilityInvocation.status == "succeeded",
                    PolicyCapabilityResult.tenant_id == lease.tenant_id,
                    PolicyCapabilityResult.run_id == run.id,
                    PolicyCapabilityResult.job_id == lease.job_id,
                    PolicyCapabilityResult.status == "succeeded",
                )
            )
        ).all()
        proposal_capabilities = [
            (invocation, result)
            for invocation, result in capability_rows
            if result.payload.get("proposal_id") == proposal.id
        ]
        if len(proposal_capabilities) != 1 and test_capability is None:
            raise RuntimeConflict("approval_policy_binding_missing")
        if proposal_capabilities:
            policy_invocation, policy_result = proposal_capabilities[0]
            policy_binding = {
                "schema_version": "deterministic-policy-binding.v1",
                "policy_version": "supportguard-policy-gate.v1",
                "capability_invocation_id": policy_invocation.id,
                "capability_name": policy_invocation.capability_name,
                "causal_decision_hash": policy_invocation.causal_decision_hash,
                "observation_binding_hash": policy_invocation.observation_binding_hash,
                "effect_identity": policy_invocation.effect_identity,
                "result_payload_hash": policy_result.payload_hash,
                "citation_lineage": citation_lineage,
            }
        snapshot_payload = approval_snapshot_payload(
            approval_id=approval.id,
            proposal_id=proposal.id,
            tenant_id=lease.tenant_id,
            run_id=run.id,
            origin_job_id=marker.job_id,
            origin_marker_id=marker.id,
            origin_fencing_token=marker.fencing_token,
            action_type=proposal.action_type,
            action_payload=dict(proposal.action_payload),
            action_hash=proposal.action_hash,
            resource_version=proposal.resource_version,
            policy_binding=policy_binding,
            citation_binding_refs=citation_binding_refs,
        )
        snapshot = ApprovalSnapshot(
            id=snapshot_id,
            tenant_id=lease.tenant_id,
            ticket_id=ticket.id,
            customer_id=run.customer_id,
            run_id=run.id,
            approval_id=approval.id,
            proposal_id=proposal.id,
            origin_job_id=marker.job_id,
            origin_marker_id=marker.id,
            origin_fencing_token=marker.fencing_token,
            origin_segment_ref=marker.id,
            action_type=proposal.action_type,
            action_payload=dict(proposal.action_payload),
            action_hash=proposal.action_hash,
            resource_version=proposal.resource_version,
            policy_binding=policy_binding,
            citation_binding_refs=citation_binding_refs,
            snapshot_hash=approval_snapshot_hash(snapshot_payload),
        )
        revision = ApprovalActionRevision(
            id=revision_id,
            tenant_id=lease.tenant_id,
            approval_id=approval.id,
            proposal_id=proposal.id,
            snapshot_id=snapshot.id,
            revision_number=0,
            action_payload=dict(proposal.action_payload),
            action_hash=proposal.action_hash,
            resource_version=proposal.resource_version,
            created_by_ref="interrupt_finalizer",
            revision_reason="base_interrupt",
        )
        approval.selected_revision_id = revision.id
        approval.selected_revision_number = 0
        claim_savepoint = (
            await self.session.begin_nested()
            if self.session.get_bind().dialect.name == "postgresql"
            else None
        )
        proposal.status = "bound"
        marker.status = "finalized"
        marker.status_version += 1
        run.canonical_checkpoint_ns = marker.private_namespace
        run.canonical_checkpoint_id = marker.final_checkpoint_id
        run.canonical_checkpoint_hash = marker.final_checkpoint_hash
        run.canonical_checkpoint_version = int(marker.final_checkpoint_version)
        run.status = "interrupted"
        run.checkpoint_stage = "awaiting_approval"
        run.checkpoint_id = marker.final_checkpoint_id
        run.active_job_id = None
        run.active_fencing_token = None
        run.status_version += 1
        turn = (
            await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
            if run.turn_id
            else None
        )
        if turn is not None:
            turn.activity_state = "waiting_external"
            turn.result_state = "proposal_created"
        ticket.status = "awaiting_approval"
        ticket.risk = "high"
        ticket.version += 1
        first_event = True
        if state is not None:
            for event in state.get("segment_events", []):
                try:
                    await AgentRunStore(self.session).append_event(
                        run,
                        event_type=str(event["event_type"]),
                        payload=dict(event.get("payload", {})),
                        visibility=event.get("visibility", "internal"),
                        status=str(event.get("status", "completed")),
                        tool_call_id=event.get("tool_call_id"),
                        step_index=int(event.get("step_index", 0)),
                        tool_round=int(event.get("tool_round", 0)),
                        expected_ticket_head_event_id=(
                            verified.expected_heads.expected_ticket_head_event_id
                            if first_event
                            else ...
                        ),
                        expected_ticket_sequence=(
                            verified.expected_heads.expected_ticket_sequence
                            if first_event
                            else None
                        ),
                        expected_ticket_event_hash=(
                            verified.expected_heads.expected_ticket_event_hash
                            if first_event
                            else ...
                        ),
                    )
                except CanonicalEventHeadConflict:
                    await self._abort_finalizer(
                        lease,
                        marker=marker,
                        reason="finalizer_actual_head_conflict",
                        proposal=proposal,
                    )
                first_event = False
        try:
            interrupt_event = await AgentRunStore(self.session).append_event(
                run,
                event_type="approval_interrupted",
                payload={"approval_id": approval.id, "proposal_id": proposal.id},
                visibility="customer",
                expected_ticket_head_event_id=(
                    verified.expected_heads.expected_ticket_head_event_id if first_event else ...
                ),
                expected_ticket_sequence=(
                    verified.expected_heads.expected_ticket_sequence if first_event else None
                ),
                expected_ticket_event_hash=(
                    verified.expected_heads.expected_ticket_event_hash if first_event else ...
                ),
            )
        except CanonicalEventHeadConflict:
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="finalizer_actual_head_conflict",
                proposal=proposal,
            )
        approval.review_context = {
            **approval.review_context,
            "expected_ticket_head_event_id": interrupt_event.id,
            "expected_ticket_sequence": interrupt_event.ticket_sequence,
            "expected_ticket_event_hash": interrupt_event.event_hash,
        }
        approval.expected_ticket_head_event_id = interrupt_event.id
        approval.expected_ticket_sequence = interrupt_event.ticket_sequence
        approval.expected_ticket_event_hash = interrupt_event.event_hash
        # The approval binding is immutable after INSERT. Build its canonical
        # event-head fields first, then persist the parent chain in FK order.
        if claim_savepoint is None:
            self.session.add(approval)
            await self.session.flush()
        else:
            inserted_id = await self._insert_active_approval(approval)
            if inserted_id is None:
                # Remove the losing provisional event/state delta without
                # aborting the outer Worker transaction, then publish a clean
                # reuse outcome against the canonical active Approval.
                await claim_savepoint.rollback()
                for entity in (marker, run, proposal, ticket):
                    await self.session.refresh(entity)
                active_approval = await ActionLifecycleService(self.session).find_active(
                    identity,
                    lock=False,
                )
                if active_approval is None:
                    raise RuntimeConflict("active_approval_race_unresolved")
                return await self._finalize_reused_approval(
                    lease,
                    marker=marker,
                    run=run,
                    proposal=proposal,
                    ticket=ticket,
                    active_approval=active_approval,
                    verified=verified,
                )
            persisted = await self.session.get(
                ApprovalRequest,
                inserted_id,
                with_for_update=True,
            )
            if persisted is None:
                raise RuntimeConflict("active_approval_insert_missing")
            approval = persisted
            await claim_savepoint.commit()
        answer = (
            "我已核验相关事实，并提交了一项需要独立人工审批的高风险操作。审批期间你仍可继续提问。"
        )
        if state and isinstance(state.get("final"), dict):
            answer = str(state["final"].get("answer") or answer)
        await self._publish_message(
            ticket=ticket,
            run=run,
            kind="assistant",
            content=answer,
            publication_key=f"assistant:{run.id}",
            approval_id=approval.id,
            source_refs=_final_message_source_refs(state) if state else [],
        )
        await self._publish_message(
            ticket=ticket,
            run=run,
            kind="action_proposal",
            content="高风险操作已提交人工审批；在审批完成前不会执行真实业务动作。",
            publication_key=f"approval:{approval.id}:proposal",
            approval_id=approval.id,
        )
        await RuntimeJobRepository(self.session).finalize_control(
            lease, status="succeeded", outcome="interrupted"
        )
        await _activate_and_converge_application_fallback(
            self.session,
            ticket=ticket,
            trace_id=f"turn-dispatch:{run.id}",
            default_status="awaiting_approval",
        )
        await self.session.flush()
        self.session.add(snapshot)
        await self.session.flush()
        self.session.add(revision)
        await self.session.flush()
        return approval
