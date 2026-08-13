from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from supportguard.contracts.action_preconditions import (
    ActionType,
    validate_entitlement_target,
)
from supportguard.contracts.tools import ObservationEnvelope, ToolCallContext
from supportguard.tools.gateway import ActionToolCall, ToolGateway

ActionIssueType = Literal["billing_refund", "credential_security", "entitlement_change"]
ObligationKind = Literal["resource", "knowledge", "usage"]
ProposalAction = Literal[
    "refund_proposal",
    "api_key_revocation_proposal",
    "entitlement_change_proposal",
]
PolicyCapability = Literal[
    "propose_refund",
    "propose_api_key_revocation",
    "propose_entitlement_change",
]
RuntimeEffectCapability = Literal[
    "execute_refund",
    "execute_api_key_revocation",
    "execute_entitlement_change",
]
ApprovalDecisionAction = Literal["approve", "edit_and_approve", "reject", "manual_takeover"]
RuntimeEffectStatus = Literal[
    "succeeded",
    "stale",
    "execution_pending",
    "execution_precondition_failed",
    "approved",
    "rejected",
    "manual_takeover",
]
ProposalFieldSource = Literal["admission", "observation", "request_reason"]
TerminalOutcomeClass = Literal["action_ineligible", "resource_not_available"]
TerminalOutcomePredicate = Literal[
    "observation_status_in",
    "resource_status_not_allowed",
    "observation_field_falsy",
    "admission_not_in_observation",
    "admission_equals_observation",
]
TerminalOutcomeMessageKey = Literal[
    "refund_resource_not_available",
    "refund_status_not_actionable",
    "refund_duplicate_relation_unconfirmed",
    "api_key_resource_not_available",
    "api_key_status_not_actionable",
    "subscription_resource_not_available",
    "subscription_status_not_actionable",
    "entitlement_target_unsupported",
    "entitlement_target_noop",
]
LeaseT = TypeVar("LeaseT")
CapabilityT = TypeVar("CapabilityT")


class RefundProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_record_id: str = Field(min_length=1, max_length=64)
    refund_reason: str = Field(min_length=5, max_length=2000)


class ApiKeyRevocationProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=5, max_length=2000)


class EntitlementChangeProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(min_length=1, max_length=128)
    change_type: Literal["quota_change", "plan_change"]
    target: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=2000)

    @model_validator(mode="after")
    def validate_typed_target(self) -> EntitlementChangeProposalArguments:
        self.target = validate_entitlement_target(self.change_type, self.target)
        return self


class EvidenceObligationSpec(BaseModel):
    """One deterministic evidence contract required before proposal assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    obligation_id: str = Field(min_length=1, max_length=64)
    kind: ObligationKind
    capabilities: tuple[str, ...] = Field(min_length=1)
    required_data_fields: tuple[str, ...] = ()
    required_truthy_data_fields: tuple[str, ...] = ()
    resource_ref_argument: str | None = None
    observed_resource_field: str | None = None
    allowed_resource_statuses: tuple[str, ...] = ()
    require_freshness: bool = True
    policy_family: str | None = None
    topic: str | None = None
    allowed_document_keys: tuple[str, ...] = ()
    allowed_document_types: tuple[str, ...] = ()
    allowed_section_terms: tuple[str, ...] = ()
    minimum_version: str | None = None

    @model_validator(mode="after")
    def validate_kind_contract(self) -> EvidenceObligationSpec:
        if any(
            field not in self.required_data_fields for field in self.required_truthy_data_fields
        ):
            raise ValueError("truthy resource fields must also be required")
        if self.kind == "knowledge":
            required = (
                self.policy_family,
                self.topic,
                self.allowed_document_keys,
                self.allowed_document_types,
                self.allowed_section_terms,
                self.minimum_version,
            )
            if not all(required) or self.capabilities != ("search_knowledge",):
                raise ValueError("knowledge obligation requires a frozen retrieval contract")
        elif any(
            (
                self.policy_family,
                self.topic,
                self.allowed_document_keys,
                self.allowed_document_types,
                self.allowed_section_terms,
                self.minimum_version,
            )
        ):
            raise ValueError("only knowledge obligations accept retrieval constraints")
        return self


class ActionErrorCodes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    missing: str
    stale: str
    conflict: str
    terminal: str


class ProposalFieldBinding(BaseModel):
    """Declarative field projection consumed by the generic proposal assembler."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_field: str
    source: ProposalFieldSource
    source_path: str
    obligation_id: str | None = None

    @model_validator(mode="after")
    def validate_observation_binding(self) -> ProposalFieldBinding:
        if (self.source == "observation") != (self.obligation_id is not None):
            raise ValueError("observation proposal fields require one obligation")
        return self


