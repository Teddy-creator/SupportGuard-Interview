from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.actions.service import get_action_spec_or_none
from supportguard.agent.persistence import AgentRunStore, CanonicalEventHeadConflict
from supportguard.contracts.finalizer import canonical_hash
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    AgentRun,
    ApiKeyMetadata,
    ApprovalActionRevision,
    ApprovalRequest,
    AuditEvent,
    BillingRecord,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    PlanCatalog,
    ProposalRecord,
    Subscription,
    SupportTicket,
    ToolInvocation,
    ToolObservation,
    TurnGroup,
)
from supportguard.services.approval_edits import revision_matches_approval_edit
from supportguard.services.approval_lifecycle import (
    ActionLifecycleService,
    activate_next_turn_and_converge_ticket,
)
from supportguard.services.approver_scope import assert_execution_approver_scope
from supportguard.services.business import action_hash
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.kill_switches import assert_mutation_enabled
from supportguard.services.refunds import (
    lock_and_evaluate_billing_refund_pair,
    refund_pair_matches_proposal,
)
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository


@dataclass(frozen=True)
class ActionResult:
    business_action_id: str | None
    action_type: str
    resource_id: str
    reused: bool
    status: str = "succeeded"
    reason: str | None = None


@dataclass(frozen=True)
class _LegacyRefundBinding:
    approval: ApprovalRequest
    decision: HumanDecision
    revision: ApprovalActionRevision | None
    payload: dict[str, Any]
    payload_hash: str
    resource_version: int
    resource_id: str


class RuntimeActionCapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["runtime-action-result.v1"]
    approval_id: str
    human_decision_id: str
    business_action_id: str | None
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    resource_id: str
    status: Literal["succeeded", "stale"]
    reused: bool
    reason: (
        Literal[
            "binding_stale",
            "publication_binding_stale",
            "approver_scope_stale",
            "resource_snapshot_stale",
            "policy_stale",
            "refund_pair_execution_stale",
        ]
        | None
    )


def action_resource_id(
    action_type: str,
    action_payload: dict[str, Any],
) -> str:
    """Resolve the canonical business resource from one typed action payload."""

    action_spec = get_action_spec_or_none(action_type)
    if action_spec is None or not action_payload.get(action_spec.resource_field):
        raise RuntimeConflict("approval_resource_missing")
    return str(action_payload[action_spec.resource_field])


async def execute_runtime_action_capability(
    session: AsyncSession,
    *,
    approval_id: str,
    human_decision_id: str,
    lease: JobLease,
) -> RuntimeActionCapabilityResult:
    raw = await session.scalar(
        text(
            "SELECT supportguard_worker_execute_approved_action("
            ":approval_id,:human_decision_id,:job_id,:fencing_token)"
        ),
        {
            "approval_id": approval_id,
            "human_decision_id": human_decision_id,
            "job_id": lease.job_id,
            "fencing_token": lease.fencing_token,
        },
    )
    return RuntimeActionCapabilityResult.model_validate(raw)


