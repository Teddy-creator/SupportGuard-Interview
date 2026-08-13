from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from supportguard.api.auth import (
    CustomerContext,
    Principal,
    SubscriptionContext,
    TenantContext,
)
from supportguard.api.problems import ProductProblem
from supportguard.contracts.timestamps import (
    CanonicalUtcTimestamp,
    validate_canonical_utc_timestamp_text,
)
from supportguard.services.approval_edits import MAX_TARGET_CONCURRENCY


class SessionRequest(BaseModel):
    role: Literal["customer", "approver"]
    customer_id: str | None = None
    tenant_id: str | None = None
    external_subject: str | None = None


class SessionResponse(BaseModel):
    principal: Principal
    csrf_token: str


class SessionContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_mode: Literal["development", "production"]
    csrf_token: str | None = None
    principal: dict[str, Any]
    active_tenant: TenantContext
    customer: CustomerContext | None
    subscription: SubscriptionContext | None
    accessible_tenants: list[TenantContext]
    configured_runtime: dict[str, Any]


class MessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ApprovalInput(BaseModel):
    reason: str = Field(default="", max_length=2000)
    approver_note: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def optional_reason_is_blank_or_meaningful(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and len(normalized) < 3:
            raise ValueError("approval reason must be blank or at least 3 characters")
        return normalized


class RejectionInput(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)
    approver_note: str | None = Field(default=None, max_length=2000)


class ApprovalEditChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_reason: str | None = Field(default=None, min_length=5, max_length=2000)
    target_concurrency: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_TARGET_CONCURRENCY,
    )

    @model_validator(mode="after")
    def exactly_one_change(self) -> ApprovalEditChanges:
        if sum(value is not None for value in (self.refund_reason, self.target_concurrency)) != 1:
            raise ValueError("exactly one supported approval change is required")
        return self


class EditApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, min_length=3, max_length=2000)
    approver_note: str | None = Field(default=None, max_length=2000)
    changes: ApprovalEditChanges | None = None
    refund_reason: str | None = Field(
        default=None,
        min_length=5,
        max_length=2000,
        deprecated=True,
    )

    @model_validator(mode="after")
    def one_edit_shape(self) -> EditApprovalInput:
        if (self.changes is None) == (self.refund_reason is None):
            raise ValueError("provide exactly one canonical or legacy approval edit")
        return self

    def edited_payload(self) -> dict[str, object]:
        if self.changes is not None:
            return self.changes.model_dump(exclude_none=True)
        return {"refund_reason": self.refund_reason or ""}


class WithdrawalInput(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class CommandAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["command-accepted.v1"]
    ticket_id: str
    run_id: str | None
    job_id: str | None
    accepted_at: CanonicalUtcTimestamp
    status: Literal["queued", "accepted"]
    status_url: str | None
    events_url: str
    reused: bool

    _accepted_at_is_canonical = field_validator("accepted_at")(
        validate_canonical_utc_timestamp_text
    )


class DecisionAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["decision-accepted.v1"]
    approval_id: str
    decision: Literal["approve", "edit_and_approve", "reject"]
    ticket_id: str
    run_id: str
    job_id: str | None
    accepted_at: CanonicalUtcTimestamp
    status: Literal["decision_accepted"]
    status_url: str | None
    events_url: str
    reused: bool

    _accepted_at_is_canonical = field_validator("accepted_at")(
        validate_canonical_utc_timestamp_text
    )


class WithdrawalAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["withdrawal-accepted.v1"]
    approval_id: str
    ticket_id: str
    withdrawal_id: str
    accepted_at: CanonicalUtcTimestamp
    action_status: Literal["withdrawn"]
    reused: bool

    _accepted_at_is_canonical = field_validator("accepted_at")(
        validate_canonical_utc_timestamp_text
    )


class ConversationLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["conversation-lifecycle.v1"]
    conversation_id: str
    lifecycle: Literal["active", "archived"]
    accepted_at: CanonicalUtcTimestamp
    reused: bool

    _accepted_at_is_canonical = field_validator("accepted_at")(
        validate_canonical_utc_timestamp_text
    )


MUTATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ProductProblem,
        "description": "A durable upgrade fence rejected a new acceptance.",
    }
}

TICKET_LIST_LIMIT = 200
TICKET_MESSAGE_LIMIT = 100
TICKET_TIMELINE_LIMIT = 200
TICKET_EVIDENCE_LIMIT = 50
TICKET_FACT_LIMIT = 50
TICKET_EVENT_LIMIT = 500


class TicketListItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    issue_type: str
    risk: str
    title: str
    appendable: bool
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class PublicEventPayloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str | None = None
    freshness_status: str | None = None
    outcome: str | None = None
    projection_status: str | None = None
    route: str | None = None
    tool_name: str | None = None
    source_count: int | None = None


class InspectorEventPayloadResponse(PublicEventPayloadResponse):
    model_config = ConfigDict(extra="forbid")

    injected_tool_allowlist: list[str] | None = None
    injected_tools: list[str] | None = None
    remaining_budget: dict[str, int] | None = None
    failure_recorded: bool | None = None
    stop_condition_recorded: bool | None = None


class AgentEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    event_type: str
    payload: PublicEventPayloadResponse
    run_id: str
    ticket_sequence: int
    run_sequence: int
    step_index: int
    tool_round: int
    status: str
    created_at: datetime | str


class InspectorEventResponse(AgentEventResponse):
    payload: InspectorEventPayloadResponse


class RuntimeIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    provider: str | None = None
    provider_mode: str | None = None
    tool_call_mode: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    context_assembly_version: str | None = None
    knowledge_index_contract: str | None = None
    attempt_status: str | None = None
    source: str | None = None
    provider_transport_attempts: int | None = None
    provider_retry_count: int | None = None


class RunJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    outcome: str | None = None
    has_error: bool


class RunProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str | None = None
    status: str
    status_version: int
    finish_reason: str | None = None
    model: str
    provider_mode: str
    tool_call_mode: str
    configured_runtime: RuntimeIdentityResponse
    actual_runtime: RuntimeIdentityResponse | None
    knowledge_index_version: str | None = None
    failure_category: Literal["api_request", "provider", "tool", "runtime"] | None = None
    created_at: datetime | str
    completed_at: datetime | str | None = None
    budgets: dict[str, int]
    job: RunJobResponse | None = None


class RunInspectorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    turn_id: str
    run_id: str
    run: RunProjectionResponse
    timeline: list[InspectorEventResponse]
    knowledge_sources: list[dict[str, Any]]
    business_facts: list[dict[str, Any]]


class AggregateWindowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int
    returned: int
    total: int
    total_is_exact: bool
    has_more: bool
    boundary: str | None = None


class TicketAggregationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: AggregateWindowResponse
    timeline: AggregateWindowResponse
    knowledge_sources: AggregateWindowResponse
    business_facts: AggregateWindowResponse


class TicketDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: str
    issue_type: str
    risk: str
    version: int
    final_response: str | None = None
    appendable: bool
    allowed_actions: list[str]
    created_at: datetime | str
    updated_at: datetime | str
    messages: list[dict[str, Any]]
    summary: dict[str, Any] | None = None
    latest_run: RunProjectionResponse | None = None
    timeline: list[AgentEventResponse]
    knowledge_sources: list[dict[str, Any]]
    business_facts: list[dict[str, Any]]
    approval: dict[str, Any] | None = None
    business_action: dict[str, Any] | None = None
    aggregation: TicketAggregationResponse


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]
    next_cursor: str | None = None


class CustomerEntitlementTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    rpm_limit: int | None = Field(default=None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(default=None, ge=0, le=1_000_000)


class CustomerActionPayloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_record_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    api_key_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    subscription_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    amount: str | None = Field(default=None, pattern=r"^\d{1,12}(?:\.\d{1,2})?$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    original_billing_record_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    duplicate_pair_verified: bool | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    target: CustomerEntitlementTargetResponse | None = None

    @model_validator(mode="after")
    def exactly_one_resource_identity(self) -> CustomerActionPayloadResponse:
        identities = (
            self.billing_record_id,
            self.api_key_id,
            self.subscription_id,
        )
        if sum(value is not None for value in identities) != 1:
            raise ValueError("customer action resource identity is invalid")
        if self.billing_record_id is None and (
            self.amount is not None
            or self.currency is not None
            or self.original_billing_record_id is not None
            or self.duplicate_pair_verified is not None
            or self.service_period_start is not None
            or self.service_period_end is not None
        ):
            raise ValueError("customer action billing fields require a billing resource")
        if (self.service_period_start is None) != (self.service_period_end is None):
            raise ValueError("customer action service period must be complete")
        if (
            self.service_period_start is not None
            and self.service_period_end is not None
            and self.service_period_start >= self.service_period_end
        ):
            raise ValueError("customer action service period is invalid")
        if self.subscription_id is None and self.target is not None:
            raise ValueError("customer action target requires a subscription resource")
        return self


class ConversationActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    turn_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    status: Literal[
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
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    action_payload: CustomerActionPayloadResponse
    actionable: bool
    allowed_actions: list[Literal["withdraw"]]
    status_version: int = Field(ge=1)
    customer_safe_reason_code: str = Field(min_length=1, max_length=128)
    created_at: datetime | str
    updated_at: datetime | str


class ConversationDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    lifecycle: str
    automation_mode: str
    activity_label: str
    allowed_actions: list[str]
    turns: list[dict[str, Any]]
    pending_actions: list[ConversationActionResponse]
    turn_pagination: dict[str, Any]
    created_at: datetime | str
    updated_at: datetime | str


class ApprovalListItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str
    source_label: str
    status: str
    action_type: str
    resource_summary: str
    risk: str
    actionable: bool
    allowed_actions: list[str]
    created_at: datetime | str


class ApprovalSourceMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    kind: Literal["customer", "assistant"]
    role: Literal["customer", "assistant"]
    content: str = Field(min_length=1, max_length=8000)
    sequence: int = Field(ge=1)
    is_origin_turn: bool
    created_at: datetime | str


class ApprovalSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    ticket_id: str
    title: str
    origin_turn_id: str
    messages: list[ApprovalSourceMessageResponse]
    returned: int = Field(ge=0, le=100)
    has_more: bool
    next_before_sequence: int | None = Field(default=None, ge=1)
    next_before_message_id: str | None = Field(default=None, min_length=1, max_length=128)