class TerminalOutcomeRule(BaseModel):
    """One bounded, registry-owned non-actionable business-state rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_code: str = Field(min_length=1, max_length=100)
    terminal_class: TerminalOutcomeClass
    obligation_id: str = Field(min_length=1, max_length=64)
    predicate: TerminalOutcomePredicate
    observation_field: str | None = None
    admission_path: str | None = None
    match_values: tuple[str, ...] = ()
    public_observation_fields: tuple[str, ...] = ()
    public_admission_paths: tuple[str, ...] = ()
    customer_message_key: TerminalOutcomeMessageKey
    recommended_next_step: str = Field(min_length=1, max_length=100)
    require_business_source: bool = True

    @model_validator(mode="after")
    def validate_predicate_shape(self) -> TerminalOutcomeRule:
        if self.predicate == "observation_status_in":
            if not self.match_values or self.observation_field or self.admission_path:
                raise ValueError("observation-status rule requires only match_values")
        elif self.predicate == "resource_status_not_allowed":
            if self.observation_field != "status" or self.admission_path:
                raise ValueError("resource-status rule requires the status field")
        elif self.predicate == "observation_field_falsy":
            if not self.observation_field or self.admission_path:
                raise ValueError("field-falsy rule requires one observation field")
        elif self.predicate in {
            "admission_not_in_observation",
            "admission_equals_observation",
        } and (not self.observation_field or not self.admission_path):
            raise ValueError("admission comparison requires both field paths")
        if self.terminal_class == "resource_not_available" and self.require_business_source:
            raise ValueError("resource-not-available must not invent a business source")
        return self


class ActionSpec(BaseModel):
    """Shared action contract; it grants no Policy or Runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-spec.v2"] = "action-spec.v2"
    action_type: ActionType
    issue_type: ActionIssueType
    admission_required_fields: tuple[str, ...] = Field(min_length=1)
    obligations: tuple[EvidenceObligationSpec, ...] = Field(min_length=1)
    proposal_action: ProposalAction
    proposal_schema_name: ProposalAction
    proposal_schema: SkipJsonSchema[type[BaseModel]] = Field(exclude=True)
    policy_capability: PolicyCapability
    runtime_effect_capability: RuntimeEffectCapability
    proposal_fields: tuple[ProposalFieldBinding, ...] = Field(min_length=1)
    terminal_outcomes: tuple[TerminalOutcomeRule, ...] = Field(min_length=1)
    clarification_fields: tuple[str, ...] = Field(min_length=1)
    error_codes: ActionErrorCodes

    @model_validator(mode="after")
    def validate_unique_obligations(self) -> ActionSpec:
        obligation_ids = [item.obligation_id for item in self.obligations]
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("ActionSpec obligation IDs must be unique")
        proposal_targets = [item.target_field for item in self.proposal_fields]
        if len(proposal_targets) != len(set(proposal_targets)):
            raise ValueError("ActionSpec proposal target fields must be unique")
        if any(
            item.obligation_id not in obligation_ids
            for item in self.proposal_fields
            if item.obligation_id is not None
        ):
            raise ValueError("proposal field references an unknown obligation")
        if any(item.obligation_id not in obligation_ids for item in self.terminal_outcomes):
            raise ValueError("terminal outcome references an unknown obligation")
        outcome_codes = [item.outcome_code for item in self.terminal_outcomes]
        if len(outcome_codes) != len(set(outcome_codes)):
            raise ValueError("terminal outcome codes must be unique")
        if self.proposal_schema_name != self.proposal_action:
            raise ValueError("proposal schema identity must match the proposal action")
        resource_obligations = [
            item
            for item in self.obligations
            if item.kind == "resource" and item.observed_resource_field is not None
        ]
        if len(resource_obligations) != 1:
            raise ValueError("ActionSpec requires exactly one resource obligation")
        resource_obligation = resource_obligations[0]
        if len(resource_obligation.capabilities) != 1:
            raise ValueError("resource obligation requires exactly one read capability")
        if not any(
            item.source == "observation"
            and item.obligation_id == resource_obligation.obligation_id
            and item.target_field == resource_obligation.observed_resource_field
            and item.source_path == resource_obligation.observed_resource_field
            for item in self.proposal_fields
        ):
            raise ValueError("resource obligation must bind the proposal resource field")
        return self

    @property
    def resource_obligation(self) -> EvidenceObligationSpec:
        """Return the sole current-business-resource obligation."""

        return next(
            item
            for item in self.obligations
            if item.kind == "resource" and item.observed_resource_field is not None
        )

    @property
    def resource_field(self) -> str:
        """Return the persisted proposal field that identifies the resource."""

        field = self.resource_obligation.observed_resource_field
        if field is None:  # pragma: no cover - guarded by model validation
            raise ValueError("resource obligation is missing its observed field")
        return field

    @property
    def primary_read_capability(self) -> str:
        """Return the sole read capability that refreshes the current resource."""

        return self.resource_obligation.capabilities[0]