async def validate_execution_binding(
    session: AsyncSession,
    approval: ApprovalRequest,
    revision: ApprovalActionRevision,
) -> ProposalRecord:
    """Re-read the immutable business and policy observations before effect commit."""
    proposal = await session.get(ProposalRecord, approval.proposal_id or "")
    if (
        proposal is None
        or proposal.tenant_id != approval.tenant_id
        or proposal.run_id != approval.run_id
        or proposal.action_type != approval.action_type
        or proposal.resource_version != revision.resource_version
    ):
        raise RuntimeConflict("proposal_snapshot_binding_conflict")
    base_payload = dict(proposal.action_payload)
    revision_payload = dict(revision.action_payload)
    if revision.revision_number == 0:
        if revision_payload != base_payload or revision.action_hash != proposal.action_hash:
            raise RuntimeConflict("proposal_snapshot_binding_conflict")
    else:
        if (
            revision.revision_number != 1
            or not revision_matches_approval_edit(
                action_type=approval.action_type,
                base_payload=base_payload,
                revision_payload=revision_payload,
            )
            or action_hash(revision_payload) != revision.action_hash
        ):
            raise RuntimeConflict("proposal_revision_binding_conflict")
    action_spec = get_action_spec_or_none(approval.action_type)
    if action_spec is None:
        raise RuntimeConflict("unsupported_runtime_action")
    expected_tool = action_spec.primary_read_capability
    resource_field = action_spec.resource_field
    expected_resource = str(revision.action_payload.get(resource_field, ""))
    business = [
        item
        for item in proposal.observation_binding
        if item.get("tool_name") == expected_tool
        and item.get("status") == "ok"
        and item.get("resource_id") == expected_resource
        and int(item.get("resource_version", -1)) == revision.resource_version
    ]
    policy = [
        item
        for item in proposal.observation_binding
        if item.get("tool_name") == "search_knowledge"
        and item.get("status") == "ok"
        and bool(item.get("source_refs"))
    ]
    if len(business) != 1 or not policy:
        raise RuntimeConflict("proposal_observation_binding_conflict")
    for item in [business[0], policy[-1]]:
        row = await session.scalar(
            select(ToolObservation)
            .join(ToolInvocation, ToolInvocation.id == ToolObservation.invocation_id)
            .join(TurnGroup, TurnGroup.id == ToolInvocation.turn_group_id)
            .where(
                ToolObservation.id == item.get("observation_id"),
                ToolObservation.invocation_id == item.get("invocation_id"),
                ToolObservation.content_hash == item.get("observation_content_hash"),
                ToolObservation.tenant_id == approval.tenant_id,
                ToolObservation.run_id == approval.run_id,
                ToolObservation.status == "ok",
                ToolInvocation.tool_name == item.get("tool_name"),
                ToolInvocation.lifecycle == "terminal",
                ToolInvocation.outcome == "succeeded",
                TurnGroup.id == item.get("turn_group_id"),
                TurnGroup.status == "closed",
            )
        )
        if row is None:
            raise RuntimeConflict("proposal_observation_ledger_conflict")
        if item.get("tool_name") == expected_tool:
            data = dict(row.payload.get("data", {}))
            if (
                str(data.get(resource_field, "")) != expected_resource
                or int(data.get("version", -1)) != revision.resource_version
            ):
                raise RuntimeConflict("proposal_business_observation_conflict")
        elif not row.payload.get("source_refs"):
            raise RuntimeConflict("proposal_policy_evidence_conflict")
    return proposal


