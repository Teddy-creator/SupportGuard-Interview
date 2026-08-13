from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.contracts.action_preconditions import validate_entitlement_target
from supportguard.contracts.context import McpCallContext
from supportguard.rag.types import EligibilityEnvelope, SourceLocator


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCallContext(BaseModel):
    """Trusted runtime context. Never construct this from model arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    tenant_id: str
    ticket_id: str
    run_id: str
    job_id: str
    segment_id: str
    delivery_generation: int = Field(ge=1, le=5)
    fencing_token: int = Field(ge=1)
    checkpoint_id: str | None = None
    observation_binding: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str
    trace_id: str
    mcp_context: McpCallContext | None = None

    @classmethod
    def fixture(
        cls,
        *,
        customer_id: str,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
        tool_call_id: str,
        trace_id: str,
        checkpoint_id: str | None = None,
        observation_binding: list[dict[str, Any]] | None = None,
    ) -> ToolCallContext:
        """Explicit test-only builder; production runtime never calls this method."""
        return cls(
            customer_id=customer_id,
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            run_id=run_id,
            job_id="test-fixture-job",
            segment_id="test-fixture-segment",
            delivery_generation=1,
            fencing_token=1,
            checkpoint_id=checkpoint_id,
            observation_binding=observation_binding or [],
            tool_call_id=tool_call_id,
            trace_id=trace_id,
        )


class SourceRef(BaseModel):
    source_type: Literal["business_record", "tool_result", "knowledge_chunk"]
    source_id: str
    observed_at: datetime


class ToolResultBase(BaseModel):
    tool_call_id: str
    ticket_id: str
    source_refs: list[SourceRef]


ObservationStatus = Literal[
    "ok",
    "not_found",
    "denied",
    "forbidden_tool",
    "invalid_input",
    "conflict",
    "timeout",
    "unavailable",
    "invalid_result",
]


class ObservationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["observation.v1"] = "observation.v1"
    tool_name: str
    tool_call_id: str
    ticket_id: str
    run_id: str
    tenant_id: str | None = None
    customer_id: str | None = None
    scope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attempt_index: int = Field(ge=1)
    status: ObservationStatus
    retryable: bool
    error_code: str | None = None
    safe_error_summary: str | None = None
    observed_at: datetime
    freshness_class: Literal[
        "transactional",
        "near_real_time",
        "event_record",
        "versioned_knowledge",
        "unknown",
    ] = "unknown"
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    fresh_until: datetime | None = None
    duration_ms: int = Field(ge=0)
    source_refs: list[SourceRef] = Field(default_factory=list)
    resource_version: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    # Internal carrier only: persisted on the matching AgentCallAttempt, never
    # serialized into the model-visible or customer-visible Observation.
    transport_lifecycle: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )


class AccountResult(ToolResultBase):
    account_status: str
    security_status: str
    region: str
    observed_at: datetime
    resource_version: str


class UsageResult(ToolResultBase):
    window: Literal["1m", "5m", "1h", "24h"]
    window_start: datetime
    window_end: datetime
    request_count: int
    input_token_count: int
    output_token_count: int
    concurrency_current: int
    concurrency_peak: int
    remaining_balance: Decimal
    balance_currency: str
    freshness_seconds: int
    freshness_status: Literal["fresh", "stale", "unknown"]
    observed_at: datetime
    resource_version: str


class SubscriptionResult(ToolResultBase):
    subscription_id: str
    plan: str
    status: str
    rpm_limit: int
    concurrency_limit: int
    catalog_eligibility: list[str]
    version: int


class RequestTraceResult(ToolResultBase):
    request_id: str
    model: str
    region: str
    status_code: int
    error_class: str | None
    stage_latency_ms: dict[str, int]
    observed_at: datetime
    version: int


class ApiKeyMetadataResult(ToolResultBase):
    api_key_id: str
    fingerprint: str
    status: str
    version: int
    last_used_summary: dict[str, Any]


class IncidentImpactResult(ToolResultBase):
    request_id: str
    impacted: bool | None
    incident_id: str | None
    public_incident_ref: str | None
    observed_at: datetime


class ServiceStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=64)


class ServiceStatusResult(ToolResultBase):
    model: str
    region: str
    status: str
    summary: str
    observed_at: datetime


class BillingRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_record_id: str = Field(min_length=1, max_length=64)


class BillingRecordResult(ToolResultBase):
    billing_record_id: str
    amount: Decimal
    currency: str
    status: str
    charged_at: datetime
    service_period_start: date
    service_period_end: date
    duplicate_of: str | None
    version: int
    original_billing_record_id: str | None
    original_amount: Decimal | None
    original_currency: str | None
    original_status: str | None
    original_charged_at: datetime | None
    original_service_period_start: date | None
    original_service_period_end: date | None
    original_version: int | None
    duplicate_pair_eligible: bool
    refund_pair_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    refund_pair_checks: dict[str, bool]


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)


class UsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: Literal["1m", "5m", "1h", "24h"]


class RequestTraceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)


class ApiKeyMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_ref: str = Field(min_length=1, max_length=128)


class IncidentImpactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)


class KnowledgeEvidence(BaseModel):
    evidence_id: str
    document_id: str
    document_type: str
    chunk_id: str
    title: str
    section_path: str
    version: str
    effective_at: datetime
    content_hash: str
    source_locator: SourceLocator
    chunk_locator: SourceLocator
    eligibility_envelope: EligibilityEnvelope
    supporting_span: str
    supporting_span_eligible: bool
    supporting_span_reason: str
    token_count: int
    retrieval_score: str = Field(pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
    evidence_group: Literal["current", "historical"] | None = None


class KnowledgeSearchResult(ToolResultBase):
    normalized_query: str
    evidence: list[KnowledgeEvidence]
    conflict: bool
    refusal_reason: str | None = None
    index_version: str | None = None


class EscalationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class EscalationResult(ToolResultBase):
    escalation_id: str
    status: str
    idempotency_key: str


class RefundProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_record_id: str = Field(min_length=1, max_length=64)
    refund_reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RefundProposalResult(ToolResultBase):
    proposal_id: str
    approval_id: str
    status: Literal["pending"]
    billing_record_id: str
    amount: Decimal
    currency: str
    action_hash: str
    business_version: int
    idempotency_key: str
    run_id: str
    checkpoint_id: str


class ApiKeyRevocationProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class EntitlementChangeProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(min_length=1, max_length=128)
    change_type: Literal["quota_change", "plan_change"]
    target: dict[str, Any]
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_typed_target(self) -> EntitlementChangeProposalInput:
        self.target = validate_entitlement_target(self.change_type, self.target)
        return self


class DraftProposalResult(ToolResultBase):
    proposal_id: str
    status: Literal["draft"]
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    action_hash: str
    resource_id: str
    resource_version: int
    idempotency_key: str


class RuntimeCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    fencing_token: int = Field(ge=1)


class ToolErrorPayload(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
