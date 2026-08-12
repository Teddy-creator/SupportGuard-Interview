from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceEligibilityConfig(BaseModel):
    """Frozen safety thresholds, not a retrieval-quality tuning surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evidence-eligibility.v1"] = "evidence-eligibility.v1"
    minimum_authority: int = Field(default=50, ge=0, le=100)
    cross_channel_vector_floor: float = Field(default=0.55, ge=-1, le=1)
    vector_only_floor: float = Field(default=0.72, ge=-1, le=1)


class EvidenceEligibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_intent: Literal["current", "historical", "compare"]
    status: Literal["active", "deprecated"]
    authority: int = Field(ge=0, le=100)
    scope_match: bool
    time_match: bool
    unresolved_conflict: bool = False
    missing_required_scope: bool = False
    exact_token_match: bool = False
    structured_field_match: bool = False
    vector_similarity: float = Field(ge=-1, le=1)
    keyword_channel_match: bool = False


class EvidenceEligibilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evidence-eligibility-decision.v1"] = "evidence-eligibility-decision.v1"
    outcome: Literal["eligible", "background_only", "abstain", "clarify"]
    reason_code: str


FROZEN_ELIGIBILITY_CONFIG = EvidenceEligibilityConfig()


def decide_evidence_eligibility(
    item: EvidenceEligibilityInput,
    config: EvidenceEligibilityConfig = FROZEN_ELIGIBILITY_CONFIG,
) -> EvidenceEligibilityDecision:
    if item.missing_required_scope:
        return EvidenceEligibilityDecision(outcome="clarify", reason_code="missing_scope")
    if not item.scope_match:
        return EvidenceEligibilityDecision(outcome="abstain", reason_code="scope_mismatch")
    if not item.time_match:
        return EvidenceEligibilityDecision(outcome="abstain", reason_code="time_mismatch")
    if item.authority < config.minimum_authority:
        return EvidenceEligibilityDecision(outcome="background_only", reason_code="low_authority")
    if item.unresolved_conflict:
        return EvidenceEligibilityDecision(outcome="clarify", reason_code="unresolved_conflict")
    if item.retrieval_intent == "current" and item.status != "active":
        return EvidenceEligibilityDecision(outcome="abstain", reason_code="not_current")

    cross_channel = item.keyword_channel_match and (
        item.vector_similarity >= config.cross_channel_vector_floor
    )
    absolute_support = (
        item.exact_token_match
        or item.structured_field_match
        or cross_channel
        or item.vector_similarity >= config.vector_only_floor
    )
    if not absolute_support:
        return EvidenceEligibilityDecision(outcome="abstain", reason_code="insufficient_relevance")
    return EvidenceEligibilityDecision(outcome="eligible", reason_code="supported")
