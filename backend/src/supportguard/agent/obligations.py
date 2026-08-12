from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from supportguard.actions.service import (
    ActionSpec,
    EvidenceObligationSpec,
    TerminalOutcomeRule,
)
from supportguard.contracts.action_preconditions import ActionAdmissionV2

ObligationStatus = Literal[
    "pending",
    "read_qualified",
    "satisfied",
    "stale",
    "conflicted",
    "failed",
    "terminal",
]
LedgerNextState = Literal[
    "collect_reads",
    "synthesize",
    "assemble_candidate",
    "clarify",
    "safe_stop",
    "explain_terminal",
]


class ContextCitationBinding(BaseModel):
    """Public, auditable citation membership produced by one Provider context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_binding_id: str
    provider_attempt_id: str
    evidence_id: str
    document_id: str
    chunk_id: str
    content_hash: str
    locator_hash: str


class ObligationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    invocation_id: str
    observation_id: str
    observation_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_id: str | None = None
    resource_version: str | None = None
    tenant_id: str
    customer_id: str
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    freshness_status: str
    fresh_until: datetime | None = None
    source_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    citation_binding_ids: tuple[str, ...] = ()


class TerminalBusinessOutcome(BaseModel):
    """Current-run, source-bound reason why an admitted action cannot proceed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["terminal-business-outcome.v1"] = "terminal-business-outcome.v1"
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    terminal_class: Literal["action_ineligible", "resource_not_available"]
    outcome_code: str
    obligation_id: str
    resource_ref: str | None = None
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    binding: ObligationBinding
    customer_message_key: str
    recommended_next_step: str
    proposal_allowed: Literal[False] = False
    approval_allowed: Literal[False] = False
    execution_allowed: Literal[False] = False
    outcome_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObligationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    obligation_id: str
    kind: Literal["resource", "knowledge", "usage"]
    status: ObligationStatus
    capabilities: tuple[str, ...]
    binding: ObligationBinding | None = None
    terminal_outcome: TerminalBusinessOutcome | None = None
    reason_code: str


