from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard.agent.graph import AgentState
from supportguard.contracts.context import WorkerExecutionContext, worker_execution_context
from supportguard.contracts.queue import RuntimeJobMessage
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    CheckpointCommitMarker,
    FinalizerPayload,
    HumanDecision,
    PolicyCapabilityInvocation,
    RawProviderDecisionEnvelope,
    TicketMessage,
    TurnGroup,
)
from supportguard.db.scope import set_local_scope
from supportguard.runtime import AppRuntime
from supportguard.services.capability_ledger import PolicyCapabilityLedger
from supportguard.services.runtime_jobs import (
    FinalizerCommitUnknown,
    JobLease,
    RuntimeJobRepository,
)
from supportguard.services.segments import SegmentRepository
from supportguard.services.tool_ledger import ToolLedger

_FinalizerResult = TypeVar("_FinalizerResult")


def finalizer_state(output: Mapping[str, Any]) -> dict[str, Any]:
    """Remove LangGraph transport metadata from the canonical domain delta."""

    return {key: value for key, value in output.items() if not key.startswith("__")}


class AgentJobHandler:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        runtime: AppRuntime,
    ) -> None:
        self.factory = factory
        self.runtime = runtime

    @staticmethod
    def _commit_outcome_is_unknown(exc: BaseException) -> bool:
        """Return true only for transport loss while COMMIT is in flight."""

        if isinstance(exc, DBAPIError):
            sqlstate = str(
                getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None) or ""
            )
            return bool(
                exc.connection_invalidated
                or sqlstate.startswith("08")
                or sqlstate in {"57P01", "57P02", "57P03"}
            )
        return isinstance(exc, (DisconnectionError, ConnectionError, OSError))

    async def _commit_finalizer_transaction(
        self,
        lease: JobLease,
        operation: Callable[[AsyncSession], Awaitable[_FinalizerResult]],
        *,
        recovery_mode: Literal["action_effect", "finalizer_only"],
    ) -> _FinalizerResult:
        """Run one Finalizer transaction and classify only COMMIT uncertainty.

        Domain, publication and memory failures happen before ``commit()`` and
        retain their ordinary typed error path.  A database disconnect while
        awaiting the outer COMMIT is different: the server may already have
        committed the action.  Raising a dedicated signal prevents the queue
        layer from converting that ambiguity into an unsafe retry/dead Job.
        """

        async with self.factory() as session:
            transaction = session.begin()
            await transaction.start()
            try:
                await set_local_scope(
                    session,
                    tenant_id=lease.tenant_id,
                    principal_id=lease.owner,
                    principal_role="system_worker",
                )
                run = await session.get(AgentRun, lease.run_id)
                if (
                    run is None
                    or run.tenant_id != lease.tenant_id
                    or (lease.ticket_id is not None and run.ticket_id != lease.ticket_id)
                ):
                    raise RuntimeError("finalizer_execution_context_unavailable")
                execution_context = WorkerExecutionContext(
                    tenant_id=lease.tenant_id,
                    actor_principal_id=run.customer_id,
                    executor_service_principal=lease.owner,
                    customer_id=run.customer_id,
                    ticket_id=run.ticket_id,
                    run_id=lease.run_id,
                    job_id=lease.job_id,
                    segment_id=f"finalizer:{lease.job_id}:{lease.fencing_token}",
                    delivery_generation=max(1, min(5, lease.attempt)),
                    fencing_token=lease.fencing_token,
                    trace_id=f"finalizer:{lease.job_id}:{lease.fencing_token}",
                    deadline=lease.expires_at,
                )
                with worker_execution_context.bind(execution_context):
                    result = await operation(session)
            except BaseException:
                if transaction.is_active:
                    await transaction.rollback()
                raise
            try:
                await transaction.commit()
            except (DBAPIError, DisconnectionError, ConnectionError, OSError) as exc:
                if not self._commit_outcome_is_unknown(exc):
                    raise
                raise FinalizerCommitUnknown(
                    job_id=lease.job_id,
                    recovery_mode=recovery_mode,
                ) from exc
            return result

    async def __call__(self, message: RuntimeJobMessage, lease: JobLease) -> str:
        async with self.factory() as lookup:
            await set_local_scope(
                lookup,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            job_kind = lease.kind
            recoverable_marker = await lookup.scalar(
                select(CheckpointCommitMarker)
                .where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                    CheckpointCommitMarker.fencing_token < lease.fencing_token,
                )
                .order_by(CheckpointCommitMarker.fencing_token.desc())
                .limit(1)
            )
            recoverable_turn = await lookup.scalar(
                select(TurnGroup)
                .where(
                    TurnGroup.job_id == lease.job_id,
                    TurnGroup.run_id == lease.run_id,
                    TurnGroup.status.in_({"open", "completing"}),
                    TurnGroup.fencing_token < lease.fencing_token,
                )
                .order_by(TurnGroup.fencing_token.desc())
                .limit(1)
            )
            unresolved_provider_attempt = await lookup.scalar(
                select(AgentCallAttempt)
                .outerjoin(
                    RawProviderDecisionEnvelope,
                    RawProviderDecisionEnvelope.provider_attempt_id == AgentCallAttempt.id,
                )
                .where(
                    AgentCallAttempt.job_id == lease.job_id,
                    AgentCallAttempt.run_id == lease.run_id,
                    AgentCallAttempt.call_kind == "llm",
                    AgentCallAttempt.status == "started",
                    AgentCallAttempt.fencing_token < lease.fencing_token,
                    RawProviderDecisionEnvelope.id.is_(None),
                )
                .order_by(AgentCallAttempt.ordinal.desc())
                .limit(1)
            )
            unreplayable_provider_envelope = await lookup.scalar(
                select(RawProviderDecisionEnvelope)
                .join(
                    AgentCallAttempt,
                    AgentCallAttempt.id == RawProviderDecisionEnvelope.provider_attempt_id,
                )
                .where(
                    AgentCallAttempt.job_id == lease.job_id,
                    AgentCallAttempt.run_id == lease.run_id,
                    AgentCallAttempt.call_kind.in_({"llm", "structure_repair"}),
                    AgentCallAttempt.status == "succeeded",
                    AgentCallAttempt.fencing_token < lease.fencing_token,
                    RawProviderDecisionEnvelope.intake_status.in_({"received", "parsed"}),
                )
                .order_by(AgentCallAttempt.created_at.desc())
                .limit(1)
            )
            stale_active_capability = await lookup.scalar(
                select(PolicyCapabilityInvocation)
                .where(
                    PolicyCapabilityInvocation.job_id == lease.job_id,
                    PolicyCapabilityInvocation.run_id == lease.run_id,
                    PolicyCapabilityInvocation.status.in_({"reserved", "executing", "unknown"}),
                    PolicyCapabilityInvocation.fencing_token < lease.fencing_token,
                )
                .order_by(PolicyCapabilityInvocation.sequence)
                .limit(1)
            )
        if stale_active_capability is not None:
            return await self._terminalize_stale_capabilities(lease)
        if recoverable_marker is not None:
            return await self._recover_finalizer_only(lease, recoverable_marker.id)
        if recoverable_turn is not None:
            return await self._recover_durable_tool_turn(lease, recoverable_turn.id)
        if unresolved_provider_attempt is not None:
            await self._converge_unresolved_provider_attempt(
                lease,
                unresolved_provider_attempt.id,
            )
        if unreplayable_provider_envelope is not None:
            return await self._terminalize_unreplayable_provider_decision(lease)
        if job_kind == "approval_resume":
            return await self._resume_approval(lease)
        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            run = await session.get(AgentRun, lease.run_id)
            if run is None:
                raise RuntimeError("claimed AgentRun disappeared")
            if message.tenant_id != lease.tenant_id or run.tenant_id != lease.tenant_id:
                raise RuntimeError("trusted job tenant mismatch")
            message_row = await session.get(TicketMessage, run.message_id)
            if message_row is None:
                raise RuntimeError("AgentRun message disappeared")
            marker = await SegmentRepository(session).prepare(
                lease,
                delivery_generation=message.delivery_generation,
                segment_kind="agent_start",
                segment_input={"message_id": message_row.id, "kind": "agent_start"},
            )
            marker_id = marker.id
            checkpoint_ns = marker.private_namespace
            state = AgentState(
                tenant_id=lease.tenant_id,
                ticket_id=run.ticket_id,
                customer_id=run.customer_id,
                run_id=run.id,
                job_id=lease.job_id,
                segment_id=marker_id,
                delivery_generation=message.delivery_generation,
                fencing_token=lease.fencing_token,
                trace_id=message.event_id,
                user_message=message_row.content,
                ingress_redaction_count=sum(
                    int(item.get("count", 0))
                    for item in message_row.source_refs
                    if isinstance(item, dict) and item.get("kind") == "ingress_redaction_receipt"
                ),
                redaction_rule_ids=list(
                    dict.fromkeys(
                        str(rule_id)
                        for item in message_row.source_refs
                        if isinstance(item, dict)
                        and item.get("kind") == "ingress_redaction_receipt"
                        for rule_id in item.get("rule_ids", [])
                    )
                ),
            )
        execution_context = WorkerExecutionContext(
            tenant_id=lease.tenant_id,
            actor_principal_id=run.customer_id,
            executor_service_principal=lease.owner,
            customer_id=run.customer_id,
            ticket_id=run.ticket_id,
            run_id=run.id,
            job_id=lease.job_id,
            segment_id=marker_id,
            delivery_generation=message.delivery_generation,
            fencing_token=lease.fencing_token,
            trace_id=message.event_id,
            deadline=lease.expires_at,
        )
        output = await self.runtime.run_ticket(
            state,
            execution_context=execution_context,
            checkpoint_ns=checkpoint_ns,
        )
        return await self._persist_graph_output(
            lease,
            marker_id=marker_id,
            checkpoint_ns=checkpoint_ns,
            output=output,
        )

    async def _converge_unresolved_provider_attempt(
        self,
        lease: JobLease,
        attempt_id: str,
    ) -> None:
        """Consume an unproven Provider attempt without refunding its durable budget."""

        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            await RuntimeJobRepository(session).assert_fence(lease)
            attempt = await session.get(AgentCallAttempt, attempt_id, with_for_update=True)
            raw = await session.scalar(
                select(RawProviderDecisionEnvelope).where(
                    RawProviderDecisionEnvelope.provider_attempt_id == attempt_id
                )
            )
            if (
                attempt is None
                or attempt.run_id != lease.run_id
                or attempt.job_id != lease.job_id
                or attempt.status != "started"
                or raw is not None
            ):
                raise RuntimeError("unresolved provider attempt changed during recovery")
            attempt.status = "unknown"
            attempt.error_code = "provider_result_unproven_after_takeover"

    async def _terminalize_stale_capabilities(self, lease: JobLease) -> str:
        """Settle abandoned policy effects once, then expose an explicit failure."""

        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            rows = (
                await session.scalars(
                    select(PolicyCapabilityInvocation)
                    .where(
                        PolicyCapabilityInvocation.tenant_id == lease.tenant_id,
                        PolicyCapabilityInvocation.run_id == lease.run_id,
                        PolicyCapabilityInvocation.job_id == lease.job_id,
                        PolicyCapabilityInvocation.status.in_({"reserved", "executing", "unknown"}),
                        PolicyCapabilityInvocation.fencing_token < lease.fencing_token,
                    )
                    .order_by(PolicyCapabilityInvocation.sequence)
                    .with_for_update()
                )
            ).all()
            ledger = PolicyCapabilityLedger(session)
            for row in rows:
                await ledger.reconcile_stale_active_effect(
                    lease,
                    invocation_id=row.id,
                )
            terminal_outcome = await RuntimeJobRepository(session).terminal_fail(
                lease,
                error_code="stale_capability_reconciled_failed",
            )
        return (
            terminal_outcome
            if terminal_outcome.startswith(("failed:", "terminal_failed:"))
            else "failed"
        )

    async def _terminalize_unreplayable_provider_decision(
        self,
        lease: JobLease,
    ) -> str:
        """Fail closed when a taken-over Provider decision has no replayable payload.

        Raw response bodies are intentionally not persisted.  A successor may
        resume a durable tool turn or finalizer, but it must never call the
        Provider again after a successful decision whose next checkpoint was
        not committed.
        """

        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            terminal_outcome = await RuntimeJobRepository(session).terminal_fail(
                lease,
                error_code="provider_decision_unreplayable_after_takeover",
            )
        return (
            terminal_outcome
            if terminal_outcome.startswith(("failed:", "terminal_failed:"))
            else "failed"
        )

    async def _persist_graph_output(
        self,
        lease: JobLease,
        *,
        marker_id: str,
        checkpoint_ns: str,
        output: AgentState,
    ) -> str:
        config = {
            "configurable": {
                "thread_id": checkpoint_ns,
                "checkpoint_ns": "",
            }
        }
        checkpoint = await self.runtime.checkpointer.aget_tuple(config)
        if checkpoint is None:
            raise RuntimeError("Graph segment returned without a checkpoint")
        checkpoint_id = str(checkpoint.config["configurable"]["checkpoint_id"])
        checkpoint_hash = hashlib.sha256(
            json.dumps(checkpoint.checkpoint, sort_keys=True, default=str).encode()
        ).hexdigest()
        proposal_id = str(output.get("action_result", {}).get("proposal_id", ""))
        outcome: Literal["interrupted", "completed"] = "interrupted" if proposal_id else "completed"
        # ``checkpoint_written`` is a durable recovery boundary, not part of
        # the later canonical Finalizer transaction.  If publication, memory,
        # or the Runtime-only action fails, the Finalizer transaction may roll
        # back without demoting the Marker to ``prepared`` and without
        # replaying the Graph/Provider.
        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            reconciled_proposal_id = await self._reconcile_unknown_capabilities(
                session,
                lease,
                output,
            )
            if reconciled_proposal_id:
                proposal_id = reconciled_proposal_id
                outcome = "interrupted"
            segments = SegmentRepository(session)
            await segments.checkpoint_written(
                lease,
                marker_id=marker_id,
                checkpoint_id=checkpoint_id,
                checkpoint_hash=checkpoint_hash,
                outcome=outcome,
                state=finalizer_state(output),
                proposal_id=proposal_id or None,
            )

        async def finalize_output(session: AsyncSession) -> None:
            segments = SegmentRepository(session)
            if outcome == "interrupted":
                await segments.finalize_interrupt(
                    lease,
                    marker_id=marker_id,
                    proposal_id=proposal_id,
                )
            else:
                await segments.finalize(lease, marker_id=marker_id)

        await self._commit_finalizer_transaction(
            lease,
            finalize_output,
            recovery_mode="finalizer_only",
        )
        return outcome

    @staticmethod
    async def _reconcile_unknown_capabilities(
        session: AsyncSession,
        lease: JobLease,
        output: AgentState,
    ) -> str | None:
        """Converge unknown policy calls from durable receipts before publication."""

        unknowns = (
            await session.scalars(
                select(PolicyCapabilityInvocation)
                .where(
                    PolicyCapabilityInvocation.tenant_id == lease.tenant_id,
                    PolicyCapabilityInvocation.run_id == lease.run_id,
                    PolicyCapabilityInvocation.job_id == lease.job_id,
                    PolicyCapabilityInvocation.fencing_token == lease.fencing_token,
                    PolicyCapabilityInvocation.status == "unknown",
                )
                .with_for_update()
            )
        ).all()
        proposal_id: str | None = None
        ledger = PolicyCapabilityLedger(session)
        for invocation in unknowns:
            result = await ledger.reconcile_unknown_effect(
                lease,
                invocation_id=invocation.id,
            )
            if result.status == "succeeded" and result.payload.get("proposal_id"):
                output["action_result"] = dict(result.payload)
                proposal_id = str(result.payload["proposal_id"])
        return proposal_id

    async def _recover_durable_tool_turn(self, lease: JobLease, turn_id: str) -> str:
        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            turn = await session.get(TurnGroup, turn_id, with_for_update=True)
            run = await session.get(AgentRun, lease.run_id, with_for_update=True)
            if turn is None or run is None:
                raise RuntimeError("durable tool turn disappeared")
            marker = await SegmentRepository(session).takeover_prepared_tool_turn(
                lease, marker_id=turn.segment_id
            )
            await ToolLedger(session).takeover(lease, turn.id)
            checkpoint_ns = marker.private_namespace
            execution_context = WorkerExecutionContext(
                tenant_id=lease.tenant_id,
                actor_principal_id=run.customer_id,
                executor_service_principal=lease.owner,
                customer_id=run.customer_id,
                ticket_id=run.ticket_id,
                run_id=run.id,
                job_id=lease.job_id,
                segment_id=marker.id,
                delivery_generation=marker.delivery_generation,
                fencing_token=lease.fencing_token,
                trace_id=f"turn-takeover:{turn.id}",
                deadline=lease.expires_at,
            )
            marker_id = marker.id
        try:
            output = await self.runtime.resume_durable_tool_turn(
                checkpoint_ns=checkpoint_ns,
                execution_context=execution_context,
            )
        except RuntimeError as exc:
            if str(exc) != "durable tool turn checkpoint is unavailable":
                raise
            async with self.factory() as session, session.begin():
                await set_local_scope(
                    session,
                    tenant_id=lease.tenant_id,
                    principal_id=lease.owner,
                    principal_role="system_worker",
                )
                await ToolLedger(session).abort_pending(
                    lease,
                    turn_id,
                    ticket_id=execution_context.ticket_id,
                    reason="durable_turn_checkpoint_unavailable",
                )
                terminal_outcome = await RuntimeJobRepository(session).terminal_fail(
                    lease,
                    error_code="durable_turn_checkpoint_unavailable",
                )
            return (
                terminal_outcome
                if terminal_outcome.startswith(("failed:", "terminal_failed:"))
                else "failed"
            )
        return await self._persist_graph_output(
            lease,
            marker_id=marker_id,
            checkpoint_ns=checkpoint_ns,
            output=output,
        )

    async def _recover_finalizer_only(self, lease: JobLease, source_marker_id: str) -> str:
        async with self.factory() as lookup:
            await set_local_scope(
                lookup,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            source_payload = await lookup.scalar(
                select(FinalizerPayload).where(
                    FinalizerPayload.marker_id == source_marker_id,
                )
            )
            source_variant = (
                str(source_payload.domain_delta.get("variant", ""))
                if source_payload is not None
                else ""
            )
        recovery_mode: Literal["action_effect", "finalizer_only"] = (
            "action_effect"
            if source_variant
            in {
                "approval_action_intent",
                "approval_actionful",
                "approval_fail_closed",
            }
            else "finalizer_only"
        )

        async def recover_finalizer(session: AsyncSession) -> str:
            segments = SegmentRepository(session)
            marker = await segments.takeover_finalizer(lease, source_marker_id=source_marker_id)
            payload = await session.scalar(
                select(FinalizerPayload).where(FinalizerPayload.marker_id == marker.id)
            )
            if payload is None:
                raise RuntimeError("taken-over finalizer payload disappeared")
            if marker.segment_outcome == "interrupted":
                proposal_id = str(payload.domain_delta.get("proposal_id", ""))
                if not proposal_id:
                    raise RuntimeError("interrupt finalizer has no proposal binding")
                await segments.finalize_interrupt(
                    lease, marker_id=marker.id, proposal_id=proposal_id
                )
                return "interrupted"
            await segments.finalize(lease, marker_id=marker.id)
            return "completed"

        return await self._commit_finalizer_transaction(
            lease,
            recover_finalizer,
            recovery_mode=recovery_mode,
        )

    async def _resume_approval(self, lease: JobLease) -> str:
        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            if not lease.approval_id:
                raise RuntimeError("approval resume job has no approval")
            approval = await session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == lease.approval_id,
                    ApprovalRequest.tenant_id == lease.tenant_id,
                    ApprovalRequest.run_id == lease.run_id,
                    ApprovalRequest.ticket_id == lease.ticket_id,
                )
            )
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == lease.approval_id)
            )
            revision = (
                await session.get(ApprovalActionRevision, approval.selected_revision_id or "")
                if approval is not None
                else None
            )
            if approval is None or decision is None or not approval.canonical_checkpoint_ns:
                raise RuntimeError("approval resume binding is incomplete")
            if revision is None or decision.action_revision_id != revision.id:
                raise RuntimeError("approval revision binding is incomplete")
            payload = {
                "action": decision.decision,
                "approval_id": approval.id,
                "idempotency_key": approval.idempotency_key,
                "approver_id": decision.actor_id,
                "reason": decision.reason,
                "job_id": lease.job_id,
                "fencing_token": lease.fencing_token,
            }
            if decision.decision == "edit_and_approve":
                payload["refund_reason"] = str(revision.action_payload.get("refund_reason", ""))
            checkpoint_ns = approval.canonical_checkpoint_ns
            checkpoint_id = str(approval.checkpoint_id)
            run_id = str(approval.run_id)
            approval_id = approval.id
            action_effect_resume = decision.decision in {
                "approve",
                "edit_and_approve",
            }
            marker = await SegmentRepository(session).prepare(
                lease,
                delivery_generation=max(1, min(5, lease.attempt)),
                segment_kind="approval_resume",
                segment_input={
                    "approval_id": approval.id,
                    "decision_id": decision.id,
                    "kind": "approval_resume",
                },
            )
            marker_id = marker.id
            private_namespace = marker.private_namespace
        await self.runtime.fork_checkpoint(
            source_namespace=checkpoint_ns,
            source_checkpoint_id=checkpoint_id,
            target_namespace=private_namespace,
        )
        output = await self.runtime.resume_ticket(
            run_id=run_id,
            approval_id=approval_id,
            decision=payload,
            execution_context=WorkerExecutionContext(
                tenant_id=lease.tenant_id,
                actor_principal_id=approval.customer_id,
                executor_service_principal=lease.owner,
                customer_id=approval.customer_id,
                ticket_id=approval.ticket_id,
                run_id=run_id,
                job_id=lease.job_id,
                segment_id=marker_id,
                delivery_generation=max(1, min(5, lease.attempt)),
                fencing_token=lease.fencing_token,
                trace_id=f"resume:{approval_id}",
                deadline=lease.expires_at,
            ),
            checkpoint_ns=private_namespace,
        )
        config = {
            "configurable": {
                "thread_id": private_namespace,
                "checkpoint_ns": "",
            }
        }
        checkpoint = await self.runtime.checkpointer.aget_tuple(config)
        if checkpoint is None:
            raise RuntimeError("Approval resume returned without a checkpoint")
        final_checkpoint_id = str(checkpoint.config["configurable"]["checkpoint_id"])
        checkpoint_hash = hashlib.sha256(
            json.dumps(checkpoint.checkpoint, sort_keys=True, default=str).encode()
        ).hexdigest()
        # Commit the selected private checkpoint before entering the action
        # Finalizer.  This is the only recovery entry after a successful Graph
        # resume; a failed Finalizer must never force another Provider call.
        async with self.factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=lease.tenant_id,
                principal_id=lease.owner,
                principal_role="system_worker",
            )
            segments = SegmentRepository(session)
            await segments.checkpoint_written(
                lease,
                marker_id=marker_id,
                checkpoint_id=final_checkpoint_id,
                checkpoint_hash=checkpoint_hash,
                outcome="completed",
                state=finalizer_state(output),
                approval_id=approval_id,
            )

        async def finalize_resume(session: AsyncSession) -> None:
            segments = SegmentRepository(session)
            await segments.finalize(lease, marker_id=marker_id)

        await self._commit_finalizer_transaction(
            lease,
            finalize_resume,
            recovery_mode=("action_effect" if action_effect_resume else "finalizer_only"),
        )
        return "completed"
