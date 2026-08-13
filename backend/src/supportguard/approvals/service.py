from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.finalizer import canonical_hash
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    AuditEvent,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    ProposalRecord,
    SupportTicket,
)
from supportguard.services.actions import (
    RuntimeActionExecutor,
)
from supportguard.services.approval_lifecycle import (
    ActionLifecycleService,
    activate_next_turn_and_converge_ticket,
)
from supportguard.services.business import REFUND_LIMIT_USD, action_hash
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.refunds import (
    bind_refund_pair_to_proposal,
    lock_and_evaluate_billing_refund_pair,
    refund_pair_action_fields,
)
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict


class ApprovalDecisionResult(BaseModel):
    approval_id: str
    status: str
    action_hash: str


class RefundExecutionResult(BaseModel):
    approval_id: str
    business_action_id: str | None
    billing_record_id: str
    amount: Decimal
    currency: str
    status: Literal["succeeded", "stale"]
    idempotency_key: str
    reused: bool
    reason: str | None = None


class ApprovalService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def validate_actionable(self, approval_id: str) -> ApprovalRequest:
        approval, _, _ = await self._load_actionable(approval_id)
        return approval

    async def decide(
        self,
        approval_id: str,
        *,
        decision: Literal["approve", "reject", "manual_takeover"],
        approver_id: str,
        reason: str,
        trace_id: str,
        approver_note: str | None = None,
    ) -> ApprovalDecisionResult:
        if decision == "manual_takeover":
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "New manual-takeover decisions are not supported",
            )
        approval, run, ticket = await self._load_actionable(approval_id)
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status={"approve": "approved", "reject": "rejected"}[decision],
            expected_status="pending",
            expected_version=approval.status_version,
            decided_at=datetime.now(UTC),
        )
        approval.approver_id = approver_id
        approval.decision_reason = reason
        approval.approver_note = approver_note
        human = HumanDecision(
            tenant_id=approval.tenant_id,
            approval_id=approval.id,
            actor_id=approver_id,
            decision=decision,
            reason=reason,
            action_hash=approval.action_hash,
            decision_hash=self._decision_hash(
                approval,
                actor_id=approver_id,
                decision=decision,
                reason=reason,
                approver_note=approver_note,
            ),
            audit_metadata={"approver_note": approver_note or ""},
        )
        self.session.add(human)
        await self._append_legacy_decision_event(approval, run, human)
        if decision != "approve":
            now = datetime.now(UTC)
            if approval.proposal_id:
                proposal = await self.session.get(ProposalRecord, approval.proposal_id)
                if proposal is not None:
                    proposal.status = "stale"
            run.status = "completed"
            run.checkpoint_stage = "completed"
            run.agent_finish_reason = "rejected"
            run.error_code = None
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
                    turn.result_state = "rejected"
                    turn.completed_at = now
            await activate_next_turn_and_converge_ticket(
                self.session,
                ticket=ticket,
                trace_id=f"{trace_id}:next-turn",
                default_status="rejected",
            )
        self._audit(
            approval,
            f"approval_{decision}",
            {"reason": reason, "approver_note": approver_note or ""},
            actor_id=approver_id,
            trace_id=trace_id,
        )
        await self.session.flush()
        return ApprovalDecisionResult(
            approval_id=approval.id,
            status=approval.status,
            action_hash=approval.action_hash,
        )

    async def edit_and_approve(
        self,
        approval_id: str,
        *,
        approver_id: str,
        refund_reason: str,
        approver_note: str | None,
        trace_id: str,
    ) -> ApprovalDecisionResult:
        approval, _, _ = await self._load_actionable(approval_id)
        proposal = await self.session.get(ProposalRecord, approval.proposal_id or "")
        if proposal is None:
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Approval is not bound to a durable proposal",
            )
        target_id = str(approval.action_payload["billing_record_id"])
        billing, pair = await lock_and_evaluate_billing_refund_pair(
            self.session,
            tenant_id=approval.tenant_id,
            customer_id=approval.customer_id,
            billing_record_id=target_id,
            now=datetime.now(UTC),
        )
        if billing is None:
            raise DomainError(ErrorCode.BILLING_RECORD_NOT_FOUND, "Billing record was not found")
        if billing.customer_id != approval.customer_id:
            raise DomainError(ErrorCode.BILLING_SCOPE_VIOLATION, "Billing record is out of scope")
        if (
            billing.status != "charged"
            or billing.amount > REFUND_LIMIT_USD
            or pair is None
            or not pair.eligible
        ):
            raise DomainError(ErrorCode.BILLING_NOT_CHARGED, "Billing record is not eligible")
        payload: dict[str, str | int] = {
            "billing_record_id": billing.id,
            "customer_id": approval.customer_id,
            "amount": str(billing.amount),
            "currency": billing.currency,
            "refund_reason": refund_reason,
            "business_version": billing.version,
            **refund_pair_action_fields(pair),
        }
        approval.action_payload = payload
        approval.action_hash = action_hash(payload)
        approval.business_version = billing.version
        bind_refund_pair_to_proposal(proposal, pair)
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="approved",
            expected_status="pending",
            expected_version=approval.status_version,
            decided_at=datetime.now(UTC),
        )
        approval.approver_id = approver_id
        approval.decision_reason = "refund_reason_edited_and_approved"
        approval.approver_note = approver_note
        human = HumanDecision(
            tenant_id=approval.tenant_id,
            approval_id=approval.id,
            actor_id=approver_id,
            decision="edit_and_approve",
            reason="refund_reason_edited_and_approved",
            action_hash=approval.action_hash,
            decision_hash=self._decision_hash(
                approval,
                actor_id=approver_id,
                decision="edit_and_approve",
                reason="refund_reason_edited_and_approved",
                approver_note=approver_note,
            ),
            audit_metadata={"approver_note": approver_note or ""},
        )
        self.session.add(human)
        await self._append_legacy_decision_event(
            approval, await self.session.get(AgentRun, approval.run_id or ""), human
        )
        self._audit(
            approval,
            "approval_edit_and_approve",
            {
                "billing_record_id": billing.id,
                "refund_reason": refund_reason,
                "approver_note": approver_note or "",
            },
            actor_id=approver_id,
            trace_id=trace_id,
        )
        await self.session.flush()
        return ApprovalDecisionResult(
            approval_id=approval.id,
            status=approval.status,
            action_hash=approval.action_hash,
        )

    async def _load_actionable(
        self, approval_id: str
    ) -> tuple[ApprovalRequest, AgentRun, SupportTicket]:
        approval = await self.session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update()
        )
        if approval is None:
            raise DomainError(ErrorCode.APPROVAL_NOT_FOUND, "Approval was not found")
        if approval.status != "pending":
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "Approval is no longer pending",
                details={"status": approval.status},
            )
        if approval.run_id is None or approval.checkpoint_id is None:
            await ActionLifecycleService(self.session).transition(
                approval,
                to_status="stale",
                expected_status="pending",
                expected_version=approval.status_version,
            )
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Approval is not bound to a resumable Agent Run",
            )
        run = await self.session.get(AgentRun, approval.run_id)
        ticket = await self.session.get(SupportTicket, approval.ticket_id)
        if (
            run is None
            or ticket is None
            or run.status != "interrupted"
            or run.checkpoint_stage != "awaiting_approval"
            or run.checkpoint_id != approval.checkpoint_id
        ):
            await ActionLifecycleService(self.session).transition(
                approval,
                to_status="stale",
                expected_status="pending",
                expected_version=approval.status_version,
            )
            raise DomainError(
                ErrorCode.CHECKPOINT_NOT_INTERRUPTED,
                "Approval does not match an active approval interrupt",
            )
        return approval, run, ticket

    @staticmethod
    def _decision_hash(
        approval: ApprovalRequest,
        *,
        actor_id: str,
        decision: str,
        reason: str,
        approver_note: str | None,
    ) -> str:
        return canonical_hash(
            {
                "approval_id": approval.id,
                "actor_id": actor_id,
                "decision": decision,
                "reason": reason,
                "approver_note": approver_note or "",
                "action_hash": approval.action_hash,
            }
        )

    async def _append_legacy_decision_event(
        self,
        approval: ApprovalRequest,
        run: AgentRun | None,
        human: HumanDecision,
    ) -> None:
        """Testing-only synchronous runtime still emits the canonical audit fact."""
        if run is None or not human.decision_hash:
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Decision has no bound Agent Run",
            )
        await self.session.flush()
        kwargs: dict[str, object] = {}
        if approval.expected_ticket_sequence is not None:
            kwargs = {
                "expected_ticket_head_event_id": approval.expected_ticket_head_event_id,
                "expected_ticket_sequence": approval.expected_ticket_sequence,
                "expected_ticket_event_hash": approval.expected_ticket_event_hash,
            }
        event = await AgentRunStore(self.session).append_event(
            run,
            event_type="human_decision_accepted",
            payload={
                "approval_id": approval.id,
                "human_decision_id": human.id,
                "decision": human.decision,
                "decision_hash": human.decision_hash,
                "action_hash": human.action_hash,
            },
            visibility="approver",
            **kwargs,  # type: ignore[arg-type]
        )
        human.canonical_event_id = event.id
        human.canonical_event_hash = event.event_hash
        approval.expected_ticket_head_event_id = event.id
        approval.expected_ticket_sequence = event.ticket_sequence
        approval.expected_ticket_event_hash = event.event_hash

    def _audit(
        self,
        approval: ApprovalRequest,
        event_type: str,
        payload: dict[str, str],
        *,
        actor_id: str,
        trace_id: str,
    ) -> None:
        self.session.add(
            AuditEvent(
                tenant_id=approval.tenant_id,
                ticket_id=approval.ticket_id,
                customer_id=approval.customer_id,
                event_type=event_type,
                actor_type="approver",
                actor_id=actor_id,
                payload={"approval_id": approval.id, **payload},
                trace_id=trace_id,
                run_id=approval.run_id,
                created_at=datetime.now(UTC),
            )
        )


