from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.contracts.canonical_json import canonical_json_hash


class _FrozenCapabilityDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_arguments: dict[str, Any]
    observation_binding_hash: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def canonical_payload_only(self) -> _FrozenCapabilityDecision:
        canonical_json_hash(self.model_dump(mode="python"))
        return self


class ProposalCausalDecisionV2(_FrozenCapabilityDecision):
    variant: Literal["proposal"] = "proposal"
    capability_name: Literal[
        "propose_refund", "propose_api_key_revocation", "propose_entitlement_change"
    ]
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    resource_id: str = Field(min_length=1, max_length=255)
    resource_version: int = Field(ge=1)

    @model_validator(mode="after")
    def capability_matches_action(self) -> ProposalCausalDecisionV2:
        expected = {
            "propose_refund": "refund",
            "propose_api_key_revocation": "api_key_revocation",
            "propose_entitlement_change": "entitlement_change",
        }
        if expected[self.capability_name] != self.action_type:
            raise ValueError("proposal capability and action type do not match")
        return self


class EscalationCausalDecisionV2(_FrozenCapabilityDecision):
    variant: Literal["escalation"] = "escalation"
    capability_name: Literal["create_support_escalation"] = "create_support_escalation"
    ticket_id: str = Field(min_length=1, max_length=255)
    ticket_version: int = Field(ge=1)
    customer_id: str = Field(min_length=1, max_length=255)


CausalDecisionV2 = Annotated[
    ProposalCausalDecisionV2 | EscalationCausalDecisionV2,
    Field(discriminator="variant"),
]

CAUSAL_DECISION_SCHEMA_VERSION = "causal-decision.v2"