_COMMON_ERROR_CODES = ActionErrorCodes(
    missing="action_evidence_missing",
    stale="action_evidence_stale",
    conflict="action_evidence_conflict",
    terminal="action_evidence_invalid",
)


ACTION_SPECS: Final[Mapping[ActionType, ActionSpec]] = MappingProxyType(
    {
        "refund": ActionSpec(
            action_type="refund",
            issue_type="billing_refund",
            admission_required_fields=("billing_record_id",),
            obligations=(
                EvidenceObligationSpec(
                    obligation_id="billing_record_current",
                    kind="resource",
                    capabilities=("query_billing_record",),
                    required_data_fields=(
                        "billing_record_id",
                        "amount",
                        "currency",
                        "status",
                        "charged_at",
                        "service_period_start",
                        "service_period_end",
                        "duplicate_of",
                        "version",
                        "original_billing_record_id",
                        "original_amount",
                        "original_currency",
                        "original_status",
                        "original_charged_at",
                        "original_service_period_start",
                        "original_service_period_end",
                        "original_version",
                        "duplicate_pair_eligible",
                        "refund_pair_hash",
                    ),
                    required_truthy_data_fields=(
                        "duplicate_of",
                        "original_billing_record_id",
                        "duplicate_pair_eligible",
                        "refund_pair_hash",
                    ),
                    resource_ref_argument="billing_record_id",
                    observed_resource_field="billing_record_id",
                    allowed_resource_statuses=("charged",),
                ),
                EvidenceObligationSpec(
                    obligation_id="refund_policy_current",
                    kind="knowledge",
                    capabilities=("search_knowledge",),
                    required_data_fields=("evidence", "index_version"),
                    require_freshness=False,
                    policy_family="billing-refunds",
                    topic="duplicate-charge-refund",
                    allowed_document_keys=("billing-refunds-v3",),
                    allowed_document_types=("official_policy",),
                    allowed_section_terms=("退款", "重复扣费", "审批"),
                    minimum_version="3.1",
                ),
            ),
            proposal_action="refund_proposal",
            proposal_schema_name="refund_proposal",
            proposal_schema=RefundProposalArguments,
            policy_capability="propose_refund",
            runtime_effect_capability="execute_refund",
            proposal_fields=(
                ProposalFieldBinding(
                    target_field="billing_record_id",
                    source="observation",
                    source_path="billing_record_id",
                    obligation_id="billing_record_current",
                ),
                ProposalFieldBinding(
                    target_field="refund_reason",
                    source="request_reason",
                    source_path="request_reason",
                ),
            ),
            terminal_outcomes=(
                TerminalOutcomeRule(
                    outcome_code="refund_resource_not_available",
                    terminal_class="resource_not_available",
                    obligation_id="billing_record_current",
                    predicate="observation_status_in",
                    match_values=("not_found",),
                    customer_message_key="refund_resource_not_available",
                    recommended_next_step="verify_billing_reference",
                    require_business_source=False,
                ),
                TerminalOutcomeRule(
                    outcome_code="refund_status_not_actionable",
                    terminal_class="action_ineligible",
                    obligation_id="billing_record_current",
                    predicate="resource_status_not_allowed",
                    observation_field="status",
                    public_observation_fields=(
                        "billing_record_id",
                        "status",
                    ),
                    customer_message_key="refund_status_not_actionable",
                    recommended_next_step="review_existing_refund",
                ),
                TerminalOutcomeRule(
                    outcome_code="refund_duplicate_relation_unconfirmed",
                    terminal_class="action_ineligible",
                    obligation_id="billing_record_current",
                    predicate="observation_field_falsy",
                    observation_field="duplicate_pair_eligible",
                    public_observation_fields=(
                        "billing_record_id",
                        "status",
                        "duplicate_of",
                        "duplicate_pair_eligible",
                    ),
                    customer_message_key="refund_duplicate_relation_unconfirmed",
                    recommended_next_step="verify_duplicate_billing_reference",
                ),
            ),
            clarification_fields=("billing_record_id",),
            error_codes=_COMMON_ERROR_CODES,
        ),
        "api_key_revocation": ActionSpec(
            action_type="api_key_revocation",
            issue_type="credential_security",
            admission_required_fields=("api_key_ref",),
            obligations=(
                EvidenceObligationSpec(
                    obligation_id="api_key_metadata_current",
                    kind="resource",
                    capabilities=("query_api_key_metadata",),
                    required_data_fields=(
                        "api_key_id",
                        "fingerprint",
                        "status",
                        "version",
                        "last_used_summary",
                    ),
                    resource_ref_argument="api_key_ref",
                    observed_resource_field="api_key_id",
                    allowed_resource_statuses=("active",),
                ),
                EvidenceObligationSpec(
                    obligation_id="api_key_revocation_policy_current",
                    kind="knowledge",
                    capabilities=("search_knowledge",),
                    required_data_fields=("evidence", "index_version"),
                    require_freshness=False,
                    policy_family="api-key-security",
                    topic="api-key-revocation",
                    allowed_document_keys=(
                        "api-key-incident-v1",
                        "authentication-security-v3",
                    ),
                    allowed_document_types=("security_policy",),
                    allowed_section_terms=("撤销", "密钥", "Secret", "轮换"),
                    minimum_version="1.0",
                ),
            ),
            proposal_action="api_key_revocation_proposal",
            proposal_schema_name="api_key_revocation_proposal",
            proposal_schema=ApiKeyRevocationProposalArguments,
            policy_capability="propose_api_key_revocation",
            runtime_effect_capability="execute_api_key_revocation",
            proposal_fields=(
                ProposalFieldBinding(
                    target_field="api_key_id",
                    source="observation",
                    source_path="api_key_id",
                    obligation_id="api_key_metadata_current",
                ),
                ProposalFieldBinding(
                    target_field="reason",
                    source="request_reason",
                    source_path="request_reason",
                ),
            ),
            terminal_outcomes=(
                TerminalOutcomeRule(
                    outcome_code="api_key_resource_not_available",
                    terminal_class="resource_not_available",
                    obligation_id="api_key_metadata_current",
                    predicate="observation_status_in",
                    match_values=("not_found",),
                    customer_message_key="api_key_resource_not_available",
                    recommended_next_step="verify_key_reference",
                    require_business_source=False,
                ),
                TerminalOutcomeRule(
                    outcome_code="api_key_status_not_actionable",
                    terminal_class="action_ineligible",
                    obligation_id="api_key_metadata_current",
                    predicate="resource_status_not_allowed",
                    observation_field="status",
                    public_observation_fields=("api_key_id", "status"),
                    customer_message_key="api_key_status_not_actionable",
                    recommended_next_step="review_key_status",
                ),
            ),
            clarification_fields=("api_key_ref",),
            error_codes=_COMMON_ERROR_CODES,
        ),
        "entitlement_change": ActionSpec(
            action_type="entitlement_change",
            issue_type="entitlement_change",
            admission_required_fields=("target",),
            obligations=(
                EvidenceObligationSpec(
                    obligation_id="subscription_current",
                    kind="resource",
                    capabilities=("query_subscription",),
                    required_data_fields=(
                        "subscription_id",
                        "plan",
                        "status",
                        "rpm_limit",
                        "concurrency_limit",
                        "catalog_eligibility",
                        "version",
                    ),
                    observed_resource_field="subscription_id",
                    allowed_resource_statuses=("active",),
                ),
                EvidenceObligationSpec(
                    obligation_id="entitlement_policy_current",
                    kind="knowledge",
                    capabilities=("search_knowledge",),
                    required_data_fields=("evidence", "index_version"),
                    require_freshness=False,
                    policy_family="entitlement-changes",
                    topic="quota-and-plan-change",
                    allowed_document_keys=("entitlement-changes-v1",),
                    allowed_document_types=("official_policy",),
                    allowed_section_terms=("配额", "套餐", "目标", "审批"),
                    minimum_version="1.0",
                ),
            ),
            proposal_action="entitlement_change_proposal",
            proposal_schema_name="entitlement_change_proposal",
            proposal_schema=EntitlementChangeProposalArguments,
            policy_capability="propose_entitlement_change",
            runtime_effect_capability="execute_entitlement_change",
            proposal_fields=(
                ProposalFieldBinding(
                    target_field="subscription_id",
                    source="observation",
                    source_path="subscription_id",
                    obligation_id="subscription_current",
                ),
                ProposalFieldBinding(
                    target_field="change_type",
                    source="admission",
                    source_path="change_type",
                ),
                ProposalFieldBinding(
                    target_field="target",
                    source="admission",
                    source_path="target",
                ),
                ProposalFieldBinding(
                    target_field="reason",
                    source="request_reason",
                    source_path="request_reason",
                ),
            ),
            terminal_outcomes=(
                TerminalOutcomeRule(
                    outcome_code="subscription_resource_not_available",
                    terminal_class="resource_not_available",
                    obligation_id="subscription_current",
                    predicate="observation_status_in",
                    match_values=("not_found",),
                    customer_message_key="subscription_resource_not_available",
                    recommended_next_step="verify_subscription",
                    require_business_source=False,
                ),
                TerminalOutcomeRule(
                    outcome_code="subscription_status_not_actionable",
                    terminal_class="action_ineligible",
                    obligation_id="subscription_current",
                    predicate="resource_status_not_allowed",
                    observation_field="status",
                    public_observation_fields=(
                        "subscription_id",
                        "status",
                    ),
                    customer_message_key="subscription_status_not_actionable",
                    recommended_next_step="restore_or_review_subscription",
                ),
                TerminalOutcomeRule(
                    outcome_code="entitlement_target_unsupported",
                    terminal_class="action_ineligible",
                    obligation_id="subscription_current",
                    predicate="admission_not_in_observation",
                    observation_field="catalog_eligibility",
                    admission_path="change_type",
                    public_observation_fields=(
                        "subscription_id",
                        "catalog_eligibility",
                    ),
                    public_admission_paths=("change_type",),
                    customer_message_key="entitlement_target_unsupported",
                    recommended_next_step="choose_supported_change_type",
                ),
                TerminalOutcomeRule(
                    outcome_code="entitlement_plan_target_noop",
                    terminal_class="action_ineligible",
                    obligation_id="subscription_current",
                    predicate="admission_equals_observation",
                    observation_field="plan",
                    admission_path="target.plan",
                    public_observation_fields=("subscription_id", "plan"),
                    public_admission_paths=("change_type", "target.plan"),
                    customer_message_key="entitlement_target_noop",
                    recommended_next_step="choose_different_target",
                ),
                TerminalOutcomeRule(
                    outcome_code="entitlement_rpm_target_noop",
                    terminal_class="action_ineligible",
                    obligation_id="subscription_current",
                    predicate="admission_equals_observation",
                    observation_field="rpm_limit",
                    admission_path="target.rpm_limit",
                    public_observation_fields=("subscription_id", "rpm_limit"),
                    public_admission_paths=("change_type", "target.rpm_limit"),
                    customer_message_key="entitlement_target_noop",
                    recommended_next_step="choose_different_target",
                ),
                TerminalOutcomeRule(
                    outcome_code="entitlement_concurrency_target_noop",
                    terminal_class="action_ineligible",
                    obligation_id="subscription_current",
                    predicate="admission_equals_observation",
                    observation_field="concurrency_limit",
                    admission_path="target.concurrency_limit",
                    public_observation_fields=(
                        "subscription_id",
                        "concurrency_limit",
                    ),
                    public_admission_paths=(
                        "change_type",
                        "target.concurrency_limit",
                    ),
                    customer_message_key="entitlement_target_noop",
                    recommended_next_step="choose_different_target",
                ),
            ),
            clarification_fields=("target",),
            error_codes=_COMMON_ERROR_CODES,
        ),
    }
)