class RuntimeActionExecutor:
    """Runtime-only high-risk commands; never registered with MCP or the model."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.session = session
        self.test_capability = test_capability

    async def execute(
        self,
        lease: JobLease,
        *,
        approval_id: str,
        trace_id: str | None = None,
    ) -> ActionResult:
        production_capability = (
            self.session.get_bind().dialect.name == "postgresql" and self.test_capability is None
        )
        if production_capability:
            # The owner capability owns the complete PostgreSQL lock order.
            # These are lock-free identity reads; taking an Approval lock here
            # would invert Ticket -> Approval against API decision/withdrawal.
            approval = await self.session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            decision = await self.session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            if approval is None or decision is None:
                raise RuntimeConflict("approval_not_found")
            capability = await execute_runtime_action_capability(
                self.session,
                approval_id=approval.id,
                human_decision_id=decision.id,
                lease=lease,
            )
            await self.session.refresh(approval)
            if capability.status == "stale":
                if approval.status != "stale":
                    raise RuntimeConflict("approval_state_conflict")
                if not capability.reused:
                    await self._bind_stale_event(
                        approval,
                        decision,
                        reason=capability.reason or "action_snapshot_stale",
                    )
                return ActionResult(
                    None,
                    capability.action_type,
                    capability.resource_id,
                    reused=capability.reused,
                    status="stale",
                    reason=capability.reason,
                )
            action = await self.session.get(
                BusinessAction,
                capability.business_action_id or "",
            )
            if action is None:
                raise RuntimeConflict("business_action_missing")
            if not capability.reused:
                await self._bind_action_event(approval, decision, action)
            elif not action.canonical_event_id or not action.canonical_event_hash:
                raise RuntimeConflict("action_effect_binding_conflict")
            await self.session.flush()
            return ActionResult(
                action.id,
                action.action_type,
                capability.resource_id,
                reused=capability.reused,
            )

        await RuntimeJobRepository(self.session).assert_fence(lease)
        approval = await self.session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update()
        )
        if approval is None or approval.run_id != lease.run_id:
            raise RuntimeConflict("approval_not_found")
        decision = await self.session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        revision = await self.session.get(
            ApprovalActionRevision, approval.selected_revision_id or ""
        )
        if (
            decision is None
            or revision is None
            or decision.action_revision_id != revision.id
            or decision.decision not in {"approve", "edit_and_approve"}
            or decision.action_hash != revision.action_hash
        ):
            raise RuntimeConflict("action_decision_binding_conflict")
        resource_id = self._resource_id(approval, revision)
        existing = await self.session.scalar(
            select(BusinessAction).where(
                BusinessAction.tenant_id == approval.tenant_id,
                BusinessAction.action_type == approval.action_type,
                BusinessAction.resource_id == resource_id,
                BusinessAction.resource_version == revision.resource_version,
            )
        )
        # PostgreSQL convergence is owned by the restricted Runtime capability:
        # invoking it again is the only supported way to reconcile an already
        # committed effect with an Approval that still appears approved.
        if existing is not None and not production_capability:
            if (
                existing.action_hash != revision.action_hash
                or existing.result.get("approval_id") != approval.id
                or existing.approval_id != approval.id
                or existing.human_decision_id != decision.id
                or existing.decision_hash != decision.decision_hash
                or existing.action_revision_id != revision.id
                or existing.effect_identity
                != self._effect_identity(approval, revision, resource_id)
                or not existing.canonical_event_id
                or not existing.canonical_event_hash
            ):
                raise RuntimeConflict("action_effect_binding_conflict")
            if approval.status not in {"approved", "executed"}:
                raise RuntimeConflict("approval_not_executable")
            if approval.status == "approved":
                await ActionLifecycleService(self.session).transition(
                    approval,
                    to_status="executed",
                    expected_status="approved",
                    expected_version=approval.status_version,
                )
                approval.consumed_at = datetime.now(UTC)
            if approval.proposal_id:
                proposal = await self.session.get(ProposalRecord, approval.proposal_id)
                if proposal is not None:
                    proposal.status = "stale"
            await self.session.flush()
            return ActionResult(existing.id, existing.action_type, resource_id, reused=True)
        try:
            if not production_capability:
                await assert_execution_approver_scope(
                    self.session,
                    approval_id=approval.id,
                    human_decision_id=decision.id,
                    lease=lease,
                    tenant_id=approval.tenant_id,
                    actor_id=decision.actor_id,
                    test_capability=self.test_capability,
                )
                await validate_execution_binding(self.session, approval, revision)
        except RuntimeConflict:
            return await self._mark_stale(approval, resource_id)
        await assert_mutation_enabled(
            self.session,
            tenant_id=approval.tenant_id,
            action_type=approval.action_type,
        )
        if approval.status != "approved" or approval.consumed_at is not None:
            raise RuntimeConflict("approval_not_executable")
        if action_hash(revision.action_payload) != revision.action_hash:
            return await self._mark_stale(approval, resource_id)
        if approval.action_type == "refund":
            result = await self._refund(
                approval,
                revision,
                resource_id,
                trace_id=trace_id or f"runtime:{lease.job_id}",
            )
        elif approval.action_type == "api_key_revocation":
            result = await self._revoke_key(approval, revision, resource_id)
        elif approval.action_type == "entitlement_change":
            result = await self._change_entitlement(approval, revision, resource_id)
        else:
            raise RuntimeConflict("unsupported_runtime_action")
        if result.status == "stale":
            return result
        action = await self.session.get(BusinessAction, result.business_action_id)
        if action is None:
            raise RuntimeConflict("business_action_missing")
        await self._bind_action_event(approval, decision, action)
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="executed",
            expected_status="approved",
            expected_version=approval.status_version,
        )
        approval.consumed_at = datetime.now(UTC)
        siblings = await self.session.scalars(
            select(ProposalRecord).where(
                ProposalRecord.tenant_id == approval.tenant_id,
                ProposalRecord.action_type == approval.action_type,
                ProposalRecord.resource_id == resource_id,
                ProposalRecord.status.in_(["draft", "bound"]),
            )
        )
        for sibling in siblings:
            sibling.status = "stale"
        await self.session.flush()
        return result

    async def execute_legacy_refund(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        trace_id: str,
    ) -> ActionResult:
        """Compatibility entry for the pre-worker synchronous refund surface.

        Current worker execution always uses :meth:`execute`. Keeping this
        bounded adapter here makes the Runtime Action executor the sole effect
        owner until the legacy API surface is removed in Phase 6.
        """

        binding = await self._load_legacy_refund_binding(
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )
        existing = await self.session.scalar(
            select(BusinessAction).where(
                BusinessAction.tenant_id == binding.approval.tenant_id,
                BusinessAction.action_type == "refund",
                BusinessAction.resource_id == binding.resource_id,
                BusinessAction.resource_version == binding.resource_version,
            )
        )
        if existing is not None:
            return await self._reuse_legacy_refund(
                binding,
                existing,
                trace_id=f"{trace_id}:effect-replay",
            )
        try:
            await assert_mutation_enabled(
                self.session,
                tenant_id=binding.approval.tenant_id,
                action_type="refund",
            )
        except RuntimeConflict as exc:
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "Refund automation is disabled",
            ) from exc
        if binding.approval.status != "approved" or binding.approval.consumed_at is not None:
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "Approval is not executable",
                details={"status": binding.approval.status},
            )
        if action_hash(binding.payload) != binding.payload_hash:
            raise DomainError(ErrorCode.APPROVAL_SNAPSHOT_MISMATCH, "Action hash changed")
        billing = await self._load_legacy_refund_billing(binding)
        return await self._commit_legacy_refund(
            binding,
            billing,
            trace_id=trace_id,
        )

    async def _load_legacy_refund_binding(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
    ) -> _LegacyRefundBinding:
        approval = await self.session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update()
        )
        if approval is None:
            raise DomainError(ErrorCode.APPROVAL_NOT_FOUND, "Approval was not found")
        if approval.idempotency_key != idempotency_key:
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key mismatch")
        if approval.status not in {"approved", "executed"}:
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "Approval is not executable",
                details={"status": approval.status},
            )
        decision = await self.session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        revision = await self.session.get(
            ApprovalActionRevision,
            approval.selected_revision_id or "",
        )
        payload = dict(revision.action_payload if revision is not None else approval.action_payload)
        payload_hash = revision.action_hash if revision is not None else approval.action_hash
        resource_version = (
            revision.resource_version if revision is not None else approval.business_version
        )
        if (
            decision is None
            or decision.decision not in {"approve", "edit_and_approve"}
            or decision.action_hash != payload_hash
        ):
            raise DomainError(
                ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                "Human decision does not bind the executable action",
            )
        return _LegacyRefundBinding(
            approval=approval,
            decision=decision,
            revision=revision,
            payload=payload,
            payload_hash=payload_hash,
            resource_version=resource_version,
            resource_id=str(payload.get("billing_record_id", "")),
        )

    async def _load_legacy_refund_billing(
        self,
        binding: _LegacyRefundBinding,
    ) -> BillingRecord:
        approval = binding.approval
        payload = binding.payload
        billing, pair = await lock_and_evaluate_billing_refund_pair(
            self.session,
            tenant_id=approval.tenant_id,
            customer_id=approval.customer_id,
            billing_record_id=binding.resource_id,
            now=datetime.now(UTC),
        )
        proposal = await self.session.get(ProposalRecord, approval.proposal_id or "")
        if billing is None:
            raise DomainError(ErrorCode.BILLING_RECORD_NOT_FOUND, "Billing record was not found")
        if not (
            billing.customer_id == approval.customer_id == payload.get("customer_id")
            and billing.status == "charged"
            and billing.version == binding.resource_version == payload.get("business_version")
            and str(billing.amount) == str(payload.get("amount"))
            and billing.currency == payload.get("currency")
            and pair is not None
            and proposal is not None
            and refund_pair_matches_proposal(pair, proposal)
        ):
            raise DomainError(
                ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                "Business facts changed after approval snapshot",
            )
        return billing

    async def _reuse_legacy_refund(
        self,
        binding: _LegacyRefundBinding,
        action: BusinessAction,
        *,
        trace_id: str,
    ) -> ActionResult:
        approval = binding.approval
        decision = binding.decision
        if (
            action.action_hash != binding.payload_hash
            or action.idempotency_key != approval.idempotency_key
            or action.result.get("approval_id") != approval.id
            or action.approval_id != approval.id
            or action.human_decision_id != decision.id
            or action.decision_hash != decision.decision_hash
            or (binding.revision is not None and action.action_revision_id != binding.revision.id)
            or action.effect_identity
            != self._effect_identity(approval, binding.revision, binding.resource_id)
            or not action.canonical_event_id
            or not action.canonical_event_hash
        ):
            raise DomainError(
                ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                "Existing effect belongs to a different approval binding",
            )
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="executed",
            expected_status="approved",
            expected_version=approval.status_version,
        )
        approval.consumed_at = approval.consumed_at or datetime.now(UTC)
        await self._stale_bound_proposals(approval, binding.resource_id)
        await self._converge_legacy_refund(
            approval,
            trace_id=trace_id,
        )
        await self.session.flush()
        return ActionResult(action.id, "refund", binding.resource_id, reused=True)

    async def _commit_legacy_refund(
        self,
        binding: _LegacyRefundBinding,
        billing: BillingRecord,
        *,
        trace_id: str,
    ) -> ActionResult:
        approval = binding.approval
        decision = binding.decision
        action = BusinessAction(
            tenant_id=approval.tenant_id,
            ticket_id=approval.ticket_id,
            customer_id=approval.customer_id,
            action_type="refund",
            resource_id=billing.id,
            resource_version=binding.resource_version,
            action_hash=binding.payload_hash,
            approval_id=approval.id,
            action_revision_id=(binding.revision.id if binding.revision is not None else None),
            human_decision_id=decision.id,
            decision_hash=decision.decision_hash,
            effect_identity=self._effect_identity(approval, binding.revision, billing.id),
            status="succeeded",
            idempotency_key=approval.idempotency_key,
            result={
                "approval_id": approval.id,
                "billing_record_id": billing.id,
                "amount": str(billing.amount),
                "currency": billing.currency,
            },
        )
        self.session.add(action)
        await self.session.flush()
        try:
            await self._bind_action_event(approval, decision, action)
        except RuntimeConflict as exc:
            raise DomainError(
                ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                "Canonical action head changed before commit",
            ) from exc
        billing.status = "refunded"
        billing.version += 1
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="executed",
            expected_status="approved",
            expected_version=approval.status_version,
        )
        approval.consumed_at = datetime.now(UTC)
        await self._stale_competing_refund_approvals(approval, billing.id)
        await self._stale_bound_proposals(approval, billing.id)
        await self._converge_legacy_refund(
            approval,
            trace_id=f"{trace_id}:effect-committed",
        )
        self.session.add(
            AuditEvent(
                tenant_id=approval.tenant_id,
                ticket_id=approval.ticket_id,
                customer_id=approval.customer_id,
                event_type="refund_executed",
                actor_type="runtime",
                actor_id=None,
                payload={"approval_id": approval.id, "business_action_id": action.id},
                trace_id=trace_id,
                run_id=approval.run_id,
                created_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return ActionResult(action.id, "refund", billing.id, reused=False)

    async def _stale_competing_refund_approvals(
        self,
        approval: ApprovalRequest,
        billing_record_id: str,
    ) -> None:
        siblings = await self.session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.id != approval.id,
                ApprovalRequest.tenant_id == approval.tenant_id,
                ApprovalRequest.customer_id == approval.customer_id,
                ApprovalRequest.status == "pending",
                ApprovalRequest.action_type == "refund",
            )
        )
        for sibling in siblings:
            if sibling.action_payload.get("billing_record_id") == billing_record_id:
                await ActionLifecycleService(self.session).transition(
                    sibling,
                    to_status="stale",
                    expected_status="pending",
                    expected_version=sibling.status_version,
                )

    async def _stale_bound_proposals(
        self,
        approval: ApprovalRequest,
        resource_id: str,
    ) -> None:
        proposals = await self.session.scalars(
            select(ProposalRecord).where(
                ProposalRecord.tenant_id == approval.tenant_id,
                ProposalRecord.action_type == "refund",
                ProposalRecord.resource_id == resource_id,
                ProposalRecord.status.in_(["draft", "bound"]),
            )
        )
        for proposal in proposals:
            proposal.status = "stale"

    async def _converge_legacy_refund(
        self,
        approval: ApprovalRequest,
        *,
        trace_id: str,
    ) -> None:
        ticket = await self.session.get(
            SupportTicket,
            approval.ticket_id,
            with_for_update=True,
        )
        run = (
            await self.session.get(
                AgentRun,
                approval.run_id,
                with_for_update=True,
            )
            if approval.run_id
            else None
        )
        if ticket is None or run is None:
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Executed approval has no bound Ticket or Agent Run",
            )
        now = datetime.now(UTC)
        if (
            run.status != "completed"
            or run.checkpoint_stage != "action_committed"
            or run.completed_at is None
        ):
            run.status = "completed"
            run.checkpoint_stage = "action_committed"
            run.agent_finish_reason = "executed"
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
            if turn is not None and turn.activity_state != "completed":
                turn.activity_state = "completed"
                turn.result_state = "answered"
                turn.completed_at = run.completed_at
        await activate_next_turn_and_converge_ticket(
            self.session,
            ticket=ticket,
            trace_id=trace_id,
            default_status="resolved",
        )

    async def _refund(
        self,
        approval: ApprovalRequest,
        revision: ApprovalActionRevision,
        resource_id: str,
        *,
        trace_id: str,
    ) -> ActionResult:
        billing, pair = await lock_and_evaluate_billing_refund_pair(
            self.session,
            tenant_id=approval.tenant_id,
            customer_id=approval.customer_id,
            billing_record_id=resource_id,
            now=datetime.now(UTC),
        )
        proposal = await self.session.get(ProposalRecord, approval.proposal_id or "")
        payload = revision.action_payload
        snapshot_valid = bool(
            billing is not None
            and billing.customer_id == approval.customer_id == payload.get("customer_id")
            and billing.status == "charged"
            and billing.version == revision.resource_version == payload.get("business_version")
            and str(billing.amount) == str(payload.get("amount"))
            and billing.currency == payload.get("currency")
            and pair is not None
            and proposal is not None
            and refund_pair_matches_proposal(pair, proposal)
        )
        if billing is None or not snapshot_valid:
            return await self._mark_stale(
                approval,
                resource_id,
                reason="refund_snapshot_stale",
            )
        action = self._action(
            approval,
            revision,
            resource_id,
            {
                "billing_record_id": billing.id,
                "amount": str(billing.amount),
                "currency": billing.currency,
            },
        )
        billing.status = "refunded"
        billing.version += 1
        self.session.add(action)
        await self.session.flush()
        siblings = await self.session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.id != approval.id,
                ApprovalRequest.tenant_id == approval.tenant_id,
                ApprovalRequest.customer_id == approval.customer_id,
                ApprovalRequest.status == "pending",
                ApprovalRequest.action_type == "refund",
            )
        )
        for sibling in siblings:
            if sibling.action_payload.get("billing_record_id") == billing.id:
                await ActionLifecycleService(self.session).transition(
                    sibling,
                    to_status="stale",
                    expected_status="pending",
                    expected_version=sibling.status_version,
                )
        self.session.add(
            AuditEvent(
                tenant_id=approval.tenant_id,
                ticket_id=approval.ticket_id,
                customer_id=approval.customer_id,
                event_type="refund_executed",
                actor_type="runtime",
                actor_id=None,
                payload={"approval_id": approval.id, "business_action_id": action.id},
                trace_id=trace_id,
                run_id=approval.run_id,
                created_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return ActionResult(action.id, action.action_type, resource_id, reused=False)

    async def _revoke_key(
        self,
        approval: ApprovalRequest,
        revision: ApprovalActionRevision,
        resource_id: str,
    ) -> ActionResult:
        key = await self.session.scalar(
            select(ApiKeyMetadata)
            .where(
                ApiKeyMetadata.tenant_id == approval.tenant_id,
                ApiKeyMetadata.customer_id == approval.customer_id,
                ApiKeyMetadata.key_id == resource_id,
            )
            .with_for_update()
        )
        payload = revision.action_payload
        if (
            key is None
            or key.status != "active"
            or key.version != revision.resource_version
            or key.fingerprint != payload.get("fingerprint")
        ):
            return await self._mark_stale(approval, resource_id, reason="api_key_snapshot_stale")
        action = self._action(approval, revision, resource_id, {"status": "revoked"})
        key.status = "revoked"
        key.version += 1
        self.session.add(action)
        await self.session.flush()
        return ActionResult(action.id, action.action_type, resource_id, reused=False)

    async def _change_entitlement(
        self,
        approval: ApprovalRequest,
        revision: ApprovalActionRevision,
        resource_id: str,
    ) -> ActionResult:
        subscription = await self.session.scalar(
            select(Subscription)
            .where(
                Subscription.tenant_id == approval.tenant_id,
                Subscription.customer_id == approval.customer_id,
                Subscription.id == resource_id,
            )
            .with_for_update()
        )
        if subscription is None or subscription.version != revision.resource_version:
            return await self._mark_stale(
                approval, resource_id, reason="entitlement_snapshot_stale"
            )
        target = dict(revision.action_payload.get("target", {}))
        change_type = str(revision.action_payload.get("change_type", ""))
        if change_type == "quota_change":
            catalog = await self.session.scalar(
                select(PlanCatalog)
                .where(PlanCatalog.plan == subscription.plan)
                .order_by(PlanCatalog.version.desc())
                .limit(1)
            )
            valid = catalog is not None and (
                (
                    set(target) == {"rpm_limit"}
                    and catalog.min_rpm <= int(target["rpm_limit"]) <= catalog.max_rpm
                )
                or (
                    set(target) == {"concurrency_limit"}
                    and catalog.min_concurrency
                    <= int(target["concurrency_limit"])
                    <= catalog.max_concurrency
                )
            )
            if not valid:
                return await self._mark_stale(
                    approval, resource_id, reason="entitlement_policy_stale"
                )
        elif change_type == "plan_change":
            target_plan = str(target.get("plan", ""))
            catalog = await self.session.scalar(
                select(PlanCatalog.id).where(PlanCatalog.plan == target_plan).limit(1)
            )
            if not target_plan or catalog is None:
                return await self._mark_stale(
                    approval, resource_id, reason="entitlement_plan_stale"
                )
        else:
            return await self._mark_stale(
                approval, resource_id, reason="entitlement_change_type_stale"
            )
        before: dict[str, Any] = {
            "plan": subscription.plan,
            "rpm_limit": subscription.rpm_limit,
            "concurrency_limit": subscription.concurrency_limit,
        }
        if "plan" in target:
            subscription.plan = str(target["plan"])
        if "rpm_limit" in target:
            subscription.rpm_limit = int(target["rpm_limit"])
        if "concurrency_limit" in target:
            subscription.concurrency_limit = int(target["concurrency_limit"])
        action = self._action(approval, revision, resource_id, {"before": before, "after": target})
        subscription.version += 1
        self.session.add(action)
        await self.session.flush()
        return ActionResult(action.id, action.action_type, resource_id, reused=False)

    async def _mark_stale(
        self, approval: ApprovalRequest, resource_id: str, *, reason: str = "action_snapshot_stale"
    ) -> ActionResult:
        await ActionLifecycleService(self.session).transition(
            approval,
            to_status="stale",
            expected_status=approval.status,
            expected_version=approval.status_version,
        )
        if approval.proposal_id:
            proposal = await self.session.get(ProposalRecord, approval.proposal_id)
            if proposal is not None:
                proposal.status = "stale"
        run = await self.session.get(AgentRun, approval.run_id or "")
        if run is not None:
            decision = await self.session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval.id)
            )
            if (
                decision is None
                or not decision.canonical_event_id
                or not decision.canonical_event_hash
            ):
                raise RuntimeConflict("action_decision_event_binding_missing")
            try:
                await AgentRunStore(self.session).append_event(
                    run,
                    event_type="runtime_action_reconciliation",
                    payload={
                        "approval_id": approval.id,
                        "status": "stale",
                        "reason": reason,
                    },
                    visibility="approver",
                    expected_ticket_head_event_id=decision.canonical_event_id,
                    expected_ticket_sequence=None,
                    expected_ticket_event_hash=decision.canonical_event_hash,
                )
            except CanonicalEventHeadConflict as exc:
                raise RuntimeConflict("action_expected_head_conflict") from exc
        await self.session.flush()
        return ActionResult(
            None,
            approval.action_type,
            resource_id,
            reused=False,
            status="stale",
            reason=reason,
        )

    async def _bind_stale_event(
        self,
        approval: ApprovalRequest,
        decision: HumanDecision,
        *,
        reason: str,
    ) -> None:
        """Publish stale provenance after the database-owned CAS.

        This method never mutates Approval state.  PostgreSQL state authority
        remains inside ``supportguard_worker_execute_approved_action``.
        """

        if (
            not decision.canonical_event_id
            or not decision.canonical_event_hash
            or approval.status != "stale"
        ):
            raise RuntimeConflict("action_decision_event_binding_missing")
        run = await self.session.get(AgentRun, approval.run_id or "")
        if run is None:
            raise RuntimeConflict("action_run_missing")
        try:
            await AgentRunStore(self.session).append_event(
                run,
                event_type="runtime_action_reconciliation",
                payload={
                    "approval_id": approval.id,
                    "status": "stale",
                    "reason": reason,
                },
                visibility="approver",
                expected_ticket_head_event_id=decision.canonical_event_id,
                expected_ticket_sequence=None,
                expected_ticket_event_hash=decision.canonical_event_hash,
            )
        except CanonicalEventHeadConflict as exc:
            raise RuntimeConflict("action_expected_head_conflict") from exc
        await self.session.flush()

    @staticmethod
    def _resource_id(approval: ApprovalRequest, revision: ApprovalActionRevision) -> str:
        action_spec = get_action_spec_or_none(approval.action_type)
        if (
            action_spec is None
            or approval.resource_type != action_spec.resource_field
            or not approval.resource_id
            or revision.resource_version != approval.business_version
        ):
            raise RuntimeConflict("approval_resource_missing")
        return approval.resource_id

    @staticmethod
    def _action(
        approval: ApprovalRequest,
        revision: ApprovalActionRevision,
        resource_id: str,
        result: dict[str, Any],
    ) -> BusinessAction:
        effect_identity = RuntimeActionExecutor._effect_identity(approval, revision, resource_id)
        return BusinessAction(
            tenant_id=approval.tenant_id,
            ticket_id=approval.ticket_id,
            customer_id=approval.customer_id,
            action_type=approval.action_type,
            resource_id=resource_id,
            resource_version=revision.resource_version,
            action_hash=revision.action_hash,
            approval_id=approval.id,
            action_revision_id=revision.id,
            effect_identity=effect_identity,
            status="succeeded",
            idempotency_key=(
                approval.idempotency_key
                if approval.action_type == "refund"
                else (
                    f"effect:{approval.tenant_id}:{approval.action_type}:"
                    f"{resource_id}:{revision.resource_version}"
                )
            ),
            result={"approval_id": approval.id, **result},
        )

    @staticmethod
    def _effect_identity(
        approval: ApprovalRequest,
        revision: ApprovalActionRevision | None,
        resource_id: str,
    ) -> str:
        return canonical_hash(
            {
                "tenant_id": approval.tenant_id,
                "action_type": approval.action_type,
                "resource_id": resource_id,
                "resource_version": (
                    revision.resource_version if revision is not None else approval.business_version
                ),
            }
        )

    async def _bind_action_event(
        self,
        approval: ApprovalRequest,
        decision: HumanDecision,
        action: BusinessAction,
    ) -> None:
        if not decision.decision_hash:
            raise RuntimeConflict("decision_hash_missing")
        run = await self.session.get(AgentRun, approval.run_id or "")
        if run is None:
            raise RuntimeConflict("action_run_missing")
        action.human_decision_id = decision.id
        action.decision_hash = decision.decision_hash
        try:
            event = await AgentRunStore(self.session).append_event(
                run,
                event_type="runtime_action_committed",
                payload={
                    "approval_id": approval.id,
                    "human_decision_id": decision.id,
                    "decision_hash": decision.decision_hash,
                    "business_action_id": action.id,
                    "effect_identity": action.effect_identity,
                    "action_hash": action.action_hash,
                },
                visibility="approver",
                expected_ticket_head_event_id=decision.canonical_event_id,
                expected_ticket_sequence=None,
                expected_ticket_event_hash=decision.canonical_event_hash,
            )
        except CanonicalEventHeadConflict as exc:
            raise RuntimeConflict("action_expected_head_conflict") from exc
        action.canonical_event_id = event.id
        action.canonical_event_hash = event.event_hash
