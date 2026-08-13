from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SafeIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ApprovalActionType = Literal["refund", "api_key_revocation", "entitlement_change"]
ApprovalProjectionStatus = Literal[
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
    "projection_unavailable",
]
ApprovalRisk = Literal["low", "medium", "high"]
FreshnessStatus = Literal["current", "changed_since_proposal", "unavailable"]

_ACTION_RESOURCE_TYPES: dict[str, str] = {
    "refund": "billing_record_id",
    "api_key_revocation": "api_key_id",
    "entitlement_change": "subscription_id",
}
_PROJECTION_STATUSES = {
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
}
_TICKET_STATUSES = {
    "open",
    "queued",
    "running",
    "awaiting_approval",
    "verification_pending",
    "resolved",
    "rejected",
    "failed",
    "human_queue",
    "archived",
}
_PROPOSAL_STATUSES = {"draft", "bound", "stale"}
_DECISIONS = {"approve", "edit_and_approve", "reject", "manual_takeover"}
_JOB_STATUSES = {"queued", "leased", "succeeded", "retry_wait", "dead"}
_BUSINESS_ACTION_STATUSES = {
    "pending",
    "running",
    "executing",
    "succeeded",
    "failed",
    "stale",
    "unknown",
    "verification_pending",
}
_BILLING_STATUSES = {"charged", "refunded", "pending", "failed", "void"}
_KEY_STATUSES = {"active", "revoked", "disabled", "expired"}
_SUBSCRIPTION_STATUSES = {"active", "past_due", "suspended", "cancelled", "canceled"}


class ApprovalProjectionError(RuntimeError):
    """Persisted approval data cannot be represented by the public contract."""


class _StrictProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalResourceIdentity(_StrictProjection):
    resource_type: Literal["billing_record_id", "api_key_id", "subscription_id"]
    resource_id: SafeIdentifier
    origin_turn_id: SafeIdentifier
    identity_source: Literal["persisted"] = "persisted"
    identity_complete: Literal[True] = True


