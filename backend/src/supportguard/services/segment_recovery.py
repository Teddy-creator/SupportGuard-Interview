from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Never, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.finalizer import (
    FinalizerPayloadV2,
)
from supportguard.db.models import (
    AgentRun,
    AuditEvent,
    CheckpointCommitMarker,
    ConversationTurn,
    FinalizerPayload,
    ProposalRecord,
    RawProviderDecisionEnvelope,
    RuntimeJob,
    SupportTicket,
)
from supportguard.services.runtime_jobs import (
    FinalizerRestartRequired,
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
    converge_dead_aggregates,
)

logger = logging.getLogger(__name__)


class RecoverySegments:
    session: AsyncSession

    async def takeover_prepared_tool_turn(
        self, lease: JobLease, *, marker_id: str
    ) -> CheckpointCommitMarker:
        """Transfer a prepared checkpoint namespace only when canonical heads are unchanged."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        marker = await self.session.get(CheckpointCommitMarker, marker_id, with_for_update=True)
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if (
            marker is None
            or run is None
            or marker.tenant_id != lease.tenant_id
            or marker.run_id != lease.run_id
            or marker.job_id != lease.job_id
            or marker.status != "prepared"
            or marker.fencing_token >= lease.fencing_token
            or marker.canonical_parent_id != run.canonical_checkpoint_id
            or marker.canonical_parent_hash != run.canonical_checkpoint_hash
            or marker.parent_checkpoint_version != run.canonical_checkpoint_version
        ):
            raise RuntimeConflict("prepared_turn_takeover_not_allowed")
        marker.fencing_token = lease.fencing_token
        marker.expected_run_version = run.status_version
        marker.expected_run_status = run.status
        await self.session.flush()
        return marker

    async def _abort_finalizer(
        self,
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        reason: str,
        proposal: ProposalRecord | None = None,
    ) -> Never:
        """Commit a fail-closed terminal under an already-held domain prefix.

        Every caller must acquire the Ticket/Job (or the stronger action
        aggregate) before it locks ``marker``.  Re-acquiring that prefix here
        would recreate the Marker -> Ticket edge that takeover and reconciler
        paths intentionally avoid.
        """

        marker.status = "aborted"
        marker.status_version += 1
        if proposal is not None:
            proposal.status = "stale"
        job = (
            await self.session.get(RuntimeJob, lease.job_id)
            if self.session.get_bind().dialect.name != "postgresql"
            else None
        )
        if job is not None and (
            job.run_id != lease.run_id
            or job.tenant_id != lease.tenant_id
            or job.fencing_token != lease.fencing_token
        ):
            raise RuntimeConflict("stale_fencing_token")
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is not None:
            completed_at = datetime.now(UTC)
            run.status = "failed"
            run.error_code = reason
            run.active_job_id = None
            run.active_fencing_token = None
            run.completed_at = run.completed_at or completed_at
            run.status_version += 1
            turn = (
                await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
                if run.turn_id
                else None
            )
            if turn is not None:
                turn.activity_state = "failed"
                turn.result_state = "failed"
                turn.completed_at = turn.completed_at or completed_at
            ticket = await self.session.get(SupportTicket, run.ticket_id, with_for_update=True)
            if ticket is not None:
                ticket.status = "failed"
                ticket.version += 1
                self.session.add(
                    AuditEvent(
                        tenant_id=lease.tenant_id,
                        ticket_id=ticket.id,
                        customer_id=run.customer_id,
                        event_type="finalizer_aborted",
                        actor_type="runtime",
                        actor_id=lease.owner,
                        payload={"marker_id": marker.id, "reason": reason},
                        trace_id=f"finalizer:{marker.id}",
                        run_id=run.id,
                    )
                )
        await self.session.flush()
        await RuntimeJobRepository(self.session).finalize_control(
            lease, status="dead", outcome=reason
        )
        await self.session.flush()
        if (
            run is not None
            and job is not None
            and self.session.get_bind().dialect.name != "postgresql"
        ):
            await converge_dead_aggregates(
                self.session,
                job=job,
                run=run,
                reason=reason,
            )
            await self.session.flush()
        await self.session.commit()
        raise RuntimeConflict(reason)

    async def _restart_pre_effect_finalizer(
        self,
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        run: AgentRun,
        mismatch_paths: tuple[str, ...],
    ) -> Never:
        """Abandon one stale, effect-free answer so a new fence can recompute it.

        The retry budget is durable and bounded to one restart per Agent Run.
        Raw Provider envelopes from the abandoned fence are marked rejected so
        takeover logic cannot mistake a deliberately superseded decision for
        an unreplayable success.  No Tool/Policy/Action ledger drift reaches
        this path.
        """

        prior_restart = await self.session.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.tenant_id == lease.tenant_id,
                AuditEvent.run_id == lease.run_id,
                AuditEvent.event_type == "pre_effect_finalizer_restart",
            )
            .limit(1)
        )
        if prior_restart is not None:
            await self._abort_finalizer(
                lease,
                marker=marker,
                reason="pre_effect_finalizer_restart_exhausted",
            )
        marker.status = "aborted"
        marker.status_version += 1
        envelopes = (
            await self.session.scalars(
                select(RawProviderDecisionEnvelope).where(
                    RawProviderDecisionEnvelope.tenant_id == lease.tenant_id,
                    RawProviderDecisionEnvelope.run_id == lease.run_id,
                    RawProviderDecisionEnvelope.job_id == lease.job_id,
                    RawProviderDecisionEnvelope.segment_id == marker.id,
                    RawProviderDecisionEnvelope.fencing_token == lease.fencing_token,
                    RawProviderDecisionEnvelope.intake_status.in_({"received", "parsed"}),
                )
            )
        ).all()
        for envelope in envelopes:
            envelope.intake_status = "rejected"
            envelope.rejection_code = "finalizer_head_changed_before_publication"
        self.session.add(
            AuditEvent(
                tenant_id=lease.tenant_id,
                ticket_id=run.ticket_id,
                customer_id=run.customer_id,
                event_type="pre_effect_finalizer_restart",
                actor_type="runtime",
                actor_id=lease.owner,
                payload={
                    "job_id": lease.job_id,
                    "marker_id": marker.id,
                    "reason": "finalizer_resource_version_head_changed",
                    "mismatch_paths": list(mismatch_paths),
                    "provider_envelopes_superseded": len(envelopes),
                },
                trace_id=f"finalizer:{marker.id}",
                run_id=run.id,
            )
        )
        await self.session.flush()
        await self.session.commit()
        raise FinalizerRestartRequired()

    async def _fail_confirmed_zero_effect_finalizer(
        self,
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        reason: str,
    ) -> Never:
        """Converge an inert Approval-resume through the lifecycle capability.

        An ``ActionIntentApprovalResumeDelta`` has not entered the Runtime-only
        action boundary, so a validated payload/head conflict is confirmed
        zero-effect.  The restricted PostgreSQL Worker must not hand-write only
        Marker/Run/Job terminal rows: the existing owner-controlled finish
        capability owns the complete Ticket-first lifecycle delta, including
        Approval/Proposal, customer action update, next-turn activation, and
        the dead Job outcome.
        """

        is_worker_postgres = False
        if self.session.get_bind().dialect.name == "postgresql":
            is_worker_postgres = (
                await self.session.scalar(text("SELECT session_user")) == "supportguard_worker"
            )
        if is_worker_postgres:
            outcome = f"finalizer_zero_effect_failed:{reason[:96]}"
            finished = await self.session.scalar(
                text(
                    "SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"
                ),
                {
                    "job_id": lease.job_id,
                    "owner": lease.owner,
                    "fencing_token": lease.fencing_token,
                    "outcome": outcome,
                },
            )
            if (
                not isinstance(finished, dict)
                or finished.get("status") != "dead"
                or finished.get("ticket_status") == "manual_takeover"
                or finished.get("automation_mode") == "human_queue"
            ):
                raise RuntimeConflict("finalizer_fail_closed_capability_failed")
            await self.session.commit()
            raise RuntimeConflict(reason)

        # SQLite and unrestricted test sessions use the application fallback.
        # Its transition core is exercised independently; production never
        # widens the Worker into direct aggregate table writes.
        await self._abort_finalizer(lease, marker=marker, reason=reason)

    async def takeover_finalizer(
        self, lease: JobLease, *, source_marker_id: str
    ) -> CheckpointCommitMarker:
        """K4 recovery: transfer only a verified persisted finalizer to the new fence."""
        job = await RuntimeJobRepository(self.session).assert_fence(lease)
        ticket = await self.session.get(
            SupportTicket,
            lease.ticket_id or "",
            with_for_update=True,
        )
        source = await self.session.get(
            CheckpointCommitMarker, source_marker_id, with_for_update=True
        )
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        payload = await self.session.scalar(
            select(FinalizerPayload).where(FinalizerPayload.marker_id == source_marker_id)
        )
        if (
            source is None
            or ticket is None
            or run is None
            or payload is None
            or source.status != "checkpoint_written"
            or source.job_id != lease.job_id
            or lease.fencing_token != source.fencing_token + 1
            or run.canonical_checkpoint_id != source.canonical_parent_id
        ):
            raise RuntimeConflict("finalizer_takeover_not_allowed")
        try:
            verified = FinalizerPayloadV2.model_validate(payload.full_payload)
            verified.verify()
        except ValueError as exc:
            try:
                await self._abort_finalizer(
                    lease,
                    marker=source,
                    reason="finalizer_payload_hash_mismatch",
                )
            except RuntimeConflict as conflict:
                raise conflict from exc
        expected_job_version = verified.expected_heads.expected_domain_resource_versions.get(
            f"job:{lease.job_id}"
        )
        if (
            expected_job_version is None
            # One Job/Run version records the failed lease's retry transition;
            # the next records this exact successor claim.
            or job.status_version != expected_job_version + 2
            or run.status_version != source.expected_run_version + 2
            or job.attempt != lease.attempt
            or job.fencing_token != lease.fencing_token
            or lease.attempt != source.fencing_token + 1
            or not str(job.last_error or "").startswith("failed:")
        ):
            raise RuntimeConflict("finalizer_takeover_not_allowed")
        replacement = CheckpointCommitMarker(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            delivery_generation=source.delivery_generation,
            segment_kind=source.segment_kind,
            private_namespace=source.private_namespace,
            canonical_parent_id=source.canonical_parent_id,
            canonical_parent_hash=source.canonical_parent_hash,
            parent_checkpoint_version=source.parent_checkpoint_version,
            expected_run_version=run.status_version,
            expected_run_status=run.status,
            expected_ticket_head_event_id=source.expected_ticket_head_event_id,
            expected_ticket_sequence=source.expected_ticket_sequence,
            expected_ticket_event_hash=source.expected_ticket_event_hash,
            segment_input_hash=source.segment_input_hash,
            prepared_payload_hash=source.prepared_payload_hash,
            final_checkpoint_id=source.final_checkpoint_id,
            final_checkpoint_hash=source.final_checkpoint_hash,
            final_checkpoint_version=source.final_checkpoint_version,
            segment_outcome=source.segment_outcome,
            status="checkpoint_written",
            status_version=2,
        )
        self.session.add(replacement)
        await self.session.flush()
        takeover_resource_versions = dict(verified.expected_heads.expected_domain_resource_versions)
        takeover_resource_versions[f"ticket:{ticket.id}"] = ticket.version
        takeover_resource_versions[f"run:{run.id}"] = run.status_version
        takeover_resource_versions[f"job:{job.id}"] = job.status_version
        takeover_heads = verified.expected_heads.model_copy(
            update={
                "expected_run_status": run.status,
                "expected_run_status_version": run.status_version,
                "parent_checkpoint_version": run.canonical_checkpoint_version,
                "expected_marker_status_version": replacement.status_version,
                "expected_domain_resource_versions": takeover_resource_versions,
            }
        )
        takeover_payload = FinalizerPayloadV2.build(
            tenant_id=lease.tenant_id,
            ticket_id=verified.ticket_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=replacement.id,
            delivery_generation=replacement.delivery_generation,
            fencing_token=lease.fencing_token,
            parent_segment_id=verified.parent_segment_id,
            marker_id=replacement.id,
            segment_kind=cast(Any, replacement.segment_kind),
            prepared_payload_hash=replacement.prepared_payload_hash,
            expected_heads=takeover_heads,
            state=verified.state_delta.state,
            domain_delta=verified.domain_delta,
        )
        self.session.add(
            FinalizerPayload(
                tenant_id=lease.tenant_id,
                run_id=lease.run_id,
                job_id=lease.job_id,
                marker_id=replacement.id,
                fencing_token=lease.fencing_token,
                schema_version=takeover_payload.schema_version,
                payload_hash=takeover_payload.payload_hash,
                full_payload=takeover_payload.model_dump(mode="json"),
                state_delta=takeover_payload.state_delta.model_dump(mode="json"),
                domain_delta=takeover_payload.domain_delta.model_dump(mode="json"),
                expected_heads=takeover_payload.expected_heads.model_dump(mode="json"),
            )
        )
        source.status = "aborted"
        await self.session.flush()
        return replacement
