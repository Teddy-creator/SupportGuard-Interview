from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.timestamps import (
    format_canonical_utc_timestamp,
    parse_canonical_utc_timestamp,
)
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    AuditEvent,
    ConversationTurn,
    HumanDecision,
    IdempotencyRequest,
    ProposalRecord,
    ProposalWithdrawal,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.services.approval_lifecycle import ActionLifecycleService
from supportguard.services.commands import activate_next_turn
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.runtime_jobs import IdempotencyRepository, RuntimeConflict


@dataclass(frozen=True)
class WithdrawalAccepted:
    approval_id: str
    ticket_id: str
    withdrawal_id: str
    accepted_at: datetime
    reused: bool

    def response(self) -> dict[str, object]:
        return {
            "schema_version": "withdrawal-accepted.v1",
            "approval_id": self.approval_id,
            "ticket_id": self.ticket_id,
            "withdrawal_id": self.withdrawal_id,
            "accepted_at": format_canonical_utc_timestamp(self.accepted_at),
            "action_status": "withdrawn",
            "reused": self.reused,
        }


class ProposalWithdrawalCoordinator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def withdraw(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        principal_id: str,
        approval_id: str,
        idempotency_key: str,
        reason: str,
        trace_id: str,
    ) -> WithdrawalAccepted:
        if self.session.get_bind().dialect.name == "postgresql":
            value = await self.session.scalar(
                text(
                    "SELECT supportguard_api_withdraw_proposal("
                    ":approval_id,CAST(:request AS jsonb))"
                ),
                {
                    "approval_id": approval_id,
                    "request": json.dumps(
                        {
                            "schema_version": "api-withdraw-proposal.v1",
                            "customer_id": customer_id,
                            "actor_id": principal_id,
                            "idempotency_key": idempotency_key,
                            "reason": reason,
                            "withdrawal_id": new_id("withdrawal"),
                            "message_id": new_id("msg"),
                            "audit_id": new_id("audit"),
                            "idempotency_id": new_id("idem"),
                            "trace_id": trace_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            )
            if not isinstance(value, dict):
                raise RuntimeError("withdraw_proposal_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            return WithdrawalAccepted(
                approval_id=str(value["approval_id"]),
                ticket_id=str(value["ticket_id"]),
                withdrawal_id=str(value["withdrawal_id"]),
                accepted_at=parse_canonical_utc_timestamp(value["accepted_at"]),
                reused=bool(value["reused"]),
            )

        approval = await self.session.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id, ApprovalRequest.tenant_id == tenant_id)
            .with_for_update()
        )
        if approval is None or approval.customer_id != customer_id:
            raise RuntimeConflict("approval_not_found")
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == approval.ticket_id,
                SupportTicket.tenant_id == tenant_id,
                SupportTicket.customer_id == customer_id,
            )
            .with_for_update()
        )
        if ticket is None:
            raise RuntimeConflict("approval_not_found")
        route = f"POST /api/conversations/{ticket.id}/actions/{approval.id}/withdraw"
        accepted = await IdempotencyRepository(self.session).accept(
            tenant_id=tenant_id,
            principal_id=principal_id,
            route=route,
            key=idempotency_key,
            payload={"reason": reason},
            resource_ids={},
            response_snapshot={},
            expires_at=None,
        )
        if accepted.reused and accepted.record.response_snapshot:
            snapshot = accepted.record.response_snapshot
            return WithdrawalAccepted(
                approval_id=approval.id,
                ticket_id=ticket.id,
                withdrawal_id=str(snapshot["withdrawal_id"]),
                accepted_at=parse_canonical_utc_timestamp(snapshot["accepted_at"]),
                reused=True,
            )
        existing = await self.session.scalar(
            select(ProposalWithdrawal).where(
                ProposalWithdrawal.approval_id == approval.id,
                ProposalWithdrawal.tenant_id == tenant_id,
            )
        )
        if existing is not None:
            result = WithdrawalAccepted(
                approval.id, ticket.id, existing.id, existing.created_at, reused=True
            )
            self._store_response(accepted.record, result)
            return result
        decision = await self.session.scalar(
            select(HumanDecision.id).where(HumanDecision.approval_id == approval.id)
        )
        if approval.status != "pending" or decision is not None or not approval.proposal_id:
            raise RuntimeConflict("proposal_withdrawal_conflict")
        proposal = await self.session.scalar(
            select(ProposalRecord)
            .where(
                ProposalRecord.id == approval.proposal_id,
                ProposalRecord.tenant_id == tenant_id,
                ProposalRecord.status == "bound",
            )
            .with_for_update()
        )
        if proposal is None:
            raise RuntimeConflict("proposal_withdrawal_conflict")
        now = datetime.now(UTC)
        withdrawal = ProposalWithdrawal(
            id=new_id("withdrawal"),
            tenant_id=tenant_id,
            ticket_id=ticket.id,
            customer_id=customer_id,
            approval_id=approval.id,
            proposal_id=proposal.id,
            actor_id=principal_id,
            reason=reason,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="withdrawn",
            expected_status="pending",
            expected_version=approval.status_version,
            decided_at=now,
        )
        proposal.status = "stale"
        run = (
            await self.session.get(AgentRun, approval.run_id, with_for_update=True)
            if approval.run_id
            else None
        )
        if run is not None:
            run.status = "completed"
            run.checkpoint_stage = "completed"
            run.agent_finish_reason = "withdrawn"
            run.active_job_id = None
            run.active_fencing_token = None
            run.completed_at = now
            run.status_version += 1
            if run.turn_id:
                turn = await self.session.get(
                    ConversationTurn,
                    run.turn_id,
                    with_for_update=True,
                )
                if turn is not None:
                    turn.activity_state = "completed"
                    turn.result_state = "withdrawn"
                    turn.completed_at = now
        ticket.status = "open"
        ticket.version += 1
        ticket.next_message_sequence += 1
        advance_conversation_activity(ticket, occurred_at=now)
        self.session.add(withdrawal)
        self.session.add(
            TicketMessage(
                id=new_id("msg"),
                tenant_id=tenant_id,
                ticket_id=ticket.id,
                turn_id=run.turn_id if run else None,
                run_id=run.id if run else None,
                approval_id=approval.id,
                conversation_sequence=ticket.next_message_sequence,
                message_kind="action_update",
                publication_key=f"action:{approval.id}:withdrawn:1",
                role="action",
                content="你已撤回该操作申请；系统未执行任何业务动作。",
                source_refs=[],
            )
        )
        self.session.add(
            AuditEvent(
                tenant_id=tenant_id,
                ticket_id=ticket.id,
                customer_id=customer_id,
                event_type="proposal_withdrawn",
                actor_type="customer",
                actor_id=principal_id,
                payload={
                    "approval_id": approval.id,
                    "proposal_id": proposal.id,
                    "withdrawal_id": withdrawal.id,
                },
                trace_id=trace_id,
                run_id=approval.run_id,
            )
        )
        await self.session.flush()
        result = WithdrawalAccepted(
            approval.id, ticket.id, withdrawal.id, withdrawal.created_at, reused=False
        )
        self._store_response(accepted.record, result)
        await self.session.flush()
        await activate_next_turn(
            self.session,
            ticket=ticket,
            trace_id=f"{trace_id}:next-turn",
        )
        await ActionLifecycleService(self.session).converge_ticket(
            ticket,
            default_status="open",
        )
        return result

    @staticmethod
    def _store_response(record: IdempotencyRequest, result: WithdrawalAccepted) -> None:
        # Kept local to preserve the common idempotency envelope without making
        # proposal withdrawal look like an approver decision.
        record.resource_ids = {
            "approval_id": result.approval_id,
            "ticket_id": result.ticket_id,
            "withdrawal_id": result.withdrawal_id,
        }
        record.response_snapshot = result.response() | {"reused": False}
        record.completed_at = result.accepted_at
        record.retention_class = "protected_action"