_ACTION_SPECS_BY_ACTION_TYPE: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {str(item.action_type): item for item in ACTION_SPECS.values()}
)
_ACTION_SPECS_BY_PROPOSAL: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {str(item.proposal_action): item for item in ACTION_SPECS.values()}
)
_ACTION_SPECS_BY_POLICY_CAPABILITY: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {str(item.policy_capability): item for item in ACTION_SPECS.values()}
)
_ACTION_SPECS_BY_RUNTIME_EFFECT_CAPABILITY: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {str(item.runtime_effect_capability): item for item in ACTION_SPECS.values()}
)

if len(_ACTION_SPECS_BY_ACTION_TYPE) != len(ACTION_SPECS):
    raise ValueError("ActionSpec action types must be unique")
if len(_ACTION_SPECS_BY_PROPOSAL) != len(ACTION_SPECS):
    raise ValueError("ActionSpec proposal actions must be unique")
if len(_ACTION_SPECS_BY_POLICY_CAPABILITY) != len(ACTION_SPECS):
    raise ValueError("ActionSpec policy capabilities must be unique")
if len(_ACTION_SPECS_BY_RUNTIME_EFFECT_CAPABILITY) != len(ACTION_SPECS):
    raise ValueError("ActionSpec runtime effect capabilities must be unique")


