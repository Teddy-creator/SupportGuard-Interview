from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.actions.service import get_action_spec_by_proposal
from supportguard.agent.constants import MAX_READ_TOOL_CALLS_PER_DECISION
from supportguard.tools.gateway import ReadToolCall


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: Literal[
        "product_knowledge",
        "api_diagnostics",
        "incident_support",
        "billing_refund",
        "credential_security",
        "entitlement_change",
        "unknown",
    ]
    risk: Literal["low", "medium", "high", "critical"]
    policy_boundary: Literal["allowed", "out_of_scope", "prohibited"]
    requested_action: Literal["none", "refund", "api_key_revocation", "entitlement_change"]
    requested_concurrency_limit: int | None = Field(ge=1, le=100_000)
    needs_realtime_facts: bool
    support_subject: Literal[
        "customer_problem",
        "supportguard_identity",
        "supportguard_capabilities",
        "supportguard_greeting",
    ]
    rationale: str = Field(max_length=500)

    @model_validator(mode="after")
    def validate_requested_action_target(self) -> Classification:
        if (
            self.requested_concurrency_limit is not None
            and self.requested_action != "entitlement_change"
        ):
            raise ValueError("requested_concurrency_limit is only valid for entitlement_change")
        return self


class ReadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: list[ReadToolCall] = Field(max_length=3)


class NativeReadToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=128)
    call: ReadToolCall


class CandidateCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_binding_id: str = Field(min_length=1, max_length=64)


class ProviderMaterialClaim(BaseModel):
    """One provider-selected claim before Runtime binds canonical evidence identity."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "anyOf": [
                {
                    "properties": {
                        "citation_binding_ids": {"minItems": 1},
                    }
                },
                {
                    "properties": {
                        "observation_source_ids": {"minItems": 1},
                    }
                },
            ]
        },
    )

    text: str = Field(min_length=1, max_length=1000)
    citation_binding_ids: list[str] = Field(default_factory=list)
    observation_source_ids: list[str] = Field(default_factory=list)


class ProviderBoundEvidenceSynthesis(BaseModel):
    """Authority-free provider output without Runtime-derived identity mirrors."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bound-evidence-synthesis.v1"] = "bound-evidence-synthesis.v1"
    answer: str = Field(min_length=1, max_length=4000)
    material_claims: list[ProviderMaterialClaim] = Field(min_length=1)


class GroundedRepairEligibility(BaseModel):
    """Content-free authority summary for one bounded terminal repair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["grounded-repair-eligibility.v1"] = "grounded-repair-eligibility.v1"
    selected: bool
    reason_code: Literal[
        "selected",
        "obligation_synthesis_active",
        "action_admission_schema_invalid",
        "action_admission_active",
        "eligible_authority_missing",
    ]
    require_knowledge_source: bool
    require_business_source: bool
    context_evidence_count: int = Field(ge=0)
    eligible_knowledge_count: int = Field(ge=0)
    eligible_knowledge_group_counts: dict[str, int] = Field(default_factory=dict)
    successful_knowledge_observation_count: int = Field(ge=0)
    successful_business_observation_count: int = Field(ge=0)
    unique_business_source_count: int = Field(ge=0)
    knowledge_comparison_complete: bool

    @model_validator(mode="after")
    def validate_selection_contract(self) -> GroundedRepairEligibility:
        has_authority = self.require_knowledge_source or self.require_business_source
        if self.selected != has_authority:
            raise ValueError("selected must exactly match the admitted authority namespaces")
        if self.selected != (self.reason_code == "selected"):
            raise ValueError("selected eligibility requires the selected reason code")
        if sum(self.eligible_knowledge_group_counts.values()) != self.eligible_knowledge_count:
            raise ValueError("knowledge group counts must cover every eligible item")
        if any(count < 0 for count in self.eligible_knowledge_group_counts.values()):
            raise ValueError("knowledge group counts must be nonnegative")
        return self


class MaterialClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    citation_binding_ids: list[str] = Field(default_factory=list)
    knowledge_locator_hashes: list[str] = Field(default_factory=list)
    observation_source_ids: list[str] = Field(default_factory=list)


class BoundEvidenceSynthesis(BaseModel):
    """Runtime-bound explanation with canonical evidence identities."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bound-evidence-synthesis.v1"] = "bound-evidence-synthesis.v1"
    answer: str = Field(min_length=1, max_length=4000)
    knowledge_chunk_ids: list[str]
    knowledge_citations: list[CandidateCitation] = Field(default_factory=list)
    business_source_ids: list[str]
    material_claims: list[MaterialClaim] = Field(min_length=1)


class EscalationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=2000)


class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    action: Literal[
        "answer",
        "escalate",
        "refund_proposal",
        "api_key_revocation_proposal",
        "entitlement_change_proposal",
        "reject",
        "manual_takeover",
    ]
    knowledge_chunk_ids: list[str]
    knowledge_citations: list[CandidateCitation] = Field(default_factory=list)
    business_source_ids: list[str]
    material_claims: list[MaterialClaim] = Field(default_factory=list)
    proposed_arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def validate_typed_action_draft(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        action = value.get("action")
        arguments = value.get("proposed_arguments", {})
        action_spec = get_action_spec_by_proposal(str(action))
        schema = (
            EscalationDraft
            if action == "escalate"
            else action_spec.proposal_schema
            if action_spec is not None
            else None
        )
        if schema is None:
            if arguments not in ({}, None):
                raise ValueError(f"{action} does not accept proposed_arguments")
            return {**value, "proposed_arguments": {}}
        parsed = schema.model_validate(arguments)
        return {**value, "proposed_arguments": parsed.model_dump(mode="json")}


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_type: Literal[
        "tool_calls", "final_candidate", "needs_clarification", "manual_takeover"
    ]
    decision_summary: str = Field(min_length=1, max_length=500)
    tool_calls: list[NativeReadToolCall] = Field(
        default_factory=list,
        max_length=MAX_READ_TOOL_CALLS_PER_DECISION,
    )
    candidate: CandidateResponse | None = None
    clarification_question: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> AgentDecision:
        if self.decision_type == "tool_calls":
            if not self.tool_calls or self.candidate is not None or self.clarification_question:
                raise ValueError("tool_calls requires calls and forbids terminal fields")
        elif self.decision_type == "final_candidate":
            if self.candidate is None or self.tool_calls or self.clarification_question:
                raise ValueError("final_candidate requires exactly one candidate")
        elif self.decision_type == "needs_clarification":
            if (
                not self.clarification_question
                or self.tool_calls
                or (self.candidate is not None and self.candidate.action != "answer")
            ):
                raise ValueError("needs_clarification requires exactly one question")
        elif self.tool_calls or self.clarification_question:
            raise ValueError("manual_takeover forbids tool calls and clarification")
        return self


class ProposalEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-eligibility.v1"] = "proposal-eligibility.v1"
    eligible: bool
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"] | None
    resource_type: str | None = None
    resource_id: str | None = None
    resource_version: int | None = None
    trusted_arguments: dict[str, Any] = Field(default_factory=dict)
    observation_binding: list[dict[str, Any]] = Field(default_factory=list)
    citation_binding_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None


class FinalResponse(BaseModel):
    answer: str
    terminal_state: str
    knowledge_chunk_ids: list[str]
    business_source_ids: list[str]
    material_claims: list[MaterialClaim] = Field(default_factory=list)
    policy_route: str
