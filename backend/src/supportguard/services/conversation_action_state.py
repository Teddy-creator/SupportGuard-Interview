from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import (
    AgentEvent,
    ApprovalRequest,
    BusinessAction,
    HumanDecision,
    ProposalRecord,
    ProposalWithdrawal,
    RuntimeJob,
    TicketMessage,
)
from supportguard.services.approval_lifecycle import ACTION_RESOURCE_TYPES

ActionType = Literal["refund", "api_key_revocation", "entitlement_change"]
ApprovalStatus = Literal[
    "pending",
    "approved",
    "executed",
    "rejected",
    "stale",
    "withdrawn",
    "failed",
    "manual_takeover",
]
ProjectionStatus = Literal[
    "pending",
    "approved",
    "executing",
    "verification_pending",
    "executed",
    "rejected",
    "stale",
    "withdrawn",
    "failed",
    "manual_takeover_legacy",
]
DecisionClass = Literal[
    "none",
    "approve",
    "edit_and_approve",
    "reject",
    "customer_withdrawal",
    "system_transition",
    "legacy_manual_takeover",
]
ExecutionState = Literal[
    "not_started",
    "queued",
    "in_progress",
    "verification_pending",
    "succeeded",
    "not_executed",
    "failed",
    "legacy_stopped",
]
TransitionEventType = Literal[
    "proposal_withdrawn",
    "runtime_action_reconciliation",
    "runtime_failed",
    "approval_staled_duplicate_identity",
]
CustomerAction = Literal["withdraw"]

MAX_ACTION_STATES_PER_TICKET: Final[int] = 100
_APPROVAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "pending",
        "approved",
        "executed",
        "rejected",
        "stale",
        "withdrawn",
        "failed",
        "manual_takeover",
    }
)
_TERMINAL_WITHOUT_EFFECT: Final[frozenset[str]] = frozenset(
    {"rejected", "stale", "withdrawn", "failed", "manual_takeover"}
)
_CUSTOMER_SAFE_REASON_CODES: Final[dict[ProjectionStatus, str]] = {
    "pending": "approval_pending",
    "approved": "approval_approved_awaiting_execution",
    "executing": "action_execution_in_progress",
    "verification_pending": "action_execution_verification_pending",
    "executed": "action_execution_confirmed",
    "rejected": "approval_rejected_no_effect",
    "stale": "action_requires_fresh_verification",
    "withdrawn": "approval_withdrawn_no_effect",
    "failed": "action_failed_confirmed_no_effect",
    "manual_takeover_legacy": "legacy_manual_takeover_no_operator",
}


@dataclass(frozen=True, slots=True)
class _LifecycleEvidenceContract:
    required_sources: frozenset[str]
    forbidden_sources: frozenset[str]
    allowed_decisions: frozenset[str] = frozenset()
    allowed_transition_events: frozenset[str] = frozenset()


_LIFECYCLE_EVIDENCE: Final[dict[str, _LifecycleEvidenceContract]] = {
    "pending": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal"}),
        forbidden_sources=frozenset(
            {"decision", "business_action", "withdrawal", "runtime_job", "transition_event"}
        ),
    ),
    "approved": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "decision", "runtime_job"}),
        forbidden_sources=frozenset({"withdrawal", "transition_event"}),
        allowed_decisions=frozenset({"approve", "edit_and_approve"}),
    ),
    "executed": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "decision", "business_action"}),
        forbidden_sources=frozenset({"withdrawal", "transition_event"}),
        allowed_decisions=frozenset({"approve", "edit_and_approve"}),
    ),
    "rejected": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "decision"}),
        forbidden_sources=frozenset(
            {"business_action", "withdrawal", "runtime_job", "transition_event"}
        ),
        allowed_decisions=frozenset({"reject"}),
    ),
    "withdrawn": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "withdrawal", "transition_event"}),
        forbidden_sources=frozenset({"decision", "business_action", "runtime_job"}),
        allowed_transition_events=frozenset({"proposal_withdrawn"}),
    ),
    "stale": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "transition_event"}),
        forbidden_sources=frozenset({"withdrawal"}),
        allowed_decisions=frozenset({"approve", "edit_and_approve"}),
        allowed_transition_events=frozenset(
            {"runtime_action_reconciliation", "approval_staled_duplicate_identity"}
        ),
    ),
    "failed": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal", "runtime_job", "transition_event"}),
        forbidden_sources=frozenset({"withdrawal"}),
        allowed_decisions=frozenset({"approve", "edit_and_approve"}),
        allowed_transition_events=frozenset({"runtime_failed"}),
    ),
    "manual_takeover": _LifecycleEvidenceContract(
        required_sources=frozenset({"proposal"}),
        forbidden_sources=frozenset({"business_action", "withdrawal", "runtime_job"}),
        allowed_decisions=frozenset({"manual_takeover"}),
    ),
}