def get_action_spec(action_type: ActionType) -> ActionSpec:
    return ACTION_SPECS[action_type]


def get_action_spec_or_none(action_type: str) -> ActionSpec | None:
    return _ACTION_SPECS_BY_ACTION_TYPE.get(action_type)


def get_action_spec_by_proposal(proposal_action: str) -> ActionSpec | None:
    return _ACTION_SPECS_BY_PROPOSAL.get(proposal_action)


def get_action_spec_by_policy_capability(
    policy_capability: str,
) -> ActionSpec | None:
    return _ACTION_SPECS_BY_POLICY_CAPABILITY.get(policy_capability)


def get_action_spec_by_runtime_effect_capability(
    runtime_effect_capability: str,
) -> ActionSpec | None:
    return _ACTION_SPECS_BY_RUNTIME_EFFECT_CAPABILITY.get(runtime_effect_capability)


class ActionCandidate(BaseModel):
    """Policy-authorized proposal input; it grants no Runtime effect authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-candidate.v1"] = "action-candidate.v1"
    action_type: ActionType
    proposal_action: ProposalAction
    policy_capability: PolicyCapability
    runtime_effect_capability: RuntimeEffectCapability
    resource_type: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=128)
    resource_version: int = Field(ge=0)
    trusted_arguments: dict[str, Any]
    observation_binding: tuple[dict[str, Any], ...] = Field(min_length=1)
    citation_binding_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_action_contract(self) -> ActionCandidate:
        spec = get_action_spec_by_proposal(self.proposal_action)
        if (
            spec is None
            or spec.action_type != self.action_type
            or spec.policy_capability != self.policy_capability
            or spec.runtime_effect_capability != self.runtime_effect_capability
            or spec.resource_field != self.resource_type
        ):
            raise ValueError("action candidate does not match the ActionSpec")
        spec.proposal_schema.model_validate(self.trusted_arguments)
        return self


def build_action_candidate(
    *,
    proposal_action: str,
    action_type: str,
    resource_type: str | None,
    resource_id: str | None,
    resource_version: int | None,
    trusted_arguments: Mapping[str, Any],
    observation_binding: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    citation_binding_ids: tuple[str, ...] | list[str],
) -> ActionCandidate:
    """Bind Policy output to the sole ActionSpec before proposal transport."""

    spec = get_action_spec_by_proposal(proposal_action)
    if spec is None or spec.action_type != action_type:
        raise ValueError("action candidate identity is unsupported")
    return ActionCandidate(
        action_type=spec.action_type,
        proposal_action=spec.proposal_action,
        policy_capability=spec.policy_capability,
        runtime_effect_capability=spec.runtime_effect_capability,
        resource_type=str(resource_type or spec.resource_field),
        resource_id=str(resource_id or ""),
        resource_version=-1 if resource_version is None else resource_version,
        trusted_arguments=dict(trusted_arguments),
        observation_binding=tuple(dict(item) for item in observation_binding),
        citation_binding_ids=tuple(citation_binding_ids),
    )


class ApprovalDecision(BaseModel):
    """Normalized human resume command bound to one durable Approval identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["approval-decision.v1"] = "approval-decision.v1"
    approval_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    action: ApprovalDecisionAction
    command: dict[str, Any]

    @model_validator(mode="after")
    def validate_command_identity(self) -> ApprovalDecision:
        if str(self.command.get("action", "")) != self.action:
            raise ValueError("approval decision action changed during normalization")
        command_approval_id = str(self.command.get("approval_id", ""))
        if command_approval_id and command_approval_id != self.approval_id:
            raise ValueError("approval decision identity changed during normalization")
        command_idempotency_key = str(self.command.get("idempotency_key", ""))
        if command_idempotency_key and command_idempotency_key != self.idempotency_key:
            raise ValueError("approval idempotency identity changed during normalization")
        return self