class RefundActionPayload(_StrictProjection):
    billing_record_id: SafeIdentifier
    amount: str | None = Field(default=None, pattern=r"^\d{1,12}(?:\.\d{1,2})?$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    refund_reason: str | None = Field(default=None, min_length=1, max_length=500)
    original_billing_record_id: SafeIdentifier | None = None


class ApiKeyRevocationActionPayload(_StrictProjection):
    api_key_id: SafeIdentifier


class EntitlementTarget(_StrictProjection):
    plan: SafeIdentifier | None = None
    rpm_limit: int | None = Field(default=None, ge=0)
    concurrency_limit: int | None = Field(default=None, ge=0)


class EntitlementChangeActionPayload(_StrictProjection):
    subscription_id: SafeIdentifier
    change_type: Literal["quota_change", "plan_change"] | None = None
    target: EntitlementTarget | None = None


SafeActionPayload = (
    RefundActionPayload | ApiKeyRevocationActionPayload | EntitlementChangeActionPayload
)


class BillingResourceFacts(_StrictProjection):
    kind: Literal["billing_record"] = "billing_record"
    billing_record_id: SafeIdentifier
    status: Literal["charged", "refunded", "pending", "failed", "void", "unknown"]
    amount: str | None = Field(default=None, pattern=r"^\d{1,12}(?:\.\d{1,2})?$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    duplicate_of: SafeIdentifier | None = None
    version: int | None = Field(default=None, ge=1)


class ApiKeyResourceFacts(_StrictProjection):
    kind: Literal["api_key"] = "api_key"
    api_key_id: SafeIdentifier
    status: Literal["active", "revoked", "disabled", "expired", "unknown"]
    version: int | None = Field(default=None, ge=1)


class SubscriptionResourceFacts(_StrictProjection):
    kind: Literal["subscription"] = "subscription"
    subscription_id: SafeIdentifier
    status: Literal[
        "active",
        "past_due",
        "suspended",
        "cancelled",
        "canceled",
        "unknown",
    ]
    plan: SafeIdentifier | None = None
    rpm_limit: int | None = Field(default=None, ge=0)
    concurrency_limit: int | None = Field(default=None, ge=0)
    version: int | None = Field(default=None, ge=1)


SafeResourceFacts = BillingResourceFacts | ApiKeyResourceFacts | SubscriptionResourceFacts


class SafeToolObservation(_StrictProjection):
    data: SafeResourceFacts


class SafeEvidenceSummary(_StrictProjection):
    title: str = Field(min_length=1, max_length=300)
    section_path: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=64)
    freshness: FreshnessStatus


class SafeFreshnessSummary(_StrictProjection):
    status: FreshnessStatus
    proposed_version: int = Field(ge=1)
    current_version: int | None = Field(default=None, ge=1)


class SafeReviewContext(_StrictProjection):
    original_request: str | None = Field(default=None, min_length=1, max_length=8000)
    risk: ApprovalRisk
    policy_route: Literal["确定性策略与证据已绑定", "策略或证据绑定不可用"]
    freshness: SafeFreshnessSummary
    tool_observations: list[SafeToolObservation]
    evidence: list[SafeEvidenceSummary]


class ApprovalExecutionPrecondition(_StrictProjection):
    label: str = Field(min_length=1, max_length=64)
    satisfied: bool


class ApprovalProposedDiff(_StrictProjection):
    field: str = Field(min_length=1, max_length=64)
    current: str = Field(min_length=1, max_length=128)
    proposed: str = Field(min_length=1, max_length=128)


class SafeProposalSummary(_StrictProjection):
    status: Literal["draft", "bound", "stale", "unknown"]
    resource_id: SafeIdentifier
    resource_version: int = Field(ge=1)


class SafeTicketSummary(_StrictProjection):
    id: SafeIdentifier
    title: Literal["客户支持会话"] = "客户支持会话"
    status: Literal[
        "open",
        "queued",
        "running",
        "awaiting_approval",
        "verification_pending",
        "resolved",
        "rejected",
        "failed",
        "human_queue",
        "archived",
        "unknown",
    ]
    issue_type: ApprovalActionType
    risk: ApprovalRisk


class SafeHumanDecisionSummary(_StrictProjection):
    decision: Literal["approve", "edit_and_approve", "reject", "manual_takeover"]
    created_at: datetime


class SafeResumeJobSummary(_StrictProjection):
    status: Literal["queued", "leased", "succeeded", "retry_wait", "dead", "unknown"]
    outcome: Literal["pending", "completed", "verification_pending", "failed", "unknown"]


class SafeBusinessActionSummary(_StrictProjection):
    status: Literal[
        "pending",
        "running",
        "executing",
        "succeeded",
        "failed",
        "stale",
        "unknown",
        "verification_pending",
    ]
    action_type: ApprovalActionType
    resource_id: SafeIdentifier
    resource_version: int | None = Field(default=None, ge=1)
    created_at: datetime


class ApprovalDetailResponse(_StrictProjection):
    """Strict reviewer projection.

    Every nested object is an allowlisted DTO.  Raw review context, tool
    observations, hashes, idempotency material, actor ids, exception text and
    business-action results have no representable field in this contract.
    """

    id: SafeIdentifier
    ticket_id: SafeIdentifier
    status: ApprovalProjectionStatus
    action_type: ApprovalActionType
    resource_type: Literal["billing_record_id", "api_key_id", "subscription_id"]
    resource_id: SafeIdentifier
    origin_turn_id: SafeIdentifier
    resource_identity: ApprovalResourceIdentity
    action_payload: SafeActionPayload
    review_context: SafeReviewContext
    business_version: int = Field(ge=1)
    status_version: int = Field(ge=1)
    resource_summary: SafeIdentifier
    risk: ApprovalRisk
    actionable: bool
    allowed_actions: list[Literal["approve", "edit_and_approve", "reject"]]
    execution_preconditions: list[ApprovalExecutionPrecondition]
    proposed_diff: list[ApprovalProposedDiff]
    proposal: SafeProposalSummary | None = None
    ticket: SafeTicketSummary | None = None
    human_decision: SafeHumanDecisionSummary | None = None
    resume_job: SafeResumeJobSummary | None = None
    business_action: SafeBusinessActionSummary | None = None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _known(value: object, allowed: set[str], *, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _integer(value: object, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= minimum:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) >= minimum:
        return int(value)
    return None


def _money(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1000000000000"):
        return None
    return f"{amount:.2f}"


def _currency(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha():
        return value.upper()
    return None


def _safe_plan(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return EntitlementTarget(plan=value).plan
    except ValidationError:
        return None


def _projection_status(value: object) -> ApprovalProjectionStatus:
    if isinstance(value, str) and value in _PROJECTION_STATUSES:
        return value  # type: ignore[return-value]
    return "projection_unavailable"


def _risk(value: object) -> ApprovalRisk:
    if value in {"low", "medium", "high"}:
        return value  # type: ignore[return-value]
    return "high"


def _resource_facts(
    action_type: ApprovalActionType,
    resource_id: str,
    raw: Mapping[str, Any],
) -> SafeResourceFacts | None:
    if not raw:
        return None
    current_version = _integer(raw.get("version"), minimum=1)
    if action_type == "refund":
        duplicate = raw.get("duplicate_of")
        try:
            return BillingResourceFacts(
                billing_record_id=resource_id,
                status=_known(raw.get("status"), _BILLING_STATUSES),
                amount=_money(raw.get("amount")),
                currency=_currency(raw.get("currency")),
                duplicate_of=duplicate if isinstance(duplicate, str) else None,
                version=current_version,
            )
        except ValidationError as exc:
            raise ApprovalProjectionError("approval billing facts are invalid") from exc
    if action_type == "api_key_revocation":
        try:
            return ApiKeyResourceFacts(
                api_key_id=resource_id,
                status=_known(raw.get("status"), _KEY_STATUSES),
                version=current_version,
            )
        except ValidationError as exc:
            raise ApprovalProjectionError("approval key facts are invalid") from exc
    try:
        return SubscriptionResourceFacts(
            subscription_id=resource_id,
            status=_known(raw.get("status"), _SUBSCRIPTION_STATUSES),
            plan=_safe_plan(raw.get("plan")),
            rpm_limit=_integer(raw.get("rpm_limit")),
            concurrency_limit=_integer(raw.get("concurrency_limit")),
            version=current_version,
        )
    except ValidationError as exc:
        raise ApprovalProjectionError("approval subscription facts are invalid") from exc


def _action_payload(
    action_type: ApprovalActionType,
    resource_id: str,
    resource_facts: SafeResourceFacts | None,
    requested_change: Mapping[str, Any],
) -> SafeActionPayload:
    if action_type == "refund":
        facts = resource_facts if isinstance(resource_facts, BillingResourceFacts) else None
        refund_reason = requested_change.get("refund_reason")
        return RefundActionPayload(
            billing_record_id=resource_id,
            amount=None if facts is None else facts.amount,
            currency=None if facts is None else facts.currency,
            refund_reason=(
                refund_reason.strip()[:500]
                if isinstance(refund_reason, str) and refund_reason.strip()
                else None
            ),
            original_billing_record_id=(facts.duplicate_of if facts is not None else None),
        )
    if action_type == "api_key_revocation":
        return ApiKeyRevocationActionPayload(api_key_id=resource_id)
    change_type = requested_change.get("change_type")
    if change_type not in {"quota_change", "plan_change"}:
        change_type = None
    raw_target = _mapping(requested_change.get("target"))
    target = EntitlementTarget(
        plan=_safe_plan(raw_target.get("plan")),
        rpm_limit=_integer(raw_target.get("rpm_limit")),
        concurrency_limit=_integer(raw_target.get("concurrency_limit")),
    )
    return EntitlementChangeActionPayload(
        subscription_id=resource_id,
        change_type=change_type,
        target=target if any(value is not None for value in target.model_dump().values()) else None,
    )


def _freshness(
    *,
    proposed_version: int,
    facts: SafeResourceFacts | None,
) -> SafeFreshnessSummary:
    current_version = None if facts is None else facts.version
    status: FreshnessStatus
    if current_version is None:
        status = "unavailable"
    elif current_version == proposed_version:
        status = "current"
    else:
        status = "changed_since_proposal"
    return SafeFreshnessSummary(
        status=status,
        proposed_version=proposed_version,
        current_version=current_version,
    )


def _proposed_diff(
    action_type: ApprovalActionType,
    action_payload: SafeActionPayload,
    facts: SafeResourceFacts | None,
    *,
    requested_change: Mapping[str, Any],
) -> list[ApprovalProposedDiff]:
    if action_type == "refund":
        refund_payload = action_payload if isinstance(action_payload, RefundActionPayload) else None
        billing_status = facts.status if isinstance(facts, BillingResourceFacts) else "unknown"
        proposed = "按执行前重校验的账单金额退款"
        if refund_payload is not None and refund_payload.amount is not None:
            proposed = f"退款 {refund_payload.amount}"
            if refund_payload.currency is not None:
                proposed = f"{proposed} {refund_payload.currency}"
        return [
            ApprovalProposedDiff(
                field="账单退款状态",
                current=billing_status,
                proposed=proposed,
            ),
            ApprovalProposedDiff(
                field="退款理由",
                current="无",
                proposed=(
                    refund_payload.refund_reason
                    if refund_payload is not None and refund_payload.refund_reason is not None
                    else "按原始审批快照"
                ),
            ),
        ]
    if action_type == "api_key_revocation":
        key_status = facts.status if isinstance(facts, ApiKeyResourceFacts) else "unknown"
        return [
            ApprovalProposedDiff(
                field="API Key 状态",
                current=key_status,
                proposed="已撤销",
            )
        ]
    entitlement_payload = (
        action_payload if isinstance(action_payload, EntitlementChangeActionPayload) else None
    )
    target = None if entitlement_payload is None else entitlement_payload.target
    subscription = facts if isinstance(facts, SubscriptionResourceFacts) else None
    raw_original = _mapping(requested_change.get("current"))
    original = EntitlementTarget(
        plan=_safe_plan(raw_original.get("plan")),
        rpm_limit=_integer(raw_original.get("rpm_limit")),
        concurrency_limit=_integer(raw_original.get("concurrency_limit")),
    )
    changes: list[ApprovalProposedDiff] = []
    if target is not None:
        for field in ("plan", "rpm_limit", "concurrency_limit"):
            value = getattr(target, field)
            if value is None:
                continue
            current_value = getattr(original, field)
            if current_value is None and subscription is not None:
                current_value = getattr(subscription, field)
            changes.append(
                ApprovalProposedDiff(
                    field=field,
                    current="unknown" if current_value is None else str(current_value),
                    proposed=str(value),
                )
            )
    return changes or [
        ApprovalProposedDiff(
            field="账号权益",
            current="当前值将在执行前重校验",
            proposed="按审批快照调整",
        )
    ]


def _resume_job(raw: Mapping[str, Any]) -> SafeResumeJobSummary | None:
    if not raw:
        return None
    status = _known(raw.get("status"), _JOB_STATUSES)
    if status == "succeeded":
        outcome = "completed"
    elif status == "dead":
        outcome = "failed"
    elif raw.get("verification_pending") is True:
        outcome = "verification_pending"
    elif status in {"queued", "leased", "retry_wait"}:
        outcome = "pending"
    else:
        outcome = "unknown"
    return SafeResumeJobSummary(status=status, outcome=outcome)


def _business_action(
    raw: Mapping[str, Any],
    *,
    action_type: ApprovalActionType,
    resource_id: str,
) -> SafeBusinessActionSummary | None:
    if not raw:
        return None
    created_at = raw.get("created_at")
    if created_at is None:
        return None
    try:
        return SafeBusinessActionSummary(
            status=_known(raw.get("status"), _BUSINESS_ACTION_STATUSES),
            action_type=action_type,
            resource_id=resource_id,
            resource_version=_integer(raw.get("resource_version"), minimum=1),
            created_at=created_at,
        )
    except ValidationError as exc:
        raise ApprovalProjectionError("approval business state is invalid") from exc


def _original_request(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:8000] if normalized else None


def _evidence_summaries(value: object) -> list[SafeEvidenceSummary]:
    if not isinstance(value, list):
        return []
    projected: list[SafeEvidenceSummary] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value[:20]:
        item = _mapping(raw)
        title = item.get("title")
        section_path = item.get("section_path")
        version = item.get("version")
        freshness = item.get("freshness")
        if (
            not isinstance(title, str)
            or not isinstance(section_path, str)
            or not isinstance(version, str)
            or freshness not in {"current", "changed_since_proposal", "unavailable"}
        ):
            continue
        identity = (title, section_path, version)
        if identity in seen:
            continue
        try:
            projected.append(
                SafeEvidenceSummary(
                    title=title,
                    section_path=section_path,
                    version=version,
                    freshness=cast(FreshnessStatus, freshness),
                )
            )
        except ValidationError:
            continue
        seen.add(identity)
    return projected


def project_approval_detail(raw: Mapping[str, Any]) -> ApprovalDetailResponse:
    """Rebuild Approval Detail from a strict safe allowlist.

    ``review_context`` is intentionally never read.  Callers may pass legacy
    capability rows containing it; the entire object is discarded.
    """

    action_type_raw = raw.get("action_type")
    if action_type_raw not in _ACTION_RESOURCE_TYPES:
        raise ApprovalProjectionError("approval action type is unsupported")
    action_type = cast(ApprovalActionType, action_type_raw)
    resource_type = raw.get("resource_type")
    if resource_type != _ACTION_RESOURCE_TYPES[action_type]:
        raise ApprovalProjectionError("approval resource type conflicts with action")
    resource_id = raw.get("resource_id")
    origin_turn_id = raw.get("origin_turn_id")
    if not isinstance(resource_id, str) or not isinstance(origin_turn_id, str):
        raise ApprovalProjectionError("approval resource identity is incomplete")

    business_version = _integer(raw.get("business_version"), minimum=1)
    status_version = _integer(raw.get("status_version"), minimum=1)
    if business_version is None or status_version is None:
        raise ApprovalProjectionError("approval version is invalid")

    facts = _resource_facts(
        action_type,
        resource_id,
        _mapping(raw.get("resource_facts")),
    )
    requested_change = _mapping(raw.get("requested_change"))
    action_payload = _action_payload(
        action_type,
        resource_id,
        facts,
        requested_change,
    )
    freshness = _freshness(proposed_version=business_version, facts=facts)
    snapshot = _mapping(raw.get("snapshot_summary"))
    evidence = _evidence_summaries(raw.get("evidence_summaries"))
    citation_count = len(evidence)
    policy_bound = snapshot.get("policy_bound") is True
    observations = [] if facts is None else [SafeToolObservation(data=facts)]
    risk = _risk(raw.get("risk"))

    status_value = _projection_status(raw.get("status"))
    actionable = bool(raw.get("actionable")) and status_value == "pending"
    allowed_actions: list[Literal["approve", "edit_and_approve", "reject"]] = []
    if actionable:
        allowed_actions = ["approve", "reject"]
        if action_type in {"refund", "entitlement_change"}:
            allowed_actions.insert(1, "edit_and_approve")

    proposal_raw = _mapping(raw.get("proposal_summary"))
    proposal = None
    if proposal_raw:
        try:
            proposal = SafeProposalSummary(
                status=_known(proposal_raw.get("status"), _PROPOSAL_STATUSES),
                resource_id=resource_id,
                resource_version=(
                    _integer(proposal_raw.get("resource_version"), minimum=1) or business_version
                ),
            )
        except ValidationError as exc:
            raise ApprovalProjectionError("approval proposal summary is invalid") from exc

    ticket_raw = _mapping(raw.get("ticket_summary"))
    ticket = None
    if ticket_raw:
        try:
            ticket = SafeTicketSummary(
                id=raw.get("ticket_id"),
                status=_known(ticket_raw.get("status"), _TICKET_STATUSES),
                issue_type=action_type,
                risk=risk,
            )
        except ValidationError as exc:
            raise ApprovalProjectionError("approval ticket summary is invalid") from exc

    decision_raw = _mapping(raw.get("human_decision_summary"))
    decision = None
    if decision_raw and decision_raw.get("decision") in _DECISIONS:
        try:
            decision = SafeHumanDecisionSummary(
                decision=decision_raw["decision"],
                created_at=decision_raw.get("created_at"),
            )
        except ValidationError as exc:
            raise ApprovalProjectionError("approval decision summary is invalid") from exc

    preconditions = [
        ApprovalExecutionPrecondition(
            label="申请仍处于待审批状态",
            satisfied=status_value == "pending",
        ),
        ApprovalExecutionPrecondition(
            label="提案与检查点绑定有效",
            satisfied=actionable,
        ),
        ApprovalExecutionPrecondition(
            label="策略与证据快照已持久化",
            satisfied=policy_bound and citation_count > 0,
        ),
        ApprovalExecutionPrecondition(
            label="业务提案仍可执行",
            satisfied=proposal is not None and proposal.status == "bound",
        ),
        ApprovalExecutionPrecondition(
            label="资源版本与提案一致",
            satisfied=freshness.status == "current",
        ),
    ]

    try:
        return ApprovalDetailResponse(
            id=raw.get("id"),
            ticket_id=raw.get("ticket_id"),
            status=status_value,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            origin_turn_id=origin_turn_id,
            resource_identity=ApprovalResourceIdentity(
                resource_type=resource_type,
                resource_id=resource_id,
                origin_turn_id=origin_turn_id,
            ),
            action_payload=action_payload,
            review_context=SafeReviewContext(
                original_request=_original_request(raw.get("original_request")),
                risk=risk,
                policy_route=(
                    "确定性策略与证据已绑定"
                    if policy_bound and citation_count > 0
                    else "策略或证据绑定不可用"
                ),
                freshness=freshness,
                tool_observations=observations,
                evidence=evidence,
            ),
            business_version=business_version,
            status_version=status_version,
            resource_summary=resource_id,
            risk=risk,
            actionable=actionable,
            allowed_actions=allowed_actions,
            execution_preconditions=preconditions,
            proposed_diff=_proposed_diff(
                action_type,
                action_payload,
                facts,
                requested_change=requested_change,
            ),
            proposal=proposal,
            ticket=ticket,
            human_decision=decision,
            resume_job=_resume_job(_mapping(raw.get("resume_job_summary"))),
            business_action=_business_action(
                _mapping(raw.get("business_action_summary")),
                action_type=action_type,
                resource_id=resource_id,
            ),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            decided_at=raw.get("decided_at"),
            consumed_at=raw.get("consumed_at"),
        )
    except ValidationError as exc:
        raise ApprovalProjectionError("approval detail projection is invalid") from exc