class RefundRuntime:
    """Runtime-only command. This class is never registered as an Agent or MCP tool."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.session = session
        self.test_capability = test_capability

    async def execute_refund(
        self,
        approval_id: str,
        *,
        idempotency_key: str,
        trace_id: str,
        lease: JobLease | None = None,
    ) -> RefundExecutionResult:
        executor = RuntimeActionExecutor(
            self.session,
            test_capability=self.test_capability,
        )
        if lease is None:
            result = await executor.execute_legacy_refund(
                approval_id=approval_id,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
        else:
            approval = await self.session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            if approval is None:
                raise DomainError(ErrorCode.APPROVAL_NOT_FOUND, "Approval was not found")
            if approval.idempotency_key != idempotency_key:
                raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key mismatch")
            try:
                result = await executor.execute(
                    lease,
                    approval_id=approval_id,
                    trace_id=trace_id,
                )
            except RuntimeConflict as exc:
                raise DomainError(
                    ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                    "Runtime Action execution binding failed",
                ) from exc
        approval = await self.session.get(ApprovalRequest, approval_id)
        if approval is None:
            raise DomainError(ErrorCode.APPROVAL_NOT_FOUND, "Approval was not found")
        revision = await self.session.get(
            ApprovalActionRevision,
            approval.selected_revision_id or "",
        )
        payload = dict(revision.action_payload if revision is not None else approval.action_payload)
        if result.status == "stale":
            return RefundExecutionResult(
                approval_id=approval.id,
                business_action_id=None,
                billing_record_id=result.resource_id,
                amount=Decimal(str(payload.get("amount", "0"))),
                currency=str(payload.get("currency", "USD")),
                status="stale",
                idempotency_key=approval.idempotency_key,
                reused=result.reused,
                reason=result.reason,
            )
        action = await self.session.get(BusinessAction, result.business_action_id or "")
        if action is None:
            raise DomainError(
                ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                "Runtime Action did not persist its effect",
            )
        return self._result(action, approval.id, reused=result.reused)

    @staticmethod
    def _result(action: BusinessAction, approval_id: str, *, reused: bool) -> RefundExecutionResult:
        return RefundExecutionResult(
            approval_id=approval_id,
            business_action_id=action.id,
            billing_record_id=str(action.result["billing_record_id"]),
            amount=Decimal(str(action.result["amount"])),
            currency=str(action.result["currency"]),
            status="succeeded",
            idempotency_key=action.idempotency_key,
            reused=reused,
            reason=None,
        )