def build_approval_decision(
    *,
    command: Mapping[str, Any],
    proposal_result: Mapping[str, Any],
) -> ApprovalDecision:
    command_approval_id = str(command.get("approval_id") or "")
    proposal_approval_id = str(proposal_result.get("approval_id") or "")
    if command_approval_id and proposal_approval_id and command_approval_id != proposal_approval_id:
        raise ValueError("approval decision identity changed during normalization")
    # Proposal creation and the later human command are separate durable
    # operations. Their idempotency keys intentionally live in different
    # namespaces; only the Approval identity must remain the same.
    approval_id = str(command.get("approval_id") or proposal_result.get("approval_id") or "")
    idempotency_key = str(
        command.get("idempotency_key")
        or proposal_result.get("idempotency_key")
        or (f"approval:{approval_id}" if approval_id else "")
    )
    return ApprovalDecision(
        approval_id=approval_id,
        idempotency_key=idempotency_key,
        action=str(command.get("action", "")),
        command=dict(command),
    )


class RuntimeEffectResult(BaseModel):
    """Typed projection of the Runtime-owned effect result returned to the Graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["runtime-effect-result.v1"] = "runtime-effect-result.v1"
    approval_id: str = Field(min_length=1, max_length=128)
    action_type: ActionType
    resource_id: str = Field(min_length=1, max_length=128)
    status: RuntimeEffectStatus
    business_action_id: str | None = None
    reused: bool | None = None
    reason: str | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_effect_identity(self) -> RuntimeEffectResult:
        expected = {
            "approval_id": self.approval_id,
            "action_type": self.action_type,
            "resource_id": self.resource_id,
            "status": self.status,
        }
        if any(
            key in self.payload and str(self.payload[key]) != str(value)
            for key, value in expected.items()
        ):
            raise ValueError("runtime effect identity changed during projection")
        return self


def build_runtime_effect_result(
    *,
    candidate: ActionCandidate,
    decision: ApprovalDecision,
    payload: Mapping[str, Any],
) -> RuntimeEffectResult:
    return RuntimeEffectResult(
        approval_id=decision.approval_id,
        action_type=candidate.action_type,
        resource_id=candidate.resource_id,
        status=str(payload.get("status", "")),
        business_action_id=(
            str(payload["business_action_id"]) if payload.get("business_action_id") else None
        ),
        reused=bool(payload["reused"]) if "reused" in payload else None,
        reason=str(payload["reason"]) if payload.get("reason") else None,
        payload=dict(payload),
    )


class ActionDecisionHandler(Protocol):
    async def handle(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        decision: dict[str, Any],
        trace_id: str,
        publication_state: dict[str, Any],
    ) -> dict[str, Any]: ...


class ActionCapabilitySettlement(Protocol):
    status: str
    payload: dict[str, Any]


class ActionPipelineHost(Protocol[LeaseT, CapabilityT]):
    @property
    def gateway(self) -> ToolGateway: ...

    async def reserve_action_capability(
        self,
        state: Mapping[str, Any],
        capability_name: str,
        *,
        model_arguments: dict[str, Any],
        observation_binding: list[dict[str, Any]],
    ) -> tuple[LeaseT, CapabilityT] | None: ...

    def action_tool_context(
        self,
        state: Mapping[str, Any],
        *,
        observation_binding: list[dict[str, Any]],
        capability: CapabilityT | None,
        lease: LeaseT | None,
    ) -> ToolCallContext: ...

    async def finish_action_capability(
        self,
        reservation: tuple[LeaseT, CapabilityT] | None,
        *,
        status: str,
        error_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ActionCapabilitySettlement | None: ...

    def action_capability_payload(self, result: Any) -> dict[str, Any]: ...

    def action_result_payload(self, result: Any) -> dict[str, Any]: ...


class ActionPipelineError(RuntimeError):
    """Stable fail-closed outcome from the shared proposal/effect pipeline."""

    def __init__(self, code: str, *, error_code: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.error_code = error_code


class ActionProposalResult(BaseModel):
    """Durable inert proposal produced by the deterministic Policy pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-proposal-result.v1"] = "action-proposal-result.v1"
    candidate: ActionCandidate
    proposal: dict[str, Any]


