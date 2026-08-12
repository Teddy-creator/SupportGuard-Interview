from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import AgentRunStore, CanonicalEventHeadConflict
from supportguard.agent.responses import render_executed_action_update
from supportguard.contracts.finalizer import (
    ActionfulApprovalResumeDelta,
    ActionIntentApprovalResumeDelta,
    AgentCompleteDelta,
    FailClosedApprovalResumeDelta,
    FinalizerPayloadV2,
    HitlInterruptDelta,
    NoActionApprovalResumeDelta,
    finalizer_payload_mismatch_paths,
)
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    BusinessAction,
    CheckpointCommitMarker,
    ConversationTurn,
    FinalizerPayload,
    ProposalRecord,
    SupportTicket,
)
from supportguard.memory.service import MemoryService
from supportguard.rag.citations import (
    CitationPublicationConflict,
    CitationPublicationValidator,
)
from supportguard.services.actions import RuntimeActionExecutor
from supportguard.services.runtime_jobs import (
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.segment_common import (
    _activate_and_converge_application_fallback,
    _final_message_source_refs,
    _restartable_pre_effect_head_paths,
    _validated_finalizer_terminal,
)
from supportguard.services.turn_results import turn_result_for

logger = logging.getLogger(__name__)


if TYPE_CHECKING:

    class _FinalizationDependencies:
        def _marker_lock_snapshot(self, marker: CheckpointCommitMarker) -> tuple[object, ...]:
            raise NotImplementedError

        async def _lock_marker_after_domain(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _abort_finalizer(self, *args: Any, **kwargs: Any) -> Never:
            raise NotImplementedError

        async def _restart_pre_effect_finalizer(self, *args: Any, **kwargs: Any) -> Never:
            raise NotImplementedError

        async def _fail_confirmed_zero_effect_finalizer(self, *args: Any, **kwargs: Any) -> Never:
            raise NotImplementedError

        async def _build_payload_v2(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def _log_finalizer_payload_mismatch(self, *args: Any, **kwargs: Any) -> None:
            raise NotImplementedError

        async def _publish_message(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

else:

    class _FinalizationDependencies:
        pass


class FinalizationSegments(_FinalizationDependencies):
    session: AsyncSession

    async def finalize(
        self,
        lease: JobLease,
        *,
        marker_id: str,
    ) -> CheckpointCommitMarker:
        """Finalize one Segment without ever committing a partial action effect.

        Before the Runtime-only action boundary, existing fail-closed conflicts
        intentionally persist an aborted aggregate.  Once action execution is
        entered, however, the business resource, BusinessAction, Approval,
        canonical event/publication, memory and job outcome are one atomic
        unit.  Any exception from that boundary onward therefore rolls back the
        root transaction before control is returned to a caller.
        """

        post_effect_guard = (
            "segment-finalizer-post-effect",
            marker_id,
            lease.job_id,
            lease.fencing_token,
            id(self),
        )
        self.session.info[post_effect_guard] = False
        rollback_boundary = self.session.get_nested_transaction()
        try:
            return await self._finalize_in_transaction(
                lease,
                marker_id=marker_id,
                post_effect_guard=post_effect_guard,
            )
        except BaseException:
            if bool(self.session.info.get(post_effect_guard)):
                # Do not depend on every caller remembering to rollback after
                # catching a finalizer error.  In particular, a canonical
                # publication conflict must not reach the fail-closed commit
                # path after the owner action capability has mutated state.
                if rollback_boundary is not None:
                    await rollback_boundary.rollback()
                else:
                    await self.session.rollback()
            raise
        finally:
            self.session.info.pop(post_effect_guard, None)

    async def _finalize_in_transaction(
        self,
        lease: JobLease,
        *,
        marker_id: str,
        post_effect_guard: tuple[object, ...],
    ) -> CheckpointCommitMarker:
        # Marker identity is a lock-free discovery read.  The authoritative
        # Marker lock is acquired only after the operation's Ticket/domain lock
        # prefix, matching takeover and reconciler paths.
        marker_probe = await self.session.get(CheckpointCommitMarker, marker_id)
        run_probe = await self.session.get(AgentRun, lease.run_id)
        if marker_probe is None or run_probe is None or marker_probe.status != "checkpoint_written":
            raise RuntimeConflict("marker_not_finalizable")
        marker: CheckpointCommitMarker = marker_probe
        run: AgentRun = run_probe
        marker_snapshot = self._marker_lock_snapshot(marker)
        payload = await self.session.scalar(
            select(FinalizerPayload).where(FinalizerPayload.marker_id == marker.id)
        )
        if payload is None or payload.fencing_token != lease.fencing_token:
            raise RuntimeConflict("finalizer_payload_missing")
        verified: FinalizerPayloadV2 | None = None
        try:
            verified = FinalizerPayloadV2.model_validate(payload.full_payload)
            if isinstance(verified.domain_delta, ActionfulApprovalResumeDelta):
                # This compatibility variant proves an older worker already
                # committed the business effect.  Every subsequent validation,
                # publication, memory, and terminal update is therefore
                # post-effect and may only roll back to finalizer-only recovery.
                self.session.info[post_effect_guard] = True
            verified.verify()
        except ValueError as exc:
            try:
                if isinstance(
                    getattr(verified, "domain_delta", None),
                    ActionfulApprovalResumeDelta,
                ):
                    raise RuntimeConflict("finalizer_payload_hash_mismatch")
                if isinstance(
                    getattr(verified, "domain_delta", None),
                    ActionIntentApprovalResumeDelta,
                ):
                    await self._fail_confirmed_zero_effect_finalizer(
                        lease,
                        marker=marker,
                        reason="finalizer_payload_hash_mismatch",
                    )
                else:
                    await RuntimeJobRepository(self.session).assert_fence(lease)
                    marker = await self._lock_marker_after_domain(
                        marker_id,
                        expected_snapshot=marker_snapshot,
                    )
                    await self._abort_finalizer(
                        lease,
                        marker=marker,
                        reason="finalizer_payload_hash_mismatch",
                    )
            except RuntimeConflict as conflict:
                raise conflict from exc
        if verified is None:
            raise RuntimeConflict("finalizer_payload_unverified")
        actionful_intent = isinstance(
            verified.domain_delta,
            ActionIntentApprovalResumeDelta,
        )
        if not actionful_intent:
            await RuntimeJobRepository(self.session).assert_fence(lease)
            marker = await self._lock_marker_after_domain(
                marker_id,
                expected_snapshot=marker_snapshot,
            )
            await self.session.refresh(run)

        async def abort(reason: str) -> Never:
            nonlocal marker
            if bool(self.session.info.get(post_effect_guard)):
                # The public wrapper owns the root rollback.  Calling
                # _abort_finalizer here would explicitly commit the effect
                # together with Marker=aborted/Run=failed/Job=dead.
                raise RuntimeConflict(reason)
            if actionful_intent:
                await self._fail_confirmed_zero_effect_finalizer(
                    lease,
                    marker=marker,
                    reason=reason,
                )
            await self._abort_finalizer(lease, marker=marker, reason=reason)

        proposal_id = (
            verified.domain_delta.proposal_id
            if isinstance(verified.domain_delta, HitlInterruptDelta)
            else None
        )
        approval_id = getattr(verified.domain_delta, "approval_id", None)
        rebuilt = await self._build_payload_v2(
            lease,
            marker=marker,
            checkpoint_id=str(marker.final_checkpoint_id or ""),
            checkpoint_hash=str(marker.final_checkpoint_hash or ""),
            outcome=verified.domain_delta.outcome,
            state=verified.state_delta.state,
            proposal_id=proposal_id,
            approval_id=approval_id,
            legacy_action_delta=(
                verified.domain_delta
                if isinstance(verified.domain_delta, ActionfulApprovalResumeDelta)
                else None
            ),
        )
        if rebuilt.payload_hash != verified.payload_hash:
            mismatch_paths = finalizer_payload_mismatch_paths(verified, rebuilt)
            self._log_finalizer_payload_mismatch(
                lease,
                marker=marker,
                persisted=verified,
                rebuilt=rebuilt,
            )
            if isinstance(verified.domain_delta, AgentCompleteDelta) and (
                _restartable_pre_effect_head_paths(mismatch_paths)
            ):
                await self._restart_pre_effect_finalizer(
                    lease,
                    marker=marker,
                    run=run,
                    mismatch_paths=mismatch_paths,
                )
            await abort("finalizer_head_conflict")
        if not verified.state_delta.state:
            await abort("finalizer_empty_state_delta")
        if (
            verified.expected_heads.final_checkpoint_version
            <= verified.expected_heads.parent_checkpoint_version
        ):
            await abort("finalizer_checkpoint_version_conflict")
        state = verified.state_delta.state or None
        if (
            state is not None
            # Action publication was revalidated immediately before the atomic
            # effect.  Revalidating the pre-action scope snapshot after an
            # entitlement mutation would misclassify our own version advance
            # as external publication drift.
            and not isinstance(
                verified.domain_delta,
                (ActionfulApprovalResumeDelta, ActionIntentApprovalResumeDelta),
            )
            and isinstance(state.get("final"), dict)
            and bool(state["final"].get("material_claims"))
        ):
            try:
                await CitationPublicationValidator(self.session).validate(
                    run_id=run.id, state=state
                )
            except CitationPublicationConflict as exc:
                await abort(str(exc))
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
            await abort("canonical_parent_conflict")
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
            await abort("finalizer_actual_head_conflict")
        action_event_head_id: str | None = None
        action_event_head_hash: str | None = None
        if actionful_intent:
            action_delta = cast(ActionIntentApprovalResumeDelta, verified.domain_delta)
            if self.session.get_bind().dialect.name != "postgresql":
                # SQLite/test execution has no owner function, so acquire the
                # same domain locks explicitly before the executor reaches its
                # own fence check: Ticket -> Approval/Proposal -> Run/Turn.
                action_ticket = await self.session.get(
                    SupportTicket,
                    lease.ticket_id or run.ticket_id,
                    with_for_update=True,
                )
                action_approval = await self.session.get(
                    ApprovalRequest,
                    action_delta.approval_id,
                    with_for_update=True,
                )
                if action_ticket is None or action_approval is None:
                    raise RuntimeConflict("approval_resume_binding_missing")
                if action_approval.proposal_id:
                    await self.session.get(
                        ProposalRecord,
                        action_approval.proposal_id,
                        with_for_update=True,
                    )
                run = cast(
                    AgentRun,
                    await self.session.get(
                        AgentRun,
                        lease.run_id,
                        with_for_update=True,
                    ),
                )
                if run.turn_id:
                    await self.session.get(
                        ConversationTurn,
                        run.turn_id,
                        with_for_update=True,
                    )

            approval_for_action = await self.session.get(
                ApprovalRequest,
                action_delta.approval_id,
            )
            if approval_for_action is None:
                raise RuntimeConflict("approval_resume_binding_missing")
            self.session.info[post_effect_guard] = True
            action_result = await RuntimeActionExecutor(self.session).execute(
                lease,
                approval_id=action_delta.approval_id,
                trace_id=f"finalizer:{marker.id}",
            )
            marker = await self._lock_marker_after_domain(
                marker_id,
                expected_snapshot=marker_snapshot,
            )
            await self.session.refresh(run)
            await self.session.flush()
            await self.session.refresh(approval_for_action)
            state = copy.deepcopy(cast(dict[str, Any], state))
            state["execution_result"] = {
                "approval_id": approval_for_action.id,
                "business_action_id": action_result.business_action_id,
                "action_type": action_result.action_type,
                "resource_id": action_result.resource_id,
                "status": action_result.status,
                "reused": action_result.reused,
                "reason": action_result.reason,
            }
            final = state.get("final")
            if not isinstance(final, dict):
                raise RuntimeConflict("segment_final_state_missing")
            if action_result.status == "succeeded":
                action = await self.session.get(
                    BusinessAction,
                    action_result.business_action_id or "",
                )
                selected_revision = await self.session.get(
                    ApprovalActionRevision,
                    approval_for_action.selected_revision_id or "",
                )
                if (
                    action is None
                    or selected_revision is None
                    or selected_revision.tenant_id != approval_for_action.tenant_id
                    or selected_revision.approval_id != approval_for_action.id
                    or selected_revision.proposal_id != approval_for_action.proposal_id
                    or selected_revision.revision_number
                    != approval_for_action.selected_revision_number
                    or selected_revision.resource_version != approval_for_action.business_version
                    or action.tenant_id != lease.tenant_id
                    or action.ticket_id != lease.ticket_id
                    or action.customer_id != approval_for_action.customer_id
                    or action.approval_id != approval_for_action.id
                    or action.action_revision_id != selected_revision.id
                    or action.action_hash != selected_revision.action_hash
                    or action.action_type != action_result.action_type
                    or action.resource_id != action_result.resource_id
                    or action.status != "succeeded"
                    or not action.canonical_event_id
                    or not action.canonical_event_hash
                ):
                    raise RuntimeConflict("business_action_binding_missing")
                final["terminal_state"] = "resolved"
                final["answer"] = render_executed_action_update(
                    action.action_type,
                    resource_id=action_result.resource_id,
                    result=action.result,
                )
                state["agent_finish_reason"] = "executed"
                action_event_head_id = action.canonical_event_id
                action_event_head_hash = action.canonical_event_hash
            elif action_result.status == "stale":
                final["terminal_state"] = "failed"
                final["answer"] = (
                    "执行前复核发现相关资源或审批依据已经变化，因此没有执行任何业务变更。"
                    "请刷新当前信息并重新提交申请。"
                )
                final["knowledge_chunk_ids"] = []
                final["business_source_ids"] = []
                final["material_claims"] = []
                state["agent_finish_reason"] = "binding_stale"
                stale_head = await self.session.scalar(
                    select(AgentEvent)
                    .where(
                        AgentEvent.tenant_id == lease.tenant_id,
                        AgentEvent.ticket_id == lease.ticket_id,
                        AgentEvent.run_id == lease.run_id,
                        AgentEvent.event_type == "runtime_action_reconciliation",
                    )
                    .order_by(AgentEvent.ticket_sequence.desc())
                    .limit(1)
                )
                if stale_head is None:
                    raise RuntimeConflict("action_stale_event_binding_missing")
                action_event_head_id = stale_head.id
                action_event_head_hash = stale_head.event_hash
            else:
                raise RuntimeConflict("runtime_action_result_unknown")
        run.canonical_checkpoint_ns = marker.private_namespace
        run.canonical_checkpoint_id = marker.final_checkpoint_id
        run.canonical_checkpoint_hash = marker.final_checkpoint_hash
        run.canonical_checkpoint_version = int(cast(int, marker.final_checkpoint_version))
        verification_pending = (
            isinstance(verified.domain_delta, FailClosedApprovalResumeDelta)
            and verified.domain_delta.domain_outcome_reason == "logical_degradation"
            and (
                verified.domain_delta.validation_result == "approved"
                or (
                    state is not None
                    and state.get("execution_result", {}).get("execution_state")
                    == "verification_pending"
                )
            )
        )
        if state is not None and verification_pending:
            # A legacy Action result can be durably accepted while its external
            # effect remains unknown.  This is not a successful terminal:
            # preserve the Approval's active resource identity and the durable
            # checkpoint, but do not publish an action-update/final-outcome or
            # clear the conversation into a resolved state.
            ticket = await self.session.get(
                SupportTicket,
                run.ticket_id,
                with_for_update=True,
            )
            if ticket is None:
                raise RuntimeConflict("segment_domain_state_missing")
            ticket.status = "verification_pending"
            ticket.issue_type = str(state.get("classification", {}).get("issue_type", "unknown"))
            ticket.risk = str(state.get("classification", {}).get("risk", ticket.risk))
            ticket.version += 1
            run.status = "interrupted"
            run.checkpoint_stage = "verification_pending"
            run.checkpoint_id = marker.final_checkpoint_id
            run.agent_finish_reason = "verification_pending"
            run.error_code = None
            run.tool_rounds = max(run.tool_rounds, int(state.get("tool_rounds", 0)))
            run.tool_attempts = max(run.tool_attempts, int(state.get("tool_attempts", 0)))
            run.llm_calls = max(run.llm_calls, int(state.get("llm_calls", 0)))
            run.completed_at = None
            run.active_job_id = None
            run.active_fencing_token = None
            run.status_version += 1
            turn = (
                await self.session.get(
                    ConversationTurn,
                    run.turn_id,
                    with_for_update=True,
                )
                if run.turn_id
                else None
            )
            if turn is not None:
                turn.activity_state = "waiting_external"
                turn.result_state = turn.result_state or "proposal_created"
                turn.completed_at = None
            marker.status = "finalized"
            marker.status_version += 1
            await self.session.flush()
            await RuntimeJobRepository(self.session).finalize_control(
                lease,
                status="succeeded",
                outcome="verification_pending",
            )
            await _activate_and_converge_application_fallback(
                self.session,
                ticket=ticket,
                trace_id=f"turn-dispatch:{run.id}",
                default_status="verification_pending",
            )
            await self.session.flush()
            return marker
        if state is not None:
            final = state.get("final")
            if not isinstance(final, dict):
                raise RuntimeConflict("segment_final_state_missing")
            ticket = await self.session.get(SupportTicket, run.ticket_id, with_for_update=True)
            if ticket is None:
                raise RuntimeConflict("segment_domain_state_missing")
            terminal_value = _validated_finalizer_terminal(
                segment_kind=marker.segment_kind,
                outcome="completed",
                state=state,
            )
            if terminal_value is None:
                raise RuntimeConflict("segment_terminal_state_invalid")
            terminal: str = terminal_value
            if terminal == "manual_takeover" and not (
                isinstance(verified.domain_delta, NoActionApprovalResumeDelta)
                and verified.domain_delta.decision == "manual_takeover"
            ):
                raise RuntimeConflict("segment_manual_takeover_binding_invalid")
            ticket.status = terminal
            ticket.issue_type = str(state.get("classification", {}).get("issue_type", "unknown"))
            ticket.risk = str(state.get("classification", {}).get("risk", ticket.risk))
            ticket.final_response = str(final.get("answer", ""))
            ticket.version += 1
            run.status = "completed"
            run.checkpoint_stage = "completed"
            run.checkpoint_id = marker.final_checkpoint_id
            run.agent_finish_reason = str(state.get("agent_finish_reason", "answered"))
            run.error_code = str(state.get("safe_stop_error_code") or "") or None
            run.tool_rounds = max(run.tool_rounds, int(state.get("tool_rounds", 0)))
            run.tool_attempts = max(run.tool_attempts, int(state.get("tool_attempts", 0)))
            run.llm_calls = max(run.llm_calls, int(state.get("llm_calls", 0)))
            run.completed_at = datetime.now(UTC)
            run.status_version += 1
            human_action = str(state.get("human_decision", {}).get("action", ""))
            if marker.segment_kind == "approval_resume" and human_action == "manual_takeover":
                ticket.automation_mode = "human_queue"
                run.agent_finish_reason = "manual_takeover"
            turn = (
                await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
                if run.turn_id
                else None
            )
            if turn is not None:
                turn.activity_state = "completed"
                turn.result_state = turn_result_for(
                    run.agent_finish_reason,
                    terminal_state=terminal,
                    automation_mode=ticket.automation_mode,
                )
                turn.completed_at = run.completed_at
            if marker.segment_kind == "agent_start":
                await self._publish_message(
                    ticket=ticket,
                    run=run,
                    kind="assistant",
                    content=ticket.final_response,
                    publication_key=f"assistant:{run.id}",
                    source_refs=_final_message_source_refs(state),
                )
            elif marker.segment_kind == "approval_resume" and approval_id:
                await self._publish_message(
                    ticket=ticket,
                    run=run,
                    kind=(
                        "human_queue_update"
                        if human_action == "manual_takeover"
                        else "action_update"
                    ),
                    content=ticket.final_response,
                    publication_key=f"approval:{approval_id}:resume",
                    approval_id=str(approval_id),
                )
            events = list(state.get("segment_events", []))
            first_event = True
            for event in events:
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
                            (
                                action_event_head_id
                                or verified.expected_heads.expected_ticket_head_event_id
                            )
                            if first_event
                            else ...
                        ),
                        expected_ticket_sequence=(
                            (
                                None
                                if action_event_head_id
                                else verified.expected_heads.expected_ticket_sequence
                            )
                            if first_event
                            else None
                        ),
                        expected_ticket_event_hash=(
                            (
                                action_event_head_hash
                                or verified.expected_heads.expected_ticket_event_hash
                            )
                            if first_event
                            else ...
                        ),
                    )
                except CanonicalEventHeadConflict:
                    await abort("finalizer_actual_head_conflict")
                first_event = False
            try:
                await AgentRunStore(self.session).append_event(
                    run,
                    event_type="final_outcome",
                    payload={
                        "terminal_state": terminal,
                        "policy_route": final.get("policy_route"),
                        "agent_finish_reason": run.agent_finish_reason,
                    },
                    visibility="customer",
                    expected_ticket_head_event_id=(
                        (
                            action_event_head_id
                            or verified.expected_heads.expected_ticket_head_event_id
                        )
                        if first_event
                        else ...
                    ),
                    expected_ticket_sequence=(
                        (
                            None
                            if action_event_head_id
                            else verified.expected_heads.expected_ticket_sequence
                        )
                        if first_event
                        else None
                    ),
                    expected_ticket_event_hash=(
                        (
                            action_event_head_hash
                            or verified.expected_heads.expected_ticket_event_hash
                        )
                        if first_event
                        else ...
                    ),
                )
            except CanonicalEventHeadConflict:
                await abort("finalizer_actual_head_conflict")
            await MemoryService(self.session).persist_summary(cast(Any, state))
            # Keep the active lease fence visible while finalizer publication
            # and Memory re-read the canonical source bundle.  It is cleared
            # in this same transaction immediately before the owner
            # finalization capability verifies the terminal aggregate.
            run.active_job_id = None
            run.active_fencing_token = None
        marker.status = "finalized"
        marker.status_version += 1
        if state is not None:
            await self.session.flush()
            await RuntimeJobRepository(self.session).finalize_control(
                lease, status="succeeded", outcome=terminal
            )
            await _activate_and_converge_application_fallback(
                self.session,
                ticket=cast(SupportTicket, ticket),
                trace_id=f"turn-dispatch:{run.id}",
                default_status=terminal,
            )
        await self.session.flush()
        return marker
