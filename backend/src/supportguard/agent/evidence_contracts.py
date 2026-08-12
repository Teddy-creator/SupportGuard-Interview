from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceGroup = Literal[
    "knowledge",
    "request_trace",
    "billing_record",
    "api_key_metadata",
    "subscription",
    "account",
    "api_usage",
]


class EvidenceRequirements(BaseModel):
    """Evidence capabilities required before a CandidateResponse is requested."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evidence-requirements.v1"] = "evidence-requirements.v1"
    required_groups: tuple[EvidenceGroup, ...] = ()

    @model_validator(mode="after")
    def validate_unique_groups(self) -> EvidenceRequirements:
        if len(self.required_groups) != len(set(self.required_groups)):
            raise ValueError("evidence requirement groups must be unique")
        return self


class EligibleCitation(BaseModel):
    """A citation admitted from the current Provider context membership."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_binding_id: str = Field(min_length=1, max_length=128)
    provider_attempt_id: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=256)
    document_id: str = Field(min_length=1, max_length=256)
    chunk_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FreshScopedObservation(BaseModel):
    """Identity projection of one fresh Observation in the current scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    customer_id: str = Field(min_length=1, max_length=128)
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    fresh_until: datetime
    source_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    resource_version: str | None = None


class EvidenceDecision(BaseModel):
    """Frozen evidence capability snapshot produced before CandidateResponse.

    It carries no model-authored claim and grants neither publication nor
    action authority. The later Policy stage binds CandidateResponse claims to
    these eligible identities.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evidence-decision.v1"] = "evidence-decision.v1"
    run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    customer_id: str = Field(min_length=1, max_length=128)
    provider_attempt_id: str = Field(min_length=1, max_length=128)
    requirements: EvidenceRequirements
    sufficient: bool
    result: Literal["accept", "replan", "terminal"]
    eligible_citations: tuple[EligibleCitation, ...] = ()
    fresh_scoped_observations: tuple[FreshScopedObservation, ...] = ()
    satisfied_groups: tuple[EvidenceGroup, ...] = ()
    missing_groups: tuple[EvidenceGroup, ...] = ()
    stale_groups: tuple[EvidenceGroup, ...] = ()
    conflict_reasons: tuple[str, ...] = ()
    insufficient_reasons: tuple[str, ...] = ()
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_decision_contract(self) -> EvidenceDecision:
        if self.sufficient != (self.result == "accept"):
            raise ValueError("only an accepted EvidenceDecision can be sufficient")
        if self.sufficient and (self.insufficient_reasons or self.error_code is not None):
            raise ValueError("an accepted EvidenceDecision cannot carry insufficiency")
        if not self.sufficient and not self.insufficient_reasons:
            raise ValueError("a failed EvidenceDecision requires a stable reason")
        if any(
            item.run_id != self.run_id
            or item.tenant_id != self.tenant_id
            or item.customer_id != self.customer_id
            for item in self.fresh_scoped_observations
        ):
            raise ValueError("fresh observations must match the decision scope")
        citation_ids = [item.citation_binding_id for item in self.eligible_citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("eligible citation identities must be unique")
        observation_ids = [item.observation_id for item in self.fresh_scoped_observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("fresh observation identities must be unique")
        if any(
            item.provider_attempt_id != self.provider_attempt_id for item in self.eligible_citations
        ):
            raise ValueError("eligible citations must match the decision Provider attempt")
        resolved = set(self.satisfied_groups) | set(self.missing_groups) | set(self.stale_groups)
        if resolved != set(self.requirements.required_groups):
            raise ValueError("every required evidence group needs exactly one disposition")
        if any(
            len(groups) != len(set(groups))
            for groups in (self.satisfied_groups, self.missing_groups, self.stale_groups)
        ):
            raise ValueError("each evidence group disposition must be unique")
        if (
            set(self.satisfied_groups) & set(self.missing_groups)
            or set(self.satisfied_groups) & set(self.stale_groups)
            or set(self.missing_groups) & set(self.stale_groups)
        ):
            raise ValueError("evidence group dispositions cannot overlap")
        return self