class ActionExecutionResult(BaseModel):
    """Human decision plus the Runtime-owned effect projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-execution-result.v1"] = "action-execution-result.v1"
    decision: ApprovalDecision
    effect: RuntimeEffectResult
    payload: dict[str, Any]


class ActionService:
    """Single Proposal → Approval → RuntimeEffect orchestration owner."""

    async def propose(
        self,
        *,
        host: ActionPipelineHost[LeaseT, CapabilityT],
        state: Mapping[str, Any],
        candidate: ActionCandidate,
        verify_durable: Callable[[dict[str, Any]], Awaitable[bool]],
    ) -> ActionProposalResult:
        action_name = candidate.policy_capability
        trusted_arguments = dict(candidate.trusted_arguments)
        observation_binding = [dict(item) for item in candidate.observation_binding]
        reserved: tuple[LeaseT, CapabilityT] | None = None
        try:
            reserved = await host.reserve_action_capability(
                state,
                action_name,
                model_arguments=trusted_arguments,
                observation_binding=observation_binding,
            )
            result = await host.gateway.call_action(
                ActionToolCall(name=cast(Any, action_name), arguments=trusted_arguments),
                host.action_tool_context(
                    state,
                    observation_binding=observation_binding,
                    capability=reserved[1] if reserved is not None else None,
                    lease=reserved[0] if reserved is not None else None,
                ),
            )
        except (TypeError, ValueError) as exc:
            if reserved is not None:
                await host.finish_action_capability(
                    reserved,
                    status="failed",
                    error_code=type(exc).__name__,
                )
            raise ActionPipelineError("tool_failed") from exc

        envelope = result if isinstance(result, ObservationEnvelope) else None
        capability_status = (
            "unknown"
            if envelope is not None and envelope.status in {"timeout", "unavailable"}
            else "succeeded"
            if envelope is None or envelope.status == "ok"
            else "failed"
        )
        capability_payload = host.action_capability_payload(result)
        capability_error = envelope.error_code if envelope is not None else None
        if capability_status == "succeeded" and not capability_payload.get("proposal_id"):
            capability_status = "failed"
            capability_error = "proposal_not_durable"
            capability_payload = {}
        settled = await host.finish_action_capability(
            reserved,
            status=capability_status,
            error_code=capability_error,
            payload=capability_payload or None,
        )
        proposal = (
            dict(settled.payload)
            if settled is not None and settled.status == "succeeded"
            else host.action_result_payload(result)
        )
        if envelope is not None and envelope.status != "ok":
            raise ActionPipelineError("tool_failed", error_code=envelope.error_code)
        if not proposal.get("proposal_id") or not await verify_durable(proposal):
            raise ActionPipelineError(
                "proposal_not_durable",
                error_code="proposal_not_durable",
            )
        return ActionProposalResult(candidate=candidate, proposal=proposal)

    async def execute(
        self,
        *,
        handler: ActionDecisionHandler,
        candidate: ActionCandidate,
        decision: ApprovalDecision,
        trace_id: str,
        publication_state: dict[str, Any],
    ) -> ActionExecutionResult:
        payload = await handler.handle(
            approval_id=decision.approval_id,
            idempotency_key=decision.idempotency_key,
            decision=dict(decision.command),
            trace_id=trace_id,
            publication_state=publication_state,
        )
        effect = build_runtime_effect_result(
            candidate=candidate,
            decision=decision,
            payload=payload,
        )
        return ActionExecutionResult(decision=decision, effect=effect, payload=payload)
