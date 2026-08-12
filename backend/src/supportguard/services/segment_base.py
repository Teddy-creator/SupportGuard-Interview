from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.finalizer import (
    FinalizerPayloadV2,
    finalizer_payload_mismatch_paths,
)
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    CheckpointCommitMarker,
    ConversationTurn,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.db.scope import set_local_scope
from supportguard.policies.pii import redact_pii
from supportguard.services.approval_lifecycle import (
    ACTION_RESOURCE_TYPES,
    ACTIVE_APPROVAL_STATUSES,
)
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.runtime_jobs import (
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
)

logger = logging.getLogger(__name__)


class SegmentTransactionBase:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _marker_lock_snapshot(marker: CheckpointCommitMarker) -> tuple[object, ...]:
        """Capture the immutable/fenced fields revalidated at the Marker lock.

        Finalizer identity may be discovered without a lock, but the Marker is
        deliberately not locked until the owning Ticket/domain lock prefix has
        been acquired.  Keeping this snapshot explicit prevents that pre-read
        from becoming authority.
        """

        return (
            marker.id,
            marker.tenant_id,
            marker.run_id,
            marker.job_id,
            marker.fencing_token,
            marker.status,
            marker.status_version,
            marker.segment_kind,
            marker.final_checkpoint_id,
            marker.final_checkpoint_hash,
            marker.final_checkpoint_version,
            marker.prepared_payload_hash,
        )

    async def _lock_marker_after_domain(
        self,
        marker_id: str,
        *,
        expected_snapshot: tuple[object, ...],
    ) -> CheckpointCommitMarker:
        """Lock/revalidate a Marker only after the operation's domain prefix."""

        marker = await self.session.scalar(
            select(CheckpointCommitMarker)
            .where(CheckpointCommitMarker.id == marker_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            marker is None
            or marker.status != "checkpoint_written"
            or self._marker_lock_snapshot(marker) != expected_snapshot
        ):
            raise RuntimeConflict("marker_not_finalizable")
        return marker

    async def _lock_interrupt_finalize_domain(
        self,
        lease: JobLease,
        *,
        proposal_id: str,
    ) -> tuple[
        SupportTicket,
        ProposalRecord,
        AgentRun,
        ApprovalRequest | None,
    ]:
        """Lock and revalidate the interrupt aggregate before its Marker.

        ``finalize_interrupt`` has no Approval yet.  Its frozen worker-finalize
        order is therefore Ticket -> canonical active Approval -> every live
        Proposal for the same resource (stable ID order) -> Run/Turn ->
        RuntimeJob, with the Marker acquired only after this method returns.
        Identity reads before this prefix are discovery only; every field is
        checked again under the ordered locks.

        The production Worker owns no broad UPDATE grants on these tables, so
        PostgreSQL performs the ordered locks through the existing
        ``supportguard_worker_finalize(jsonb)`` owner capability.  SQLite and
        privileged migration/fault fixtures execute the same ordering
        explicitly.
        """

        await set_local_scope(
            self.session,
            tenant_id=lease.tenant_id,
            principal_id=lease.owner,
            principal_role="system_worker",
        )
        is_restricted_worker = bool(
            self.session.get_bind().dialect.name == "postgresql"
            and await self.session.scalar(text("SELECT session_user")) == "supportguard_worker"
        )
        if is_restricted_worker:
            # The restricted Worker deliberately has no direct runtime_jobs
            # SELECT grant.  Its lease already carries the owner-issued Ticket
            # identity; the capability re-reads and revalidates the Job.
            ticket_id = lease.ticket_id
            if ticket_id is None:
                raise RuntimeConflict("stale_fencing_token")
            active_approval_id: str | None = None
            try:
                snapshot = await self.session.scalar(
                    text("SELECT supportguard_worker_finalize(CAST(:payload AS jsonb))"),
                    {
                        "payload": json.dumps(
                            {
                                "schema_version": "worker-interrupt-fence.v1",
                                "job_id": lease.job_id,
                                "run_id": lease.run_id,
                                "tenant_id": lease.tenant_id,
                                "owner": lease.owner,
                                "fencing_token": lease.fencing_token,
                                "proposal_id": proposal_id,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                )
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if sqlstate == "P0001":
                    raise RuntimeConflict("stale_fencing_token") from exc
                if sqlstate == "22023":
                    raise RuntimeConflict("interrupt_fence_schema_invalid") from exc
                raise
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("result") != "locked"
                or str(snapshot.get("job_id", "")) != lease.job_id
                or str(snapshot.get("run_id", "")) != lease.run_id
                or str(snapshot.get("tenant_id", "")) != lease.tenant_id
                or str(snapshot.get("ticket_id", "")) != ticket_id
                or str(snapshot.get("proposal_id", "")) != proposal_id
                or int(snapshot.get("fencing_token", -1)) != lease.fencing_token
                or (
                    lease.dispatch_sequence is not None
                    and int(snapshot.get("dispatch_sequence", -1)) != lease.dispatch_sequence
                )
            ):
                raise RuntimeConflict("stale_fencing_token")
            raw_active_approval_id = snapshot.get("active_approval_id")
            if raw_active_approval_id is not None:
                active_approval_id = str(raw_active_approval_id)
        else:
            job_probe = await self.session.scalar(
                select(RuntimeJob).where(RuntimeJob.id == lease.job_id)
            )
            proposal_probe = await self.session.scalar(
                select(ProposalRecord).where(
                    ProposalRecord.tenant_id == lease.tenant_id,
                    ProposalRecord.id == proposal_id,
                    ProposalRecord.run_id == lease.run_id,
                )
            )
            if (
                job_probe is None
                or job_probe.tenant_id != lease.tenant_id
                or job_probe.run_id != lease.run_id
                or proposal_probe is None
            ):
                raise RuntimeConflict("stale_fencing_token")
            ticket_id = lease.ticket_id or job_probe.ticket_id
            if ticket_id is None:
                raise RuntimeConflict("stale_fencing_token")
            ticket_lock = await self.session.scalar(
                select(SupportTicket)
                .where(
                    SupportTicket.tenant_id == lease.tenant_id,
                    SupportTicket.id == ticket_id,
                )
                .with_for_update()
            )
            if ticket_lock is None:
                raise RuntimeConflict("interrupt_not_finalizable")
            resource_type = ACTION_RESOURCE_TYPES.get(proposal_probe.action_type)
            if resource_type is None:
                raise RuntimeConflict("interrupt_not_finalizable")
            active_statement = (
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.tenant_id == lease.tenant_id,
                    ApprovalRequest.customer_id == ticket_lock.customer_id,
                    ApprovalRequest.action_type == proposal_probe.action_type,
                    ApprovalRequest.resource_type == resource_type,
                    ApprovalRequest.resource_id == proposal_probe.resource_id,
                    ApprovalRequest.status.in_(ACTIVE_APPROVAL_STATUSES),
                )
                .order_by(ApprovalRequest.id)
            )
            active_rows = list(await self.session.scalars(active_statement.with_for_update()))
            if len(active_rows) > 1:
                raise RuntimeConflict("active_approval_identity_conflict")
            active_approval = active_rows[0] if active_rows else None
            proposal_rows = list(
                await self.session.scalars(
                    select(ProposalRecord)
                    .where(
                        ProposalRecord.tenant_id == lease.tenant_id,
                        (ProposalRecord.id == proposal_id)
                        | (
                            (ProposalRecord.action_type == proposal_probe.action_type)
                            & (ProposalRecord.resource_id == proposal_probe.resource_id)
                            & ProposalRecord.status.in_(("draft", "bound"))
                        ),
                    )
                    .order_by(ProposalRecord.id)
                    .with_for_update()
                )
            )
            proposal_lock = next(
                (row for row in proposal_rows if row.id == proposal_id),
                None,
            )
            run_lock = await self.session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == lease.tenant_id,
                    AgentRun.id == lease.run_id,
                    AgentRun.ticket_id == ticket_id,
                )
                .with_for_update()
            )
            if run_lock is not None and run_lock.turn_id:
                await self.session.scalar(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.tenant_id == lease.tenant_id,
                        ConversationTurn.id == run_lock.turn_id,
                        ConversationTurn.ticket_id == ticket_id,
                    )
                    .with_for_update()
                )
            job_lock = await self.session.scalar(
                select(RuntimeJob)
                .where(
                    RuntimeJob.tenant_id == lease.tenant_id,
                    RuntimeJob.id == lease.job_id,
                    RuntimeJob.run_id == lease.run_id,
                    RuntimeJob.ticket_id == ticket_id,
                )
                .with_for_update()
            )
            if ticket_lock is None or proposal_lock is None or run_lock is None or job_lock is None:
                raise RuntimeConflict("interrupt_not_finalizable")
            if (
                proposal_lock.action_type != proposal_probe.action_type
                or proposal_lock.resource_id != proposal_probe.resource_id
                or proposal_lock.resource_version != proposal_probe.resource_version
                or proposal_lock.run_id != proposal_probe.run_id
            ):
                raise RuntimeConflict("interrupt_not_finalizable")
            # All rows are already locked in the frozen order.  Reuse the
            # ordinary fence validator for lease time and active-run checks.
            await RuntimeJobRepository(self.session).assert_fence(lease)
            if active_approval is None:
                # A corrected concurrent interrupt may have committed its
                # Approval while this transaction waited on the Proposal set.
                # The Proposal locks now serialize further creation.  Observe
                # that winner without acquiring an Approval after Proposal.
                active_approval = await self.session.scalar(
                    active_statement.execution_options(populate_existing=True)
                )

        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.tenant_id == lease.tenant_id,
                SupportTicket.id == ticket_id,
            )
            .execution_options(populate_existing=True)
        )
        proposal = await self.session.scalar(
            select(ProposalRecord)
            .where(
                ProposalRecord.tenant_id == lease.tenant_id,
                ProposalRecord.id == proposal_id,
            )
            .execution_options(populate_existing=True)
        )
        run = await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.tenant_id == lease.tenant_id,
                AgentRun.id == lease.run_id,
            )
            .execution_options(populate_existing=True)
        )
        if is_restricted_worker:
            active_approval = (
                await self.session.scalar(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == lease.tenant_id,
                        ApprovalRequest.id == active_approval_id,
                    )
                    .execution_options(populate_existing=True)
                )
                if active_approval_id is not None
                else None
            )
        resource_type = (
            ACTION_RESOURCE_TYPES.get(proposal.action_type) if proposal is not None else None
        )
        if (
            ticket is None
            or proposal is None
            or run is None
            or run.ticket_id != ticket.id
            or proposal.run_id != run.id
            or proposal.status != "draft"
            or resource_type is None
        ):
            raise RuntimeConflict("interrupt_not_finalizable")
        if active_approval is not None and (
            active_approval.tenant_id != lease.tenant_id
            or active_approval.customer_id != ticket.customer_id
            or active_approval.action_type != proposal.action_type
            or active_approval.resource_type != resource_type
            or active_approval.resource_id != proposal.resource_id
            or active_approval.status not in ACTIVE_APPROVAL_STATUSES
        ):
            raise RuntimeConflict("active_approval_identity_conflict")
        return ticket, proposal, run, active_approval

    @staticmethod
    def _log_finalizer_payload_mismatch(
        lease: JobLease,
        *,
        marker: CheckpointCommitMarker,
        persisted: FinalizerPayloadV2,
        rebuilt: FinalizerPayloadV2,
    ) -> None:
        logger.error(
            "finalizer_payload_rebuild_mismatch",
            extra={
                "event": "finalizer_payload_rebuild_mismatch",
                "tenant_id": lease.tenant_id,
                "run_id": lease.run_id,
                "job_id": lease.job_id,
                "marker_id": marker.id,
                "segment_kind": marker.segment_kind,
                "mismatch_paths": finalizer_payload_mismatch_paths(persisted, rebuilt),
                "persisted_payload_hash": persisted.payload_hash,
                "rebuilt_payload_hash": rebuilt.payload_hash,
            },
        )

    async def _publish_message(
        self,
        *,
        ticket: SupportTicket,
        run: AgentRun,
        kind: str,
        content: str,
        publication_key: str,
        approval_id: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            session_user = await self.session.scalar(text("SELECT session_user"))
            if session_user == "supportguard_worker":
                value = await self.session.scalar(
                    text(
                        "SELECT supportguard_worker_publish_conversation_message("
                        "CAST(:request AS jsonb))"
                    ),
                    {
                        "request": json.dumps(
                            {
                                "run_id": run.id,
                                "message_id": new_id("msg"),
                                "kind": kind,
                                "content": redact_pii(content).text,
                                "publication_key": publication_key,
                                "approval_id": approval_id,
                                "source_refs": (source_refs or [])[:3],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                )
                if not isinstance(value, dict) or value.get("error_code"):
                    raise RuntimeConflict(
                        str((value or {}).get("error_code", "message_publish_failed"))
                    )
                return
        existing = await self.session.scalar(
            select(TicketMessage).where(
                TicketMessage.tenant_id == ticket.tenant_id,
                TicketMessage.publication_key == publication_key,
            )
        )
        if existing is not None:
            return
        ticket.next_message_sequence += 1
        message = TicketMessage(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            turn_id=run.turn_id,
            run_id=run.id,
            approval_id=approval_id,
            conversation_sequence=ticket.next_message_sequence,
            message_kind=kind,
            publication_key=publication_key,
            role="assistant" if kind == "assistant" else "action",
            content=redact_pii(content).text,
            source_refs=(source_refs or [])[:3],
        )
        self.session.add(message)
        advance_conversation_activity(ticket)
        await self.session.flush()