class ConversationActionStateProjectionError(RuntimeError):
    """A persisted action aggregate cannot be represented as trusted state."""


class ConversationActionStateV1(BaseModel):
    """Read-only customer-safe action truth for one Approval aggregate.

    This projection may explain an existing action and resolve references.  It
    is deliberately incapable of granting approval, proposal, or execution
    authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["conversation-action-state.v1"] = "conversation-action-state.v1"
    approval_id: str
    origin_run_id: str
    origin_turn_id: str
    action_type: ActionType
    resource_type: str
    resource_id: str
    resource_version: int = Field(ge=1)
    approval_status: ApprovalStatus
    projection_status: ProjectionStatus
    status_version: int = Field(ge=1)
    actionable: bool
    allowed_customer_actions: tuple[CustomerAction, ...] = ()
    decision_class: DecisionClass
    customer_safe_reason_code: str
    execution_state: ExecutionState
    business_action_id: str | None = None
    created_at: datetime
    updated_at: datetime
    source_event_id: str | None = None
    source_event_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    grants_action_authority: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_checkpoint_timestamp(cls, value: object) -> object:
        """Keep pre-v1.5.12 checkpoint projections readable.

        Historical ``conversation-action-state.v1`` payloads only persisted
        ``updated_at``.  The first observed timestamp cannot be recovered from
        those immutable checkpoints, so use their existing authoritative
        timestamp as the conservative display fallback.  New projections
        always persist both fields.
        """

        if not isinstance(value, Mapping) or "created_at" in value:
            return value
        updated_at = value.get("updated_at")
        if updated_at is None:
            return value
        return {**value, "created_at": updated_at}

    @model_validator(mode="after")
    def validate_safe_shape(self) -> ConversationActionStateV1:
        if (self.source_event_id is None) != (self.source_event_hash is None):
            raise ValueError("source event identity must be complete or absent")
        if self.actionable != bool(self.allowed_customer_actions):
            raise ValueError("actionable must match allowed customer actions")
        if self.allowed_customer_actions not in {(), ("withdraw",)}:
            raise ValueError("unsupported customer action")
        if self.allowed_customer_actions and self.projection_status != "pending":
            raise ValueError("only a pending approval may be withdrawn")
        if self.customer_safe_reason_code != _CUSTOMER_SAFE_REASON_CODES[self.projection_status]:
            raise ValueError("customer-safe reason does not match projection status")
        return self


class _SourceFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalActionSourceFact(_SourceFact):
    id: str
    tenant_id: str
    ticket_id: str
    customer_id: str
    proposal_id: str | None
    run_id: str | None
    action_type: str
    resource_type: str
    resource_id: str
    origin_turn_id: str
    business_version: int
    status: str
    status_version: int
    expected_ticket_head_event_id: str | None = None
    expected_ticket_event_hash: str | None = None
    transition_event_id: str | None = None
    transition_event_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    transition_event_type: TransitionEventType | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def transition_event_identity_is_complete(
        self,
    ) -> ApprovalActionSourceFact:
        values = (
            self.transition_event_id,
            self.transition_event_hash,
            self.transition_event_type,
        )
        if any(item is not None for item in values) and any(
            item is None for item in values
        ):
            raise ValueError("transition event identity must be complete or absent")
        if (self.expected_ticket_head_event_id is None) != (
            self.expected_ticket_event_hash is None
        ):
            raise ValueError("approval event identity must be complete or absent")
        return self


class ProposalActionSourceFact(_SourceFact):
    id: str
    tenant_id: str
    run_id: str
    action_type: str
    resource_id: str
    resource_version: int
    status: str
    created_at: datetime
    updated_at: datetime


class HumanDecisionActionSourceFact(_SourceFact):
    id: str
    tenant_id: str
    approval_id: str
    decision: str
    canonical_event_id: str | None = None
    canonical_event_hash: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def canonical_event_identity_is_complete(
        self,
    ) -> HumanDecisionActionSourceFact:
        if (self.canonical_event_id is None) != (self.canonical_event_hash is None):
            raise ValueError("decision event identity must be complete or absent")
        return self


class BusinessActionSourceFact(_SourceFact):
    id: str
    tenant_id: str
    ticket_id: str
    customer_id: str
    approval_id: str | None
    action_type: str
    resource_id: str | None
    resource_version: int | None
    status: str
    canonical_event_id: str | None = None
    canonical_event_hash: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def canonical_event_identity_is_complete(
        self,
    ) -> BusinessActionSourceFact:
        if (self.canonical_event_id is None) != (self.canonical_event_hash is None):
            raise ValueError("business action event identity must be complete or absent")
        return self


class ProposalWithdrawalActionSourceFact(_SourceFact):
    id: str
    tenant_id: str
    ticket_id: str
    customer_id: str
    approval_id: str
    proposal_id: str
    created_at: datetime
    updated_at: datetime


class ApprovalRuntimeJobSourceFact(_SourceFact):
    id: str
    tenant_id: str
    ticket_id: str
    run_id: str
    approval_id: str | None
    kind: str
    status: str
    outcome: str | None = None
    delivery_hold_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationActionSources(_SourceFact):
    """Safe source bundle returned by role-specific narrow read adapters."""

    schema_version: Literal["conversation-action-source-bundle.v1"] = (
        "conversation-action-source-bundle.v1"
    )
    approval: ApprovalActionSourceFact
    proposal: ProposalActionSourceFact | None = None
    decision: HumanDecisionActionSourceFact | None = None
    business_action: BusinessActionSourceFact | None = None
    withdrawal: ProposalWithdrawalActionSourceFact | None = None
    runtime_job: ApprovalRuntimeJobSourceFact | None = None


@dataclass(frozen=True, slots=True)
class ConversationActionOrmSources:
    approval: ApprovalRequest
    proposal: ProposalRecord | None = None
    decision: HumanDecision | None = None
    business_action: BusinessAction | None = None
    withdrawal: ProposalWithdrawal | None = None
    runtime_job: RuntimeJob | None = None
    transition_event: AgentEvent | None = None


def conversation_action_sources_from_mapping(
    value: Mapping[str, Any],
) -> ConversationActionSources:
    """Validate a customer-safe JSON bundle returned by a narrow DB capability."""

    return ConversationActionSources.model_validate(value)


def conversation_action_sources_from_orm(
    sources: ConversationActionOrmSources,
) -> ConversationActionSources:
    """Adapt already-authorized ORM rows without copying raw payloads or notes."""

    approval = sources.approval
    proposal = sources.proposal
    decision = sources.decision
    action = sources.business_action
    withdrawal = sources.withdrawal
    job = sources.runtime_job
    transition_event = sources.transition_event
    return ConversationActionSources(
        approval=ApprovalActionSourceFact(
            id=approval.id,
            tenant_id=approval.tenant_id,
            ticket_id=approval.ticket_id,
            customer_id=approval.customer_id,
            proposal_id=approval.proposal_id,
            run_id=approval.run_id,
            action_type=approval.action_type,
            resource_type=approval.resource_type,
            resource_id=approval.resource_id,
            origin_turn_id=approval.origin_turn_id,
            business_version=approval.business_version,
            status=approval.status,
            status_version=approval.status_version,
            expected_ticket_head_event_id=approval.expected_ticket_head_event_id,
            expected_ticket_event_hash=approval.expected_ticket_event_hash,
            transition_event_id=(
                transition_event.id if transition_event is not None else None
            ),
            transition_event_hash=(
                transition_event.event_hash
                if transition_event is not None
                else None
            ),
            transition_event_type=(
                cast(TransitionEventType, transition_event.event_type)
                if transition_event is not None
                else None
            ),
            created_at=approval.created_at,
            updated_at=approval.updated_at,
        ),
        proposal=(
            ProposalActionSourceFact(
                id=proposal.id,
                tenant_id=proposal.tenant_id,
                run_id=proposal.run_id,
                action_type=proposal.action_type,
                resource_id=proposal.resource_id,
                resource_version=proposal.resource_version,
                status=proposal.status,
                created_at=proposal.created_at,
                updated_at=proposal.updated_at,
            )
            if proposal is not None
            else None
        ),
        decision=(
            HumanDecisionActionSourceFact(
                id=decision.id,
                tenant_id=decision.tenant_id,
                approval_id=decision.approval_id,
                decision=decision.decision,
                canonical_event_id=decision.canonical_event_id,
                canonical_event_hash=decision.canonical_event_hash,
                created_at=decision.created_at,
                updated_at=decision.updated_at,
            )
            if decision is not None
            else None
        ),
        business_action=(
            BusinessActionSourceFact(
                id=action.id,
                tenant_id=action.tenant_id,
                ticket_id=action.ticket_id,
                customer_id=action.customer_id,
                approval_id=action.approval_id,
                action_type=action.action_type,
                resource_id=action.resource_id,
                resource_version=action.resource_version,
                status=action.status,
                canonical_event_id=action.canonical_event_id,
                canonical_event_hash=action.canonical_event_hash,
                created_at=action.created_at,
                updated_at=action.updated_at,
            )
            if action is not None
            else None
        ),
        withdrawal=(
            ProposalWithdrawalActionSourceFact(
                id=withdrawal.id,
                tenant_id=withdrawal.tenant_id,
                ticket_id=withdrawal.ticket_id,
                customer_id=withdrawal.customer_id,
                approval_id=withdrawal.approval_id,
                proposal_id=withdrawal.proposal_id,
                created_at=withdrawal.created_at,
                updated_at=withdrawal.updated_at,
            )
            if withdrawal is not None
            else None
        ),
        runtime_job=(
            ApprovalRuntimeJobSourceFact(
                id=job.id,
                tenant_id=job.tenant_id,
                ticket_id=job.ticket_id,
                run_id=job.run_id,
                approval_id=job.approval_id,
                kind=job.kind,
                status=job.status,
                outcome=job.outcome,
                delivery_hold_reason=job.delivery_hold_reason,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            if job is not None
            else None
        ),
    )


def project_conversation_action_state(
    sources: ConversationActionSources,
) -> ConversationActionStateV1:
    """Build trusted state exclusively from persisted action aggregate rows."""

    _validate_sources(sources)
    approval = sources.approval
    approval_status = cast(ApprovalStatus, approval.status)
    projection_status = _projection_status(sources)
    source_event_id, source_event_hash = _source_event(sources)
    allowed_customer_actions: tuple[CustomerAction, ...] = (
        ("withdraw",) if projection_status == "pending" else ()
    )
    return ConversationActionStateV1(
        approval_id=approval.id,
        origin_run_id=cast(str, approval.run_id),
        origin_turn_id=approval.origin_turn_id,
        action_type=cast(ActionType, approval.action_type),
        resource_type=approval.resource_type,
        resource_id=approval.resource_id,
        # business_version remains the sole persisted resource-version truth.
        resource_version=approval.business_version,
        approval_status=approval_status,
        projection_status=projection_status,
        status_version=approval.status_version,
        actionable=bool(allowed_customer_actions),
        allowed_customer_actions=allowed_customer_actions,
        decision_class=_decision_class(sources, projection_status),
        customer_safe_reason_code=_CUSTOMER_SAFE_REASON_CODES[projection_status],
        execution_state=_execution_state(projection_status),
        business_action_id=(
            sources.business_action.id if sources.business_action is not None else None
        ),
        created_at=approval.created_at,
        updated_at=_latest_update(sources),
        source_event_id=source_event_id,
        source_event_hash=source_event_hash,
        grants_action_authority=False,
    )


class ConversationActionStateProjector:
    """Bounded ORM adapter for already-authorized query sessions.

    SQLite tests and privileged callers may use this adapter directly.
    Restricted production roles must obtain the same safe source bundle through
    a narrow owner/source capability and pass it to
    ``conversation_action_sources_from_mapping``.  This adapter is never a
    reason to widen direct table grants (notably ``runtime_jobs``).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_approval(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        approval_id: str,
    ) -> ConversationActionStateV1 | None:
        with self.session.no_autoflush:
            approval = await self.session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.customer_id == customer_id,
                    ApprovalRequest.id == approval_id,
                )
            )
            if approval is None:
                return None
            return (await self._project_many([approval]))[0]

    async def list_for_ticket(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        ticket_id: str,
        limit: int = MAX_ACTION_STATES_PER_TICKET,
    ) -> tuple[ConversationActionStateV1, ...]:
        if limit < 1 or limit > MAX_ACTION_STATES_PER_TICKET:
            raise ValueError("conversation action state limit outside bound")
        with self.session.no_autoflush:
            approvals = list(
                (
                    await self.session.scalars(
                        select(ApprovalRequest)
                        .where(
                            ApprovalRequest.tenant_id == tenant_id,
                            ApprovalRequest.customer_id == customer_id,
                            or_(
                                ApprovalRequest.ticket_id == ticket_id,
                                ApprovalRequest.id.in_(
                                    select(TicketMessage.approval_id).where(
                                        TicketMessage.tenant_id == tenant_id,
                                        TicketMessage.ticket_id == ticket_id,
                                        TicketMessage.approval_id.is_not(None),
                                    )
                                ),
                            ),
                        )
                        .order_by(
                            ApprovalRequest.updated_at.desc(),
                            ApprovalRequest.created_at.desc(),
                            ApprovalRequest.id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
            return await self._project_many(approvals)

    async def _project_many(
        self,
        approvals: list[ApprovalRequest],
    ) -> tuple[ConversationActionStateV1, ...]:
        if not approvals:
            return ()
        tenant_ids = {item.tenant_id for item in approvals}
        if len(tenant_ids) != 1:
            raise ConversationActionStateProjectionError("action state batch crosses tenant scope")
        tenant_id = next(iter(tenant_ids))
        approval_ids = [item.id for item in approvals]
        proposal_ids = [item.proposal_id for item in approvals if item.proposal_id]

        proposals = (
            (
                await self.session.scalars(
                    select(ProposalRecord).where(
                        ProposalRecord.tenant_id == tenant_id,
                        ProposalRecord.id.in_(proposal_ids),
                    )
                )
            ).all()
            if proposal_ids
            else []
        )
        decisions = (
            await self.session.scalars(
                select(HumanDecision)
                .where(
                    HumanDecision.tenant_id == tenant_id,
                    HumanDecision.approval_id.in_(approval_ids),
                )
                .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc())
            )
        ).all()
        actions = (
            await self.session.scalars(
                select(BusinessAction)
                .where(
                    BusinessAction.tenant_id == tenant_id,
                    BusinessAction.approval_id.in_(approval_ids),
                )
                .order_by(BusinessAction.created_at.desc(), BusinessAction.id.desc())
            )
        ).all()
        withdrawals = (
            await self.session.scalars(
                select(ProposalWithdrawal)
                .where(
                    ProposalWithdrawal.tenant_id == tenant_id,
                    ProposalWithdrawal.approval_id.in_(approval_ids),
                )
                .order_by(
                    ProposalWithdrawal.created_at.desc(),
                    ProposalWithdrawal.id.desc(),
                )
            )
        ).all()
        jobs = (
            await self.session.scalars(
                select(RuntimeJob)
                .where(
                    RuntimeJob.tenant_id == tenant_id,
                    RuntimeJob.approval_id.in_(approval_ids),
                    RuntimeJob.kind == "approval_resume",
                )
                .order_by(RuntimeJob.created_at.desc(), RuntimeJob.id.desc())
            )
        ).all()
        transition_events = (
            await self.session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.tenant_id == tenant_id,
                    AgentEvent.run_id.in_(
                        [item.run_id for item in approvals if item.run_id is not None]
                        or ["<none>"]
                    ),
                    AgentEvent.event_type.in_(
                        (
                            "proposal_withdrawn",
                            "runtime_action_reconciliation",
                            "runtime_failed",
                            "approval_staled_duplicate_identity",
                        )
                    ),
                )
                .order_by(
                    AgentEvent.ticket_sequence.desc(),
                    AgentEvent.id.desc(),
                )
                .limit(MAX_ACTION_STATES_PER_TICKET * 4)
            )
        ).all()

        proposal_by_id = {item.id: item for item in proposals}
        decision_by_approval: dict[str, HumanDecision] = {}
        for decision in decisions:
            decision_by_approval.setdefault(decision.approval_id, decision)
        action_by_approval: dict[str, BusinessAction] = {}
        for business_action in actions:
            if business_action.approval_id is not None:
                action_by_approval.setdefault(business_action.approval_id, business_action)
        withdrawal_by_approval: dict[str, ProposalWithdrawal] = {}
        for withdrawal in withdrawals:
            withdrawal_by_approval.setdefault(withdrawal.approval_id, withdrawal)
        job_by_approval: dict[str, RuntimeJob] = {}
        approval_by_id = {item.id: item for item in approvals}
        approval_id_by_job: dict[str, str] = {}
        for runtime_job in jobs:
            if runtime_job.approval_id is not None:
                job_by_approval.setdefault(runtime_job.approval_id, runtime_job)
                approval_id_by_job[runtime_job.id] = runtime_job.approval_id
        transition_event_by_approval: dict[str, AgentEvent] = {}
        expected_event_types = {
            "withdrawn": {"proposal_withdrawn"},
            "stale": {
                "runtime_action_reconciliation",
                "approval_staled_duplicate_identity",
            },
            "failed": {"runtime_failed"},
        }
        for event in transition_events:
            approval_id = str(event.payload.get("approval_id") or "")
            if not approval_id and event.job_id:
                approval_id = approval_id_by_job.get(event.job_id, "")
            approval = approval_by_id.get(approval_id)
            if (
                approval is None
                or event.event_type
                not in expected_event_types.get(approval.status, set())
                or event.ticket_id != approval.ticket_id
                or event.run_id != approval.run_id
                or event.customer_id != approval.customer_id
            ):
                continue
            transition_event_by_approval.setdefault(approval.id, event)
        return tuple(
            project_conversation_action_state(
                conversation_action_sources_from_orm(
                    ConversationActionOrmSources(
                        approval=approval,
                        proposal=(
                            proposal_by_id.get(approval.proposal_id)
                            if approval.proposal_id is not None
                            else None
                        ),
                        decision=decision_by_approval.get(approval.id),
                        business_action=action_by_approval.get(approval.id),
                        withdrawal=withdrawal_by_approval.get(approval.id),
                        runtime_job=job_by_approval.get(approval.id),
                        transition_event=transition_event_by_approval.get(
                            approval.id
                        ),
                    ),
                )
            )
            for approval in approvals
        )


def _validate_sources(sources: ConversationActionSources) -> None:
    approval = sources.approval
    if (
        approval.status not in _APPROVAL_STATUSES
        or approval.run_id is None
        or approval.action_type not in ACTION_RESOURCE_TYPES
        or ACTION_RESOURCE_TYPES[approval.action_type] != approval.resource_type
        or not approval.resource_id
        or approval.business_version < 1
        or approval.status_version < 1
        or not approval.origin_turn_id
    ):
        raise ConversationActionStateProjectionError(
            "approval canonical action identity is invalid"
        )
    if approval.transition_event_type is not None and approval.transition_event_type not in {
        "withdrawn": {"proposal_withdrawn"},
        "stale": {
            "runtime_action_reconciliation",
            "approval_staled_duplicate_identity",
        },
        "failed": {"runtime_failed"},
    }.get(approval.status, set()):
        raise ConversationActionStateProjectionError(
            "transition event conflicts with approval status"
        )

    proposal = sources.proposal
    if proposal is None or (
        approval.proposal_id != proposal.id
        or approval.tenant_id != proposal.tenant_id
        or approval.run_id != proposal.run_id
        or approval.action_type != proposal.action_type
        or approval.resource_id != proposal.resource_id
        or approval.business_version != proposal.resource_version
    ):
        raise ConversationActionStateProjectionError(
            "proposal canonical action identity is missing or conflicts with approval"
        )
    expected_proposal_statuses = (
        {"bound"}
        if approval.status in {"pending", "approved"}
        else {"stale"}
        if approval.status != "manual_takeover"
        else {"bound", "stale"}
    )
    if proposal.status not in expected_proposal_statuses:
        raise ConversationActionStateProjectionError(
            "proposal lifecycle conflicts with approval"
        )

    decision = sources.decision
    if decision is not None and (
        decision.tenant_id != approval.tenant_id or decision.approval_id != approval.id
    ):
        raise ConversationActionStateProjectionError(
            "human decision identity conflicts with approval"
        )

    action = sources.business_action
    if action is not None and (
        action.tenant_id != approval.tenant_id
        or action.ticket_id != approval.ticket_id
        or action.customer_id != approval.customer_id
        or action.approval_id != approval.id
        or action.action_type != approval.action_type
        or action.resource_id != approval.resource_id
        or action.resource_version != approval.business_version
    ):
        raise ConversationActionStateProjectionError(
            "business action identity conflicts with approval"
        )
    withdrawal = sources.withdrawal
    if withdrawal is not None and (
        withdrawal.tenant_id != approval.tenant_id
        or withdrawal.ticket_id != approval.ticket_id
        or withdrawal.customer_id != approval.customer_id
        or withdrawal.approval_id != approval.id
        or withdrawal.proposal_id != approval.proposal_id
    ):
        raise ConversationActionStateProjectionError("withdrawal identity conflicts with approval")
    if withdrawal is not None and action is not None and action.status == "succeeded":
        raise ConversationActionStateProjectionError(
            "withdrawal conflicts with successful business action"
        )
    if withdrawal is not None and approval.status != "withdrawn":
        raise ConversationActionStateProjectionError("withdrawal conflicts with approval status")
    if (
        action is not None
        and approval.status in _TERMINAL_WITHOUT_EFFECT
        and action.status in {"unknown", "verification_pending", "pending", "running", "executing"}
    ):
        raise ConversationActionStateProjectionError(
            "unresolved business action conflicts with no-effect approval terminal state"
        )

    job = sources.runtime_job
    if job is not None and (
        job.kind != "approval_resume"
        or job.tenant_id != approval.tenant_id
        or job.ticket_id != approval.ticket_id
        or job.run_id != approval.run_id
        or job.approval_id != approval.id
    ):
        raise ConversationActionStateProjectionError(
            "approval runtime job identity conflicts with approval"
        )
    if job is not None and job.status == "dead" and approval.status != "failed":
        raise ConversationActionStateProjectionError(
            "dead approval job has not converged the approval"
        )

    _validate_lifecycle_evidence(sources)


def _validate_lifecycle_evidence(sources: ConversationActionSources) -> None:
    approval = sources.approval
    decision = sources.decision
    action = sources.business_action
    job = sources.runtime_job
    contract = _LIFECYCLE_EVIDENCE[approval.status]
    present_sources = {
        name
        for name, present in {
            "proposal": sources.proposal is not None,
            "decision": decision is not None,
            "business_action": action is not None,
            "withdrawal": sources.withdrawal is not None,
            "runtime_job": job is not None,
            "transition_event": approval.transition_event_type is not None,
        }.items()
        if present
    }
    missing = contract.required_sources - present_sources
    forbidden = contract.forbidden_sources & present_sources
    if missing or forbidden:
        raise ConversationActionStateProjectionError(
            "approval lifecycle evidence is incomplete or contradictory"
        )
    if decision is not None and (
        not contract.allowed_decisions
        or decision.decision not in contract.allowed_decisions
        or decision.canonical_event_id is None
    ):
        raise ConversationActionStateProjectionError(
            "human decision conflicts with approval lifecycle"
        )
    if approval.transition_event_type is not None and (
        approval.transition_event_type not in contract.allowed_transition_events
    ):
        raise ConversationActionStateProjectionError(
            "transition event conflicts with approval lifecycle"
        )

    if approval.status == "approved":
        if job is None:
            raise ConversationActionStateProjectionError(
                "approved approval lacks authoritative runtime evidence"
            )
        if action is not None and action.status == "succeeded":
            raise ConversationActionStateProjectionError(
                "successful business action requires an executed approval"
            )
        if job.status == "dead":
            raise ConversationActionStateProjectionError(
                "dead approval job has not converged the approval"
            )
        if (
            job.status == "succeeded"
            and job.outcome != "verification_pending"
            and job.delivery_hold_reason != "state_unknown"
        ):
            raise ConversationActionStateProjectionError(
                "completed approval job has not converged the approval"
            )
    elif approval.status == "executed":
        if action is None:
            raise ConversationActionStateProjectionError(
                "executed approval lacks authoritative effect evidence"
            )
        if action.status != "succeeded" or action.canonical_event_id is None:
            raise ConversationActionStateProjectionError(
                "executed approval lacks authoritative effect evidence"
            )
    elif approval.status == "stale":
        if action is not None and action.status == "succeeded":
            raise ConversationActionStateProjectionError(
                "stale approval conflicts with successful business action"
            )
    elif approval.status == "failed":
        if job is None:
            raise ConversationActionStateProjectionError(
                "failed approval lacks confirmed-zero-effect runtime evidence"
            )
        if not (
            job.status == "dead"
            or job.outcome
            in {
                "failed",
                "infrastructure_exhausted",
                "verified_zero_effect",
            }
        ):
            raise ConversationActionStateProjectionError(
                "failed approval lacks confirmed-zero-effect runtime evidence"
            )
        if action is not None and action.status in {
            "succeeded",
            "unknown",
            "verification_pending",
            "pending",
            "running",
            "executing",
        }:
            raise ConversationActionStateProjectionError(
                "failed approval conflicts with unresolved or successful business action"
            )
    elif approval.status == "manual_takeover" and action is not None:
        raise ConversationActionStateProjectionError(
            "legacy manual takeover conflicts with business action evidence"
        )


def _projection_status(sources: ConversationActionSources) -> ProjectionStatus:
    approval = sources.approval
    action = sources.business_action
    job = sources.runtime_job

    if approval.status == "manual_takeover":
        return "manual_takeover_legacy"
    if sources.withdrawal is not None or approval.status == "withdrawn":
        return "withdrawn"
    if approval.status == "executed":
        return "executed"
    if approval.status in {"rejected", "stale", "failed"}:
        return cast(ProjectionStatus, approval.status)
    if approval.status == "approved":
        if (
            (job is not None and job.outcome == "verification_pending")
            or (job is not None and job.delivery_hold_reason == "state_unknown")
            or (action is not None and action.status in {"unknown", "verification_pending"})
        ):
            return "verification_pending"
        if (job is not None and job.status == "leased") or (
            action is not None and action.status in {"pending", "running", "executing"}
        ):
            return "executing"
        return "approved"
    return "pending"


def _decision_class(
    sources: ConversationActionSources,
    projection_status: ProjectionStatus,
) -> DecisionClass:
    if projection_status == "manual_takeover_legacy" or (
        sources.decision is not None and sources.decision.decision == "manual_takeover"
    ):
        return "legacy_manual_takeover"
    if sources.withdrawal is not None or projection_status == "withdrawn":
        return "customer_withdrawal"
    if sources.decision is not None and sources.decision.decision in {
        "approve",
        "edit_and_approve",
        "reject",
    }:
        return cast(DecisionClass, sources.decision.decision)
    if projection_status in {"executed", "rejected", "stale", "failed"}:
        return "system_transition"
    return "none"


def _execution_state(projection_status: ProjectionStatus) -> ExecutionState:
    if projection_status == "pending":
        return "not_started"
    if projection_status == "approved":
        return "queued"
    if projection_status == "executing":
        return "in_progress"
    if projection_status == "verification_pending":
        return "verification_pending"
    if projection_status == "executed":
        return "succeeded"
    if projection_status in {"rejected", "stale", "withdrawn"}:
        return "not_executed"
    if projection_status == "failed":
        return "failed"
    return "legacy_stopped"


def _source_event(
    sources: ConversationActionSources,
) -> tuple[str | None, str | None]:
    status = sources.approval.status
    if status in {"withdrawn", "stale", "failed"}:
        return (
            sources.approval.transition_event_id,
            sources.approval.transition_event_hash,
        )
    if status == "executed" and sources.business_action is not None:
        return (
            sources.business_action.canonical_event_id,
            sources.business_action.canonical_event_hash,
        )
    if status in {"approved", "rejected"} and sources.decision is not None:
        return (
            sources.decision.canonical_event_id,
            sources.decision.canonical_event_hash,
        )
    if (
        status == "manual_takeover"
        and sources.decision is not None
        and sources.decision.decision == "manual_takeover"
    ):
        return (
            sources.decision.canonical_event_id,
            sources.decision.canonical_event_hash,
        )
    return (
        sources.approval.expected_ticket_head_event_id,
        sources.approval.expected_ticket_event_hash,
    )


def _latest_update(sources: ConversationActionSources) -> datetime:
    timestamps = [sources.approval.updated_at]
    for row in (
        sources.proposal,
        sources.decision,
        sources.business_action,
        sources.withdrawal,
        sources.runtime_job,
    ):
        if row is not None:
            timestamps.append(row.updated_at)
    return max(timestamps, key=_timestamp_key)


def _timestamp_key(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
