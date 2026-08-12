from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import (
    AgentRunStore,
    CanonicalEventHeadConflict,
    verify_ticket_event_chain,
)
from supportguard.contracts.finalizer import canonical_hash
from supportguard.contracts.timestamps import (
    format_canonical_utc_timestamp,
    parse_canonical_utc_timestamp,
)
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    AuditEvent,
    CheckpointCommitMarker,
    ConversationTurn,
    HumanDecision,
    IdempotencyRequest,
    OutboxEvent,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.observability.metrics import APPROVAL_DECISIONS
from supportguard.services.approval_edits import (
    ApprovalEditNotAllowed,
    apply_approval_edit,
)
from supportguard.services.approval_lifecycle import ActionLifecycleService
from supportguard.services.approver_scope import assert_active_approver_scope
from supportguard.services.business import action_hash
from supportguard.services.commands import activate_next_turn
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.runtime_jobs import (
    IdempotencyRepository,
    RuntimeConflict,
)


@dataclass(frozen=True)
class DecisionAccepted:
    approval_id: str
    ticket_id: str
    run_id: str
    job_id: str | None
    decision: str
    accepted_at: datetime
    reused: bool

    def response(self) -> dict[str, object]:
        return {
            "schema_version": "decision-accepted.v1",
            "approval_id": self.approval_id,
            "ticket_id": self.ticket_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "decision": self.decision,
            "accepted_at": format_canonical_utc_timestamp(self.accepted_at),
            "status": "decision_accepted",
            "status_url": f"/api/runs/{self.run_id}" if self.job_id else None,
            "events_url": f"/api/tickets/{self.ticket_id}/events/stream",
            "reused": self.reused,
        }