class ActionObligationLedger(BaseModel):
    """Derived current-run state. It is never a source of business authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-obligation-ledger.v1", "action-obligation-ledger.v2"] = (
        "action-obligation-ledger.v2"
    )
    action_spec_version: Literal["action-spec.v1", "action-spec.v2"] = "action-spec.v2"
    action_type: Literal["refund", "api_key_revocation", "entitlement_change"]
    run_id: str
    tenant_id: str
    customer_id: str
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    obligations: tuple[ObligationEntry, ...]
    unsatisfied_capabilities: tuple[str, ...]
    next_state: LedgerNextState
    reason_code: str
    terminal_outcome: TerminalBusinessOutcome | None = None

    @property
    def all_reads_qualified(self) -> bool:
        return all(item.status in {"satisfied", "read_qualified"} for item in self.obligations)

    @property
    def all_obligations_satisfied(self) -> bool:
        return all(item.status == "satisfied" for item in self.obligations)


def qualified_knowledge_evidence_ids(
    ledger: ActionObligationLedger,
) -> tuple[str, ...]:
    """Return only evidence already qualified by the frozen action contract."""

    return tuple(
        sorted(
            {
                evidence_id
                for item in ledger.obligations
                if item.kind == "knowledge"
                and item.status in {"read_qualified", "satisfied"}
                and item.binding is not None
                for evidence_id in item.binding.evidence_ids
            }
        )
    )


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _nested_value(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _version_key(value: object) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value))
    return tuple(int(part) for part in parts) or (0,)


def _observation_scope(observation: dict[str, Any]) -> tuple[str, str, str]:
    scope = observation.get("trusted_scope")
    if not isinstance(scope, dict):
        scope = observation
    tenant_id = str(scope.get("tenant_id", ""))
    customer_id = str(scope.get("customer_id", ""))
    scope_hash = str(scope.get("scope_hash", ""))
    if not scope_hash and tenant_id and customer_id:
        scope_hash = _canonical_hash({"customer_id": customer_id, "tenant_id": tenant_id})
    return tenant_id, customer_id, scope_hash


def _source_ids(observation: dict[str, Any]) -> tuple[str, ...]:
    refs = observation.get("source_refs", [])
    if not isinstance(refs, list):
        return ()
    return tuple(
        str(item["source_id"]) for item in refs if isinstance(item, dict) and item.get("source_id")
    )


def _base_binding(
    observation: dict[str, Any],
    *,
    tenant_id: str,
    customer_id: str,
    scope_hash: str,
    resource_id: str | None = None,
    evidence_ids: tuple[str, ...] = (),
    citation_binding_ids: tuple[str, ...] = (),
) -> ObligationBinding:
    return ObligationBinding(
        tool_name=str(observation.get("tool_name", "")),
        invocation_id=str(
            observation.get("invocation_id")
            or observation.get("logical_invocation_id")
            or observation.get("tool_call_id", "")
        ),
        observation_id=str(
            observation.get("observation_id")
            or observation.get("id")
            or observation.get("tool_call_id", "")
        ),
        observation_content_hash=str(
            observation.get("observation_content_hash")
            or observation.get("content_hash")
            or _canonical_hash(observation)
        ),
        resource_id=resource_id,
        resource_version=(
            str(
                observation.get("resource_version")
                or observation.get("data", {}).get("version")
                or observation.get("data", {}).get("resource_version")
            )
            if (
                observation.get("resource_version") is not None
                or observation.get("data", {}).get("version") is not None
                or observation.get("data", {}).get("resource_version") is not None
            )
            else None
        ),
        tenant_id=tenant_id,
        customer_id=customer_id,
        scope_hash=scope_hash,
        freshness_status=str(observation.get("freshness_status", "unknown")),
        fresh_until=_parse_time(observation.get("fresh_until")),
        source_ids=_source_ids(observation),
        evidence_ids=evidence_ids,
        citation_binding_ids=citation_binding_ids,
    )


def _freshness_reason(
    observation: dict[str, Any],
    *,
    now: datetime,
) -> str | None:
    if observation.get("freshness_status") != "fresh":
        return "observation_freshness_not_fresh"
    fresh_until = _parse_time(observation.get("fresh_until"))
    if fresh_until is None or fresh_until < now:
        return "observation_expired"
    return None


def _relevant_observations(
    observations: Sequence[dict[str, Any]],
    *,
    run_id: str,
    capability: str,
) -> list[dict[str, Any]]:
    current = [
        item
        for item in observations
        if item.get("run_id") == run_id and item.get("tool_name") == capability
    ]
    return sorted(
        current,
        key=lambda item: (
            _parse_time(item.get("observed_at")) or datetime.min.replace(tzinfo=UTC),
            int(item.get("attempt_index", 0)),
            str(item.get("observation_id") or item.get("id") or ""),
        ),
        reverse=True,
    )


def _failure_entry(
    obligation: EvidenceObligationSpec,
    observation: dict[str, Any],
) -> ObligationEntry:
    status = str(observation.get("status", "invalid_result"))
    if status == "conflict":
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="conflicted",
            capabilities=obligation.capabilities,
            reason_code=str(observation.get("error_code") or "observation_conflicted"),
        )
    if status in {
        "timeout",
        "unavailable",
        "invalid_result",
        "invalid_input",
        "forbidden_tool",
    }:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="failed",
            capabilities=obligation.capabilities,
            reason_code=str(observation.get("error_code") or "read_capability_failed"),
        )
    return ObligationEntry(
        obligation_id=obligation.obligation_id,
        kind=obligation.kind,
        status="pending",
        capabilities=obligation.capabilities,
        reason_code=str(observation.get("error_code") or "read_result_not_qualifying"),
    )


def _hard_terminal_entry(
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    observations: Sequence[dict[str, Any]],
) -> ObligationEntry | None:
    """Return a terminal security/transport failure before considering older success."""

    scope_error_codes = {
        "billing_scope_violation",
        "ticket_scope_violation",
        "cross_tenant_argument",
        "cross_tenant_observation",
        "observation_scope_mismatch",
        "knowledge_scope_mismatch",
    }
    forbidden_error_codes = {
        "forbidden_surface",
        "forbidden_tool",
        "forbidden_tool_surface",
    }
    for observation in observations:
        tenant_id, customer_id, scope_hash = _observation_scope(observation)
        has_scope = bool(tenant_id or customer_id or scope_hash)
        if has_scope and (
            tenant_id != admission.tenant_id
            or customer_id != admission.customer_id
            or scope_hash != admission.scope_hash
        ):
            return ObligationEntry(
                obligation_id=obligation.obligation_id,
                kind=obligation.kind,
                status="failed",
                capabilities=obligation.capabilities,
                reason_code="observation_scope_mismatch",
            )
        status = str(observation.get("status", "invalid_result"))
        error_code = str(observation.get("error_code") or "")
        if (
            status in {"timeout", "unavailable", "forbidden_tool"}
            or error_code in scope_error_codes
            or error_code in forbidden_error_codes
            or "retry_exhausted" in error_code
        ):
            failed = _failure_entry(obligation, observation)
            if failed.status == "failed":
                return failed
            return ObligationEntry(
                obligation_id=obligation.obligation_id,
                kind=obligation.kind,
                status="failed",
                capabilities=obligation.capabilities,
                reason_code=error_code or "hard_terminal_read_failure",
            )
    return None


def _terminal_rule_matches(
    rule: TerminalOutcomeRule,
    *,
    observation: dict[str, Any],
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
) -> bool:
    status = str(observation.get("status", "invalid_result"))
    data = observation.get("data")
    if not isinstance(data, dict):
        data = {}
    if rule.predicate == "observation_status_in":
        return status in rule.match_values
    if status != "ok":
        return False
    if rule.predicate == "resource_status_not_allowed":
        observed_status = str(data.get(rule.observation_field or "status", ""))
        return bool(
            observed_status
            and obligation.allowed_resource_statuses
            and observed_status not in obligation.allowed_resource_statuses
        )
    if rule.predicate == "observation_field_falsy":
        return bool(rule.observation_field in data and not data.get(rule.observation_field or ""))
    if rule.admission_path is None or rule.observation_field is None:
        return False
    admission_exists, admission_value = _nested_value(
        admission.extracted_arguments,
        rule.admission_path,
    )
    if not admission_exists or rule.observation_field not in data:
        return False
    observed_value = data[rule.observation_field]
    if rule.predicate == "admission_not_in_observation":
        return isinstance(observed_value, (list, tuple, set)) and admission_value not in {
            str(item) for item in observed_value
        }
    if rule.predicate == "admission_equals_observation":
        return bool(admission_value == observed_value)
    return False


def _terminal_outcome_entry(
    *,
    action_spec: ActionSpec,
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    observation: dict[str, Any],
    now: datetime,
) -> ObligationEntry | None:
    """Derive a safe terminal fact before nullable proposal fields look missing."""

    tenant_id, customer_id, scope_hash = _observation_scope(observation)
    if (
        tenant_id != admission.tenant_id
        or customer_id != admission.customer_id
        or scope_hash != admission.scope_hash
    ):
        return None
    status = str(observation.get("status", "invalid_result"))
    data = observation.get("data")
    if not isinstance(data, dict):
        data = {}
    if status != "ok" and obligation.resource_ref_argument is not None:
        request_binding = observation.get("request_binding")
        admitted_ref = str(admission.extracted_arguments.get(obligation.resource_ref_argument, ""))
        if (
            not isinstance(request_binding, dict)
            or not admitted_ref
            or str(request_binding.get("resource_ref", "")).casefold() != admitted_ref.casefold()
        ):
            return None
    if status == "ok":
        if obligation.require_freshness and _freshness_reason(observation, now=now) is not None:
            return None
        if obligation.observed_resource_field is not None and not data.get(
            obligation.observed_resource_field
        ):
            return None
        if obligation.resource_ref_argument is not None:
            admitted_ref = str(
                admission.extracted_arguments.get(obligation.resource_ref_argument, "")
            )
            equivalent_refs = {
                str(data.get(obligation.observed_resource_field or "", "")),
                str(data.get("fingerprint", "")),
            }
            if not admitted_ref or admitted_ref.casefold() not in {
                item.casefold() for item in equivalent_refs if item
            }:
                return None
    for rule in action_spec.terminal_outcomes:
        if rule.obligation_id != obligation.obligation_id or not _terminal_rule_matches(
            rule,
            observation=observation,
            obligation=obligation,
            admission=admission,
        ):
            continue
        source_ids = _source_ids(observation)
        if rule.require_business_source and not source_ids:
            return None
        observed_facts = {
            field: data[field] for field in rule.public_observation_fields if field in data
        }
        for path in rule.public_admission_paths:
            exists, value = _nested_value(admission.extracted_arguments, path)
            if exists:
                observed_facts[f"requested_{path.replace('.', '_')}"] = value
        resource_ref = None
        if obligation.resource_ref_argument is not None:
            resource_ref = (
                str(admission.extracted_arguments.get(obligation.resource_ref_argument, "")) or None
            )
        if resource_ref is None and obligation.observed_resource_field is not None:
            resource_ref = str(data.get(obligation.observed_resource_field) or "") or None
        binding = _base_binding(
            observation,
            tenant_id=tenant_id,
            customer_id=customer_id,
            scope_hash=scope_hash,
            resource_id=resource_ref,
        )
        outcome_payload = {
            "action_type": action_spec.action_type,
            "binding_hash": binding.observation_content_hash,
            "observed_facts": observed_facts,
            "obligation_id": obligation.obligation_id,
            "outcome_code": rule.outcome_code,
            "resource_ref": resource_ref,
            "scope_hash": admission.scope_hash,
        }
        outcome = TerminalBusinessOutcome(
            action_type=action_spec.action_type,
            terminal_class=rule.terminal_class,
            outcome_code=rule.outcome_code,
            obligation_id=obligation.obligation_id,
            resource_ref=resource_ref,
            observed_facts=observed_facts,
            binding=binding,
            customer_message_key=rule.customer_message_key,
            recommended_next_step=rule.recommended_next_step,
            outcome_hash=_canonical_hash(outcome_payload),
        )
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="terminal",
            capabilities=obligation.capabilities,
            binding=binding,
            terminal_outcome=outcome,
            reason_code=rule.outcome_code,
        )
    return None


def _resource_entry(
    action_spec: ActionSpec,
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    observations: Sequence[dict[str, Any]],
    *,
    run_id: str,
    now: datetime,
) -> ObligationEntry:
    candidates = [
        item
        for capability in obligation.capabilities
        for item in _relevant_observations(observations, run_id=run_id, capability=capability)
    ]
    if not candidates:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="required_read_missing",
        )
    hard_terminal = _hard_terminal_entry(obligation, admission, candidates)
    if hard_terminal is not None:
        return hard_terminal
    for candidate in candidates:
        terminal = _terminal_outcome_entry(
            action_spec=action_spec,
            obligation=obligation,
            admission=admission,
            observation=candidate,
            now=now,
        )
        if terminal is not None:
            return terminal
    observation = next(
        (
            item
            for item in candidates
            if _resource_observation_qualifies(
                item,
                obligation,
                admission,
                now=now,
            )
        ),
        candidates[0],
    )
    if observation.get("status") != "ok":
        return _failure_entry(obligation, observation)
    tenant_id, customer_id, scope_hash = _observation_scope(observation)
    if (
        tenant_id != admission.tenant_id
        or customer_id != admission.customer_id
        or scope_hash != admission.scope_hash
    ):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="failed",
            capabilities=obligation.capabilities,
            reason_code="observation_scope_mismatch",
        )
    if obligation.require_freshness:
        freshness_reason = _freshness_reason(observation, now=now)
        if freshness_reason is not None:
            return ObligationEntry(
                obligation_id=obligation.obligation_id,
                kind=obligation.kind,
                status="stale",
                capabilities=obligation.capabilities,
                reason_code=freshness_reason,
            )
    data = observation.get("data")
    if not isinstance(data, dict):
        data = {}
    if any(data.get(field) is None for field in obligation.required_data_fields):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="required_resource_fields_missing",
        )
    if any(not data.get(field) for field in obligation.required_truthy_data_fields):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="required_resource_value_missing",
        )
    if (
        obligation.allowed_resource_statuses
        and str(data.get("status")) not in obligation.allowed_resource_statuses
    ):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="conflicted",
            capabilities=obligation.capabilities,
            reason_code="resource_status_conflict",
        )
    resource_id: str | None = None
    if obligation.observed_resource_field is not None:
        resource_id = str(data.get(obligation.observed_resource_field) or "")
        if not resource_id:
            return ObligationEntry(
                obligation_id=obligation.obligation_id,
                kind=obligation.kind,
                status="pending",
                capabilities=obligation.capabilities,
                reason_code="resource_identity_missing",
            )
    if obligation.resource_ref_argument is not None:
        admitted_ref = str(admission.extracted_arguments.get(obligation.resource_ref_argument, ""))
        equivalent_refs = {
            str(data.get(obligation.observed_resource_field or "", "")),
            str(data.get("fingerprint", "")),
        }
        if not admitted_ref or admitted_ref.casefold() not in {
            item.casefold() for item in equivalent_refs if item
        }:
            return ObligationEntry(
                obligation_id=obligation.obligation_id,
                kind=obligation.kind,
                status="pending",
                capabilities=obligation.capabilities,
                reason_code="resource_identity_mismatch",
            )
    sources = _source_ids(observation)
    if not sources:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind=obligation.kind,
            status="failed",
            capabilities=obligation.capabilities,
            reason_code="resource_source_binding_missing",
        )
    return ObligationEntry(
        obligation_id=obligation.obligation_id,
        kind=obligation.kind,
        status="satisfied",
        capabilities=obligation.capabilities,
        binding=_base_binding(
            observation,
            tenant_id=tenant_id,
            customer_id=customer_id,
            scope_hash=scope_hash,
            resource_id=resource_id,
        ),
        reason_code="resource_obligation_satisfied",
    )


def _resource_observation_qualifies(
    observation: dict[str, Any],
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    *,
    now: datetime,
) -> bool:
    if observation.get("status") != "ok":
        return False
    tenant_id, customer_id, scope_hash = _observation_scope(observation)
    if (
        tenant_id != admission.tenant_id
        or customer_id != admission.customer_id
        or scope_hash != admission.scope_hash
    ):
        return False
    if (
        obligation.require_freshness
        and _freshness_reason(
            observation,
            now=now,
        )
        is not None
    ):
        return False
    data = observation.get("data")
    if not isinstance(data, dict) or any(
        data.get(field) is None for field in obligation.required_data_fields
    ):
        return False
    if any(not data.get(field) for field in obligation.required_truthy_data_fields):
        return False
    if (
        obligation.allowed_resource_statuses
        and str(data.get("status")) not in obligation.allowed_resource_statuses
    ):
        return False
    if obligation.observed_resource_field is not None and not data.get(
        obligation.observed_resource_field
    ):
        return False
    if obligation.resource_ref_argument is not None:
        admitted_ref = str(admission.extracted_arguments.get(obligation.resource_ref_argument, ""))
        equivalent_refs = {
            str(data.get(obligation.observed_resource_field or "", "")),
            str(data.get("fingerprint", "")),
        }
        if not admitted_ref or admitted_ref.casefold() not in {
            item.casefold() for item in equivalent_refs if item
        }:
            return False
    return bool(_source_ids(observation))


def _knowledge_evidence_qualifies(
    evidence: dict[str, Any],
    obligation: EvidenceObligationSpec,
    *,
    observation_index_version: str,
    now: datetime,
) -> bool:
    document_id = str(evidence.get("document_id", ""))
    if document_id not in obligation.allowed_document_keys:
        return False
    if str(evidence.get("document_type", "")) not in obligation.allowed_document_types:
        return False
    if _version_key(evidence.get("version")) < _version_key(obligation.minimum_version):
        return False
    if evidence.get("supporting_span_eligible") is not True:
        return False
    supporting_span = str(evidence.get("supporting_span", ""))
    if not supporting_span or not evidence.get("content_hash"):
        return False
    # A published chunk may live below a generic section heading even though its
    # bounded supporting span is directly about the required policy family.
    # Match the frozen policy vocabulary against the complete provider-visible
    # evidence surface, while retaining the document, version, publication,
    # temporal, locator, and source-binding checks below.
    section = str(evidence.get("section_path", ""))
    policy_surface = f"{section}\n{supporting_span}".casefold()
    if not any(term.casefold() in policy_surface for term in obligation.allowed_section_terms):
        return False
    locator = evidence.get("source_locator")
    eligibility = evidence.get("eligibility_envelope")
    if not isinstance(locator, dict) or not locator.get("locator_hash"):
        return False
    if not isinstance(eligibility, dict):
        return False
    if (
        eligibility.get("status") != "active"
        or eligibility.get("outcome") != "eligible"
        or eligibility.get("index_version") != observation_index_version
    ):
        return False
    effective_from = _parse_time(eligibility.get("effective_from"))
    effective_until = _parse_time(eligibility.get("effective_until"))
    if effective_from is None or effective_from > now:
        return False
    if effective_until is not None and effective_until <= now:
        return False
    locator_index = locator.get("index_version")
    return locator_index is None or locator_index == observation_index_version


def _knowledge_entry(
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    observations: Sequence[dict[str, Any]],
    *,
    run_id: str,
    now: datetime,
    citation_bindings: Sequence[ContextCitationBinding],
    provider_attempt_id: str | None,
) -> ObligationEntry:
    candidates = _relevant_observations(observations, run_id=run_id, capability="search_knowledge")
    if not candidates:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="knowledge_read_missing",
        )
    hard_terminal = _hard_terminal_entry(obligation, admission, candidates)
    if hard_terminal is not None:
        return hard_terminal
    observation = next(
        (
            item
            for item in candidates
            if _knowledge_observation_qualifies(
                item,
                obligation,
                admission,
                now=now,
            )
        ),
        candidates[0],
    )
    if observation.get("status") != "ok":
        return _failure_entry(obligation, observation)
    tenant_id, customer_id, scope_hash = _observation_scope(observation)
    if (
        tenant_id != admission.tenant_id
        or customer_id != admission.customer_id
        or scope_hash != admission.scope_hash
    ):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="failed",
            capabilities=obligation.capabilities,
            reason_code="knowledge_scope_mismatch",
        )
    data = observation.get("data")
    if not isinstance(data, dict):
        data = {}
    if data.get("conflict") is True:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="conflicted",
            capabilities=obligation.capabilities,
            reason_code="knowledge_policy_conflict",
        )
    evidence_items = data.get("evidence")
    index_version = str(data.get("index_version") or "")
    if (
        data.get("refusal_reason")
        or not isinstance(evidence_items, list)
        or not evidence_items
        or not index_version
    ):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="knowledge_evidence_empty",
        )
    qualified = [
        item
        for item in evidence_items
        if isinstance(item, dict)
        and _knowledge_evidence_qualifies(
            item,
            obligation,
            observation_index_version=index_version,
            now=now,
        )
    ]
    if not qualified:
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="pending",
            capabilities=obligation.capabilities,
            reason_code="knowledge_evidence_not_qualified",
        )
    evidence_ids = tuple(str(item["evidence_id"]) for item in qualified)
    source_ids = set(_source_ids(observation))
    if not source_ids.intersection(
        {str(item.get("chunk_id") or item.get("evidence_id")) for item in qualified}
    ):
        return ObligationEntry(
            obligation_id=obligation.obligation_id,
            kind="knowledge",
            status="failed",
            capabilities=obligation.capabilities,
            reason_code="knowledge_source_binding_missing",
        )
    matched_citations: list[str] = []
    if provider_attempt_id is not None:
        for binding in citation_bindings:
            if binding.provider_attempt_id != provider_attempt_id:
                continue
            for evidence in qualified:
                locator = evidence.get("source_locator", {})
                if (
                    binding.evidence_id == evidence.get("evidence_id")
                    and binding.document_id == evidence.get("document_id")
                    and binding.chunk_id == evidence.get("chunk_id")
                    and binding.content_hash == evidence.get("content_hash")
                    and binding.locator_hash == locator.get("locator_hash")
                ):
                    matched_citations.append(binding.citation_binding_id)
                    break
    status: Literal["read_qualified", "satisfied"] = (
        "satisfied" if matched_citations else "read_qualified"
    )
    return ObligationEntry(
        obligation_id=obligation.obligation_id,
        kind="knowledge",
        status=status,
        capabilities=obligation.capabilities,
        binding=_base_binding(
            observation,
            tenant_id=tenant_id,
            customer_id=customer_id,
            scope_hash=scope_hash,
            evidence_ids=evidence_ids,
            citation_binding_ids=tuple(sorted(set(matched_citations))),
        ),
        reason_code=(
            "knowledge_obligation_satisfied"
            if status == "satisfied"
            else "knowledge_read_qualified"
        ),
    )


def _knowledge_observation_qualifies(
    observation: dict[str, Any],
    obligation: EvidenceObligationSpec,
    admission: ActionAdmissionV2,
    *,
    now: datetime,
) -> bool:
    if observation.get("status") != "ok":
        return False
    tenant_id, customer_id, scope_hash = _observation_scope(observation)
    if (
        tenant_id != admission.tenant_id
        or customer_id != admission.customer_id
        or scope_hash != admission.scope_hash
    ):
        return False
    data = observation.get("data")
    if not isinstance(data, dict) or data.get("conflict") is True:
        return False
    evidence_items = data.get("evidence")
    index_version = str(data.get("index_version") or "")
    if (
        data.get("refusal_reason")
        or not isinstance(evidence_items, list)
        or not evidence_items
        or not index_version
    ):
        return False
    qualified = [
        item
        for item in evidence_items
        if isinstance(item, dict)
        and _knowledge_evidence_qualifies(
            item,
            obligation,
            observation_index_version=index_version,
            now=now,
        )
    ]
    if not qualified:
        return False
    sources = set(_source_ids(observation))
    return bool(
        sources.intersection(
            {str(item.get("chunk_id") or item.get("evidence_id")) for item in qualified}
        )
    )


def evaluate_action_obligations(
    *,
    action_spec: ActionSpec,
    admission: ActionAdmissionV2,
    observations: Sequence[dict[str, Any]],
    run_id: str,
    citation_bindings: Sequence[ContextCitationBinding] = (),
    provider_attempt_id: str | None = None,
    now: datetime | None = None,
) -> ActionObligationLedger:
    """Rebuild the ledger from current-run observations and trusted bindings."""

    if admission.status != "admitted" or admission.action_type != action_spec.action_type:
        raise ValueError("obligation ledger requires matching admitted action")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    entries: list[ObligationEntry] = []
    for obligation in action_spec.obligations:
        if obligation.kind == "knowledge":
            entry = _knowledge_entry(
                obligation,
                admission,
                observations,
                run_id=run_id,
                now=current_time,
                citation_bindings=citation_bindings,
                provider_attempt_id=provider_attempt_id,
            )
        else:
            entry = _resource_entry(
                action_spec,
                obligation,
                admission,
                observations,
                run_id=run_id,
                now=current_time,
            )
        entries.append(entry)

    statuses = {item.status for item in entries}
    terminal_outcomes = [
        item.terminal_outcome for item in entries if item.terminal_outcome is not None
    ]
    if "failed" in statuses:
        next_state: LedgerNextState = "safe_stop"
        reason_code = next(
            (item.reason_code for item in entries if item.status == "failed" and item.reason_code),
            "obligation_hard_failure",
        )
    elif terminal_outcomes:
        next_state = "explain_terminal"
        reason_code = terminal_outcomes[0].outcome_code
    elif statuses == {"satisfied"}:
        next_state = "assemble_candidate"
        reason_code = "all_obligations_satisfied"
    elif statuses <= {"satisfied", "read_qualified"}:
        next_state = "synthesize"
        reason_code = "all_reads_qualified"
    elif "conflicted" in statuses:
        next_state = "safe_stop"
        reason_code = "obligation_conflict"
    else:
        next_state = "collect_reads"
        reason_code = "obligations_pending"
    unsatisfied = (
        ()
        if terminal_outcomes
        else tuple(
            sorted(
                {
                    capability
                    for item in entries
                    if item.status in {"pending", "stale"}
                    for capability in item.capabilities
                }
            )
        )
    )
    return ActionObligationLedger(
        schema_version="action-obligation-ledger.v2",
        action_spec_version=action_spec.schema_version,
        action_type=action_spec.action_type,
        run_id=run_id,
        tenant_id=admission.tenant_id,
        customer_id=admission.customer_id,
        scope_hash=admission.scope_hash,
        obligations=tuple(entries),
        unsatisfied_capabilities=unsatisfied,
        next_state=next_state,
        reason_code=reason_code,
        terminal_outcome=terminal_outcomes[0] if terminal_outcomes else None,
    )
