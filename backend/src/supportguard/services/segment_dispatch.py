from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import AgentRunStore
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    CheckpointCommitMarker,
    FinalizerPayload,
    SupportTicket,
)
from supportguard.services.runtime_jobs import (
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.segment_common import stable_hash

if TYPE_CHECKING:

    class _RunDispatchDependencies:
        async def _bind_resume_canonical_head(
            self,
            marker: CheckpointCommitMarker,
            *,
            state: dict[str, Any],
            approval_id: str | None,
        ) -> None:
            raise NotImplementedError

        async def _build_payload_v2(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

else:

    class _RunDispatchDependencies:
        pass


class RunDispatchSegments(_RunDispatchDependencies):
    session: AsyncSession

    async def prepare(
        self,
        lease: JobLease,
        *,
        delivery_generation: int,
        segment_kind: str,
        segment_input: dict[str, Any],
    ) -> CheckpointCommitMarker:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        run = await self.session.scalar(
            select(AgentRun).where(AgentRun.id == lease.run_id).with_for_update()
        )
        if run is None:
            raise RuntimeConflict("run_not_found")
        ticket = await self.session.get(SupportTicket, run.ticket_id, with_for_update=True)
        if ticket is None:
            raise RuntimeConflict("ticket_not_found")
        ticket_head = await self.session.scalar(
            select(AgentEvent)
            .where(AgentEvent.ticket_id == ticket.id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        if run.next_run_sequence == 0:
            ticket_head = await AgentRunStore(self.session).append_event(
                run,
                event_type="run_started",
                visibility="customer",
                payload={
                    "provider_mode": run.provider_mode,
                    "tool_call_mode": run.tool_call_mode,
                },
            )
        elif ticket_head is None:
            raise RuntimeConflict("segment_ticket_head_missing")
        if ticket_head is not None and ticket_head.tenant_id != lease.tenant_id:
            raise RuntimeConflict("segment_ticket_head_tenant_conflict")
        if ticket_head is not None and ticket_head.run_id != lease.run_id:
            raise RuntimeConflict("segment_ticket_head_run_conflict")
        prepared_payload = {
            "schema": "segment-prepared.v2",
            "tenant_id": lease.tenant_id,
            "ticket_id": ticket.id,
            "run_id": lease.run_id,
            "job_id": lease.job_id,
            "delivery_generation": delivery_generation,
            "fencing_token": lease.fencing_token,
            "segment_kind": segment_kind,
            "segment_input_hash": stable_hash(segment_input),
            "run_status": run.status,
            "run_status_version": run.status_version,
            "ticket_sequence": ticket.next_event_sequence,
            "ticket_event_hash": ticket_head.event_hash if ticket_head else None,
            "canonical_parent_id": run.canonical_checkpoint_id,
            "canonical_parent_hash": run.canonical_checkpoint_hash,
            "canonical_parent_version": run.canonical_checkpoint_version,
        }
        marker = CheckpointCommitMarker(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            delivery_generation=delivery_generation,
            segment_kind=segment_kind,
            private_namespace=f"{lease.run_id}/{lease.job_id}/{lease.fencing_token}",
            canonical_parent_id=run.canonical_checkpoint_id,
            canonical_parent_hash=run.canonical_checkpoint_hash,
            parent_checkpoint_version=run.canonical_checkpoint_version,
            expected_run_version=run.status_version,
            expected_run_status=run.status,
            expected_ticket_head_event_id=ticket_head.id if ticket_head else None,
            expected_ticket_sequence=ticket.next_event_sequence,
            expected_ticket_event_hash=ticket_head.event_hash if ticket_head else None,
            segment_input_hash=prepared_payload["segment_input_hash"],
            prepared_payload_hash=stable_hash(prepared_payload),
        )
        self.session.add(marker)
        await self.session.flush()
        if self.session.get_bind().dialect.name == "postgresql":
            # This FK is intentionally deferred for atomic finalizer writes, but
            # prepare is a standalone transaction.  The aggregate ticket is
            # already locked above; check the reference here so a bad head can
            # never surface only from the surrounding context-manager commit.
            await self.session.execute(
                text("SET CONSTRAINTS fk_marker_expected_event_same_run IMMEDIATE")
            )
        return marker

    async def checkpoint_written(
        self,
        lease: JobLease,
        *,
        marker_id: str,
        checkpoint_id: str,
        checkpoint_hash: str,
        outcome: str,
        state: dict[str, Any],
        proposal_id: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        marker = await self.session.get(CheckpointCommitMarker, marker_id, with_for_update=True)
        if (
            marker is None
            or marker.status != "prepared"
            or marker.fencing_token != lease.fencing_token
        ):
            raise RuntimeConflict("marker_not_writable")
        if (
            marker.segment_kind == "agent_start"
            and outcome == "completed"
            and state.get("agent_finish_reason") == "proposed"
        ):
            raise RuntimeConflict("proposal_not_durable")
        if marker.segment_kind == "approval_resume":
            await self._bind_resume_canonical_head(marker, state=state, approval_id=approval_id)
        marker.status = "checkpoint_written"
        marker.status_version += 1
        marker.final_checkpoint_id = checkpoint_id
        marker.final_checkpoint_hash = checkpoint_hash
        marker.final_checkpoint_version = marker.parent_checkpoint_version + 1
        marker.segment_outcome = outcome
        finalizer_payload = await self._build_payload_v2(
            lease,
            marker=marker,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            outcome=outcome,
            state=state,
            proposal_id=proposal_id,
            approval_id=approval_id,
        )
        finalizer_payload.verify()
        if finalizer_payload.domain_delta.outcome != outcome:
            raise RuntimeConflict("finalizer_outcome_mismatch")
        self.session.add(
            FinalizerPayload(
                tenant_id=lease.tenant_id,
                run_id=lease.run_id,
                job_id=lease.job_id,
                marker_id=marker.id,
                fencing_token=lease.fencing_token,
                schema_version=finalizer_payload.schema_version,
                payload_hash=finalizer_payload.payload_hash,
                full_payload=finalizer_payload.model_dump(mode="json"),
                state_delta=finalizer_payload.state_delta.model_dump(mode="json"),
                domain_delta=finalizer_payload.domain_delta.model_dump(mode="json"),
                expected_heads=finalizer_payload.expected_heads.model_dump(mode="json"),
            )
        )
        await self.session.flush()