class ApprovalCommandCoordinator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def decide(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        decision: str,
        actor_id: str,
        idempotency_key: str,
        reason: str,
        trace_id: str,
        approver_note: str | None = None,
        edited_payload: dict[str, object] | None = None,
    ) -> DecisionAccepted:
        if decision == "manual_takeover":
            raise RuntimeConflict("manual_takeover_public_unsupported")
        if self.session.get_bind().dialect.name == "postgresql":
            request = {
                "schema_version": "api-accept-approval-decision.v1",
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "approver_note": approver_note or "",
                "edited_payload": edited_payload or {},
                "idempotency_id": new_id("idem"),
                "revision_id": new_id("revision"),
                "decision_id": new_id("decision"),
                "event_id": new_id("event"),
                "job_id": new_id("job"),
                "outbox_id": new_id("outbox"),
                "delivery_id": new_id("delivery"),
                "audit_id": new_id("audit"),
                "trace_id": trace_id,
            }
            try:
                value = await self.session.scalar(
                    text(
                        "SELECT supportguard_api_accept_conversation_approval_decision("
                        ":approval_id,:decision,CAST(:request AS jsonb))"
                    ),
                    {
                        "approval_id": approval_id,
                        "decision": decision,
                        "request": json.dumps(request, sort_keys=True, separators=(",", ":")),
                    },
                )
            except sqlalchemy_exc.DBAPIError as exc:
                if "upgrade_in_progress" in str(exc.orig):
                    raise RuntimeConflict("upgrade_in_progress") from exc
                raise
            if not isinstance(value, dict):
                raise RuntimeError("api_accept_approval_decision_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            approval_view = await self.session.scalar(
                text("SELECT supportguard_api_get_approval(:approval_id)"),
                {"approval_id": approval_id},
            )
            if not isinstance(approval_view, dict) or not approval_view.get("ticket_id"):
                raise RuntimeError("api_get_approval_navigation_invalid")
            accepted = DecisionAccepted(
                approval_id=str(value["approval_id"]),
                ticket_id=str(approval_view["ticket_id"]),
                run_id=str(value["run_id"]),
                job_id=(str(value["job_id"]) if value.get("job_id") is not None else None),
                decision=str(value["decision"]),
                accepted_at=parse_canonical_utc_timestamp(value["accepted_at"]),
                reused=bool(value["reused"]),
            )
            APPROVAL_DECISIONS.labels(accepted.decision, str(accepted.reused).lower()).inc()
            return accepted
        approval = await self.session.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if approval is None:
            raise RuntimeConflict("approval_not_found")
        await assert_active_approver_scope(
            self.session, tenant_id=approval.tenant_id, actor_id=actor_id
        )
        route = f"POST /api/approvals/{approval_id}/decision"
        idempotency = await IdempotencyRepository(self.session).accept(
            tenant_id=approval.tenant_id,
            principal_id=actor_id,
            route=route,
            key=idempotency_key,
            payload={
                "decision": decision,
                "reason": reason,
                "approver_note": approver_note,
                "edited_payload": edited_payload or {},
            },
            resource_ids={},
            response_snapshot={},
            expires_at=None,
        )
        if idempotency.reused and idempotency.record.response_snapshot:
            snapshot = idempotency.record.response_snapshot
            accepted = DecisionAccepted(
                approval_id=str(snapshot["approval_id"]),
                ticket_id=approval.ticket_id,
                run_id=str(snapshot["run_id"]),
                job_id=(str(snapshot["job_id"]) if snapshot.get("job_id") is not None else None),
                decision=str(snapshot["decision"]),
                accepted_at=parse_canonical_utc_timestamp(snapshot["accepted_at"]),
                reused=True,
            )
            APPROVAL_DECISIONS.labels(accepted.decision, "true").inc()
            return accepted
        existing_decision = await self.session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        existing_job = await self.session.scalar(
            select(RuntimeJob).where(RuntimeJob.approval_id == approval.id)
        )
        if existing_decision is not None:
            if existing_decision.decision != decision or (
                decision != "reject" and existing_job is None
            ):
                raise RuntimeConflict("approval_decision_conflict")
            accepted_at = existing_decision.created_at
            if (
                accepted_at.tzinfo is None
                and self.session.get_bind().dialect.name == "sqlite"
            ):
                # SQLite drops timezone metadata even for aware DateTime
                # columns. Production PostgreSQL values remain authoritative.
                accepted_at = accepted_at.replace(tzinfo=UTC)
            accepted = DecisionAccepted(
                approval.id,
                approval.ticket_id,
                str(approval.run_id),
                existing_job.id if decision != "reject" and existing_job is not None else None,
                decision,
                accepted_at,
                reused=True,
            )
            self._store_idempotent_response(idempotency.record, accepted)
            APPROVAL_DECISIONS.labels(accepted.decision, "true").inc()
            return accepted
        run, ticket, proposal, marker = await self._validate_binding(approval)
        if decision not in {"approve", "edit_and_approve", "reject"}:
            raise RuntimeConflict("invalid_approval_decision")
        revision = await self.session.get(
            ApprovalActionRevision, approval.selected_revision_id or ""
        )
        if (
            revision is None
            or revision.approval_id != approval.id
            or revision.revision_number != approval.selected_revision_number
            or revision.revision_number != 0
            or proposal.action_type != approval.action_type
            or revision.action_payload != proposal.action_payload
            or revision.action_hash != proposal.action_hash
            or approval.action_hash != proposal.action_hash
        ):
            raise RuntimeConflict("approval_revision_binding_conflict")
        if (decision == "edit_and_approve") is not bool(edited_payload):
            raise RuntimeConflict("approval_edit_not_allowed")
        if edited_payload:
            try:
                payload = apply_approval_edit(
                    action_type=approval.action_type,
                    base_payload=dict(revision.action_payload),
                    edited_payload=edited_payload,
                )
            except ApprovalEditNotAllowed as exc:
                raise RuntimeConflict("approval_edit_not_allowed") from exc
            revision = ApprovalActionRevision(
                id=new_id("revision"),
                tenant_id=approval.tenant_id,
                approval_id=approval.id,
                proposal_id=proposal.id,
                snapshot_id=revision.snapshot_id,
                revision_number=revision.revision_number + 1,
                action_payload=payload,
                action_hash=action_hash(payload),
                resource_version=revision.resource_version,
                created_by_ref=actor_id,
                revision_reason="edit_and_approve",
            )
            self.session.add(revision)
            approval.selected_revision_id = revision.id
            approval.selected_revision_number = revision.revision_number
        persisted_status = {
            "approve": "approved",
            "edit_and_approve": "approved",
            "reject": "rejected",
        }[decision]
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status=persisted_status,
            expected_status="pending",
            expected_version=approval.status_version,
            decided_at=datetime.now(UTC),
        )
        decision_hash = canonical_hash(
            {
                "approval_id": approval.id,
                "actor_id": actor_id,
                "decision": decision,
                "reason": reason,
                "approver_note": approver_note or "",
                "action_hash": revision.action_hash,
                "revision_id": revision.id,
                "revision_number": revision.revision_number,
            }
        )
        human = HumanDecision(
            id=new_id("decision"),
            tenant_id=approval.tenant_id,
            approval_id=approval.id,
            actor_id=actor_id,
            decision=decision,
            reason=reason,
            action_revision_id=revision.id,
            action_hash=revision.action_hash,
            decision_hash=decision_hash,
            audit_metadata={"approver_note": approver_note or ""},
        )
        job: RuntimeJob | None = None
        if decision != "reject":
            ticket.next_dispatch_sequence += 1
            job = RuntimeJob(
                id=new_id("job"),
                tenant_id=approval.tenant_id,
                run_id=run.id,
                kind="approval_resume",
                ticket_id=approval.ticket_id,
                dispatch_sequence=ticket.next_dispatch_sequence,
                approval_id=approval.id,
            )
            self.session.add(job)
            await self.session.flush()
        current_head = await self.session.scalar(
            select(AgentEvent)
            .where(AgentEvent.ticket_id == ticket.id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        if current_head is None:
            raise RuntimeConflict("event_chain_conflict")
        try:
            decision_event = await AgentRunStore(self.session).append_event(
                run,
                event_type="human_decision_accepted",
                payload={
                    "approval_id": approval.id,
                    "human_decision_id": human.id,
                    "decision": decision,
                    "decision_hash": decision_hash,
                    "action_hash": revision.action_hash,
                    "revision_id": revision.id,
                    "revision_number": revision.revision_number,
                },
                visibility="approver",
                idempotency_id=idempotency_key,
                expected_ticket_head_event_id=current_head.id,
                expected_ticket_sequence=current_head.ticket_sequence,
                expected_ticket_event_hash=current_head.event_hash,
            )
        except CanonicalEventHeadConflict as exc:
            raise RuntimeConflict("approval_expected_head_conflict") from exc
        human.canonical_event_id = decision_event.id
        human.canonical_event_hash = decision_event.event_hash
        # Human decisions are append-only.  Populate the canonical event binding
        # before the first INSERT so the API role never needs UPDATE authority.
        self.session.add(human)
        if job is not None:
            self.session.add(
                OutboxEvent(
                    id=new_id("outbox"),
                    delivery_id=new_id("delivery"),
                    tenant_id=approval.tenant_id,
                    job_id=job.id,
                    run_id=run.id,
                    event_type="runtime_job_available",
                    payload={"traceparent": trace_id},
                )
            )
            run.status = "queued"
            run.status_version += 1
            ticket.status = "queued"
            ticket.version += 1
        else:
            await self._converge_rejected_decision(
                approval=approval,
                proposal=proposal,
                run=run,
                ticket=ticket,
                trace_id=trace_id,
            )
        self.session.add(
            AuditEvent(
                tenant_id=approval.tenant_id,
                ticket_id=ticket.id,
                customer_id=approval.customer_id,
                event_type="approval_decision_accepted",
                actor_type="approver",
                actor_id=actor_id,
                payload={
                    "approval_id": approval.id,
                    "decision": decision,
                    "proposal_id": proposal.id,
                    "marker_id": marker.id,
                    "job_id": job.id if job is not None else None,
                    "approver_note": approver_note or "",
                },
                trace_id=trace_id,
                run_id=run.id,
            )
        )
        await self.session.flush()
        accepted_at = human.created_at
        if accepted_at.tzinfo is None and self.session.get_bind().dialect.name == "sqlite":
            # SQLite drops timezone metadata even for UTC-aware DateTime columns.
            # Production PostgreSQL values must remain aware and otherwise fail closed.
            accepted_at = accepted_at.replace(tzinfo=UTC)
        accepted = DecisionAccepted(
            approval.id,
            ticket.id,
            run.id,
            job.id if job is not None else None,
            decision,
            accepted_at,
            reused=False,
        )
        self._store_idempotent_response(idempotency.record, accepted)
        await self.session.flush()
        APPROVAL_DECISIONS.labels(decision, "false").inc()
        return accepted

    async def _converge_rejected_decision(
        self,
        *,
        approval: ApprovalRequest,
        proposal: ProposalRecord,
        run: AgentRun,
        ticket: SupportTicket,
        trace_id: str,
    ) -> None:
        """Finish a reject synchronously; a no-action decision needs no Worker."""

        now = datetime.now(UTC)
        proposal.status = "stale"
        run.status = "completed"
        run.checkpoint_stage = "completed"
        run.agent_finish_reason = "rejected"
        run.error_code = None
        run.active_job_id = None
        run.active_fencing_token = None
        run.completed_at = now
        run.status_version += 1
        turn = (
            await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
            if run.turn_id
            else None
        )
        if turn is not None:
            turn.activity_state = "completed"
            turn.result_state = "refused"
            turn.completed_at = now
        ticket.status = "rejected"
        ticket.final_response = (
            "审批者已拒绝这项申请，因此没有执行任何业务操作。你仍可以在当前对话继续咨询。"
        )
        ticket.version += 1
        existing_update = await self.session.scalar(
            select(TicketMessage.id).where(
                TicketMessage.tenant_id == approval.tenant_id,
                TicketMessage.publication_key == f"approval:{approval.id}:rejected",
            )
        )
        if existing_update is None:
            ticket.next_message_sequence += 1
            advance_conversation_activity(ticket, occurred_at=now)
            self.session.add(
                TicketMessage(
                    id=new_id("msg"),
                    tenant_id=approval.tenant_id,
                    ticket_id=ticket.id,
                    turn_id=run.turn_id,
                    run_id=run.id,
                    approval_id=approval.id,
                    conversation_sequence=ticket.next_message_sequence,
                    message_kind="action_update",
                    publication_key=f"approval:{approval.id}:rejected",
                    role="action",
                    content=ticket.final_response,
                    source_refs=[],
                )
            )
        await self.session.flush()
        await activate_next_turn(
            self.session,
            ticket=ticket,
            trace_id=f"{trace_id}:next-turn",
        )
        await ActionLifecycleService(self.session).converge_ticket(
            ticket,
            default_status="rejected",
        )

    @staticmethod
    def _store_idempotent_response(record: IdempotencyRequest, accepted: DecisionAccepted) -> None:
        record.resource_ids = {
            "approval_id": accepted.approval_id,
            "ticket_id": accepted.ticket_id,
            "run_id": accepted.run_id,
        }
        if accepted.job_id is not None:
            record.resource_ids["job_id"] = accepted.job_id
        record.response_snapshot = {
            "schema_version": "decision-accepted.v1",
            "approval_id": accepted.approval_id,
            "ticket_id": accepted.ticket_id,
            "run_id": accepted.run_id,
            "job_id": accepted.job_id,
            "decision": accepted.decision,
            "accepted_at": format_canonical_utc_timestamp(accepted.accepted_at),
            "status": "decision_accepted",
            "status_url": (f"/api/runs/{accepted.run_id}" if accepted.job_id is not None else None),
            "events_url": f"/api/tickets/{accepted.ticket_id}/events/stream",
            "reused": False,
        }
        record.completed_at = accepted.accepted_at
        record.retention_class = "protected_action"

    async def _validate_binding(
        self, approval: ApprovalRequest
    ) -> tuple[AgentRun, SupportTicket, ProposalRecord, CheckpointCommitMarker]:
        if not approval.run_id or not approval.proposal_id or not approval.marker_id:
            raise RuntimeConflict("checkpoint_binding_conflict")
        run = await self.session.scalar(
            select(AgentRun)
            .where(AgentRun.id == approval.run_id, AgentRun.tenant_id == approval.tenant_id)
            .with_for_update()
        )
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == approval.ticket_id,
                SupportTicket.tenant_id == approval.tenant_id,
            )
            .with_for_update()
        )
        proposal = await self.session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.id == approval.proposal_id,
                ProposalRecord.tenant_id == approval.tenant_id,
            )
        )
        marker = await self.session.scalar(
            select(CheckpointCommitMarker).where(
                CheckpointCommitMarker.id == approval.marker_id,
                CheckpointCommitMarker.tenant_id == approval.tenant_id,
            )
        )
        if (
            approval.status != "pending"
            or run is None
            or ticket is None
            or proposal is None
            or marker is None
            or proposal.status != "bound"
            or marker.status != "finalized"
            or run.status != "interrupted"
            or run.canonical_checkpoint_ns != approval.canonical_checkpoint_ns
            or run.canonical_checkpoint_id != approval.checkpoint_id
            or run.canonical_checkpoint_hash != approval.canonical_checkpoint_hash
            or marker.final_checkpoint_id != approval.checkpoint_id
            or marker.final_checkpoint_hash != approval.canonical_checkpoint_hash
            or run.canonical_checkpoint_version != approval.checkpoint_version
            or marker.final_checkpoint_version != approval.checkpoint_version
            or approval.expected_ticket_sequence is None
        ):
            raise RuntimeConflict("checkpoint_binding_conflict")
        try:
            await verify_ticket_event_chain(self.session, ticket.id)
        except RuntimeError as exc:
            raise RuntimeConflict("event_chain_conflict") from exc
        frozen_head = await self.session.scalar(
            select(AgentEvent).where(
                AgentEvent.ticket_id == ticket.id,
                AgentEvent.id == approval.expected_ticket_head_event_id,
                AgentEvent.ticket_sequence == approval.expected_ticket_sequence,
                AgentEvent.event_hash == approval.expected_ticket_event_hash,
            )
        )
        if frozen_head is None:
            raise RuntimeConflict("approval_expected_head_conflict")
        return run, ticket, proposal, marker

    async def mark_binding_stale(
        self, *, tenant_id: str, approval_id: str
    ) -> dict[str, object]:
        """Converge an invalid binding after the decision transaction rolls back.

        Production PostgreSQL must use the narrow owner capability because the
        API role intentionally has no direct lifecycle-table write authority.
        The SQLite branch mirrors the minimum state transition used by
        deterministic unit tests.
        """
        if self.session.get_bind().dialect.name == "postgresql":
            value = await self.session.scalar(
                text(
                    "SELECT supportguard_api_converge_checkpoint_binding_stale("
                    ":approval_id)"
                ),
                {"approval_id": approval_id},
            )
            if not isinstance(value, dict):
                raise RuntimeError("checkpoint_binding_stale_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            if (
                value.get("schema_version") != "checkpoint-binding-stale.v1"
                or value.get("approval_id") != approval_id
                or value.get("status") != "stale"
            ):
                raise RuntimeError("checkpoint_binding_stale_capability_invalid")
            return value
        approval = await self.session.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.status == "pending",
            )
            .with_for_update()
        )
        if approval is None:
            return {
                "schema_version": "checkpoint-binding-stale.v1",
                "approval_id": approval_id,
                "status": "stale",
                "reused": True,
            }
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="stale",
            expected_status="pending",
            expected_version=approval.status_version,
        )
        if approval.proposal_id:
            proposal = await self.session.scalar(
                select(ProposalRecord).where(
                    ProposalRecord.id == approval.proposal_id,
                    ProposalRecord.tenant_id == tenant_id,
                )
            )
            if proposal is not None and proposal.status in {"draft", "bound"}:
                proposal.status = "stale"
        await self.session.flush()
        return {
            "schema_version": "checkpoint-binding-stale.v1",
            "approval_id": approval_id,
            "status": "stale",
            "reused": False,
        }
