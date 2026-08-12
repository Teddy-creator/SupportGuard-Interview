from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from supportguard.actions.service import (
    ActionSpec,
    ProposalFieldBinding,
    get_action_spec_by_proposal,
)
from supportguard.agent.obligations import ActionObligationLedger
from supportguard.agent.schemas import (
    BoundEvidenceSynthesis,
    CandidateCitation,
    CandidateResponse,
    GroundedRepairEligibility,
    MaterialClaim,
    ProposalEligibility,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.contracts.action_preconditions import ActionAdmissionV2


class ActionAssemblyError(RuntimeError):
    pass


class SynthesisBindingError(ActionAssemblyError):
    def __init__(self, error_paths: Sequence[str]) -> None:
        self.error_paths = tuple(dict.fromkeys(error_paths))
        super().__init__(";".join(self.error_paths))


def evaluate_grounded_repair_eligibility(
    *,
    obligation_synthesis_mode: bool,
    admission_payload: dict[str, Any] | None,
    evidence: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    knowledge_comparison_complete: bool,
) -> GroundedRepairEligibility:
    """Select a terminal Grounded Repair from current Context authority only.

    The result deliberately contains counts and stable reason codes, never
    Provider text, queries, evidence content, or business payloads.  This makes
    a rejected route independently diagnosable without weakening its authority
    boundary.
    """

    eligible_knowledge = [
        item
        for item in evidence
        if item.get("citation_binding_id") and item.get("supporting_span_eligible") is True
    ]
    group_counts: dict[str, int] = {}
    for item in eligible_knowledge:
        group = str(item.get("evidence_group") or "current")
        group_counts[group] = group_counts.get(group, 0) + 1
    successful_knowledge_observations = [
        observation
        for observation in observations
        if observation.get("status") == "ok" and observation.get("tool_name") == "search_knowledge"
    ]
    successful_business_observations = [
        observation
        for observation in observations
        if observation.get("status") == "ok" and observation.get("tool_name") != "search_knowledge"
    ]
    business_source_ids = {
        str(source["source_id"])
        for observation in successful_business_observations
        for source in observation.get("source_refs", [])
        if isinstance(source, dict) and source.get("source_id")
    }

    admission = admission_payload if isinstance(admission_payload, dict) else {}
    if obligation_synthesis_mode:
        reason_code = "obligation_synthesis_active"
    elif admission.get("schema_version") != "action-admission.v2":
        reason_code = "action_admission_schema_invalid"
    elif admission.get("status") != "none" or admission.get("planned_action") != "none":
        reason_code = "action_admission_active"
    elif not eligible_knowledge and not business_source_ids:
        reason_code = "eligible_authority_missing"
    else:
        reason_code = "selected"

    selected = reason_code == "selected"
    return GroundedRepairEligibility(
        selected=selected,
        reason_code=reason_code,
        require_knowledge_source=bool(selected and eligible_knowledge),
        require_business_source=bool(selected and business_source_ids),
        context_evidence_count=len(evidence),
        eligible_knowledge_count=len(eligible_knowledge),
        eligible_knowledge_group_counts=dict(sorted(group_counts.items())),
        successful_knowledge_observation_count=len(successful_knowledge_observations),
        successful_business_observation_count=len(successful_business_observations),
        unique_business_source_count=len(business_source_ids),
        knowledge_comparison_complete=knowledge_comparison_complete,
    )


def _denied_eligibility(
    action_spec: ActionSpec | None,
    error_code: str,
) -> ProposalEligibility:
    return ProposalEligibility(
        eligible=False,
        action_type=action_spec.action_type if action_spec is not None else None,
        error_code=error_code,
    )


def _proposal_action_spec(candidate: CandidateResponse) -> ActionSpec | None:
    return get_action_spec_by_proposal(candidate.action)


def _bound_observation(
    *,
    ledger: ActionObligationLedger,
    observation_id: str,
    tool_name: str,
    observations: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    matches = [
        item
        for item in observations
        if item.get("run_id") == ledger.run_id
        and item.get("tool_name") == tool_name
        and (
            item.get("observation_id") == observation_id
            or (not item.get("observation_id") and item.get("tool_call_id") == observation_id)
        )
    ]
    return matches[0] if len(matches) == 1 else None


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


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


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


def _source_ids(observation: dict[str, Any]) -> set[str]:
    refs = observation.get("source_refs")
    if not isinstance(refs, list):
        return set()
    return {
        str(item["source_id"]) for item in refs if isinstance(item, dict) and item.get("source_id")
    }


@dataclass(frozen=True, slots=True)
class _EligibilityContext:
    action_spec: ActionSpec
    admission: ActionAdmissionV2
    ledger: ActionObligationLedger
    effective_now: datetime
    claim_citations: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ValidatedObservationBindings:
    values: tuple[dict[str, Any], ...]
    resource_type: str
    resource_id: str
    resource_version: int


def _validate_candidate_context(
    *,
    candidate: CandidateResponse,
    admission_payload: dict[str, Any] | None,
    ledger_payload: dict[str, Any] | None,
    now: datetime | None,
) -> tuple[_EligibilityContext | None, ProposalEligibility | None]:
    action_spec = _proposal_action_spec(candidate)
    if action_spec is None:
        return None, _denied_eligibility(None, "not_proposal")
    try:
        admission = ActionAdmissionV2.model_validate(admission_payload or {})
    except (TypeError, ValueError):
        return None, _denied_eligibility(action_spec, "proposal_action_admission_missing")
    try:
        ledger = ActionObligationLedger.model_validate(ledger_payload or {})
    except (TypeError, ValueError):
        return None, _denied_eligibility(action_spec, "proposal_obligation_ledger_missing")
    if (
        admission.status != "admitted"
        or admission.action_type != action_spec.action_type
        or ledger.action_type != action_spec.action_type
        or ledger.run_id == ""
    ):
        return None, _denied_eligibility(action_spec, "proposal_action_binding_mismatch")
    if not ledger.all_obligations_satisfied:
        error_code = (
            "proposal_resource_observation_stale"
            if any(item.status == "stale" for item in ledger.obligations)
            else "proposal_obligation_not_satisfied"
        )
        return None, _denied_eligibility(action_spec, error_code)

    expected_arguments = {item.target_field for item in action_spec.proposal_fields}
    if set(candidate.proposed_arguments) != expected_arguments:
        return None, _denied_eligibility(action_spec, "proposal_argument_contract_mismatch")
    try:
        validated_arguments = action_spec.proposal_schema.model_validate(
            candidate.proposed_arguments
        ).model_dump(mode="json")
    except (TypeError, ValueError):
        return None, _denied_eligibility(action_spec, "proposal_argument_schema_invalid")
    if validated_arguments != candidate.proposed_arguments:
        return None, _denied_eligibility(action_spec, "proposal_argument_not_canonical")

    claim_citations = frozenset(
        binding_id
        for claim in candidate.material_claims
        for binding_id in claim.citation_binding_ids
    )
    claim_sources = {
        source_id
        for claim in candidate.material_claims
        for source_id in claim.observation_source_ids
    }
    knowledge_citations = {
        binding_id
        for item in ledger.obligations
        if item.kind == "knowledge" and item.binding is not None
        for binding_id in item.binding.citation_binding_ids
    }
    business_sources = {
        source_id
        for item in ledger.obligations
        if item.kind != "knowledge" and item.binding is not None
        for source_id in item.binding.source_ids
    }
    if not knowledge_citations:
        return None, _denied_eligibility(action_spec, "proposal_knowledge_evidence_missing")
    if not claim_citations or not claim_citations <= knowledge_citations:
        return None, _denied_eligibility(action_spec, "proposal_claim_citation_missing")
    if not claim_sources or not claim_sources <= business_sources:
        return None, _denied_eligibility(action_spec, "proposal_business_source_binding_missing")
    return (
        _EligibilityContext(
            action_spec=action_spec,
            admission=admission,
            ledger=ledger,
            effective_now=now or datetime.now(UTC),
            claim_citations=claim_citations,
        ),
        None,
    )


def _validate_observation_bindings(
    *,
    context: _EligibilityContext,
    observations: Sequence[dict[str, Any]],
) -> tuple[_ValidatedObservationBindings | None, str | None]:
    values: list[dict[str, Any]] = []
    primary_resource_type: str | None = None
    primary_resource_id: str | None = None
    primary_resource_version: int | None = None
    for obligation_spec in context.action_spec.obligations:
        entry = next(
            (
                item
                for item in context.ledger.obligations
                if item.obligation_id == obligation_spec.obligation_id
            ),
            None,
        )
        if entry is None or entry.binding is None:
            return None, "proposal_observation_binding_missing"
        observation = _bound_observation(
            ledger=context.ledger,
            observation_id=entry.binding.observation_id,
            tool_name=entry.binding.tool_name,
            observations=observations,
        )
        if observation is None:
            return None, "proposal_resource_observation_mismatch"
        data = observation.get("data")
        if not isinstance(data, dict):
            return None, "proposal_observation_data_missing"
        binding = entry.binding
        tenant_id, customer_id, scope_hash = _observation_scope(observation)
        if (
            observation.get("status") != "ok"
            or tenant_id != context.admission.tenant_id
            or customer_id != context.admission.customer_id
            or scope_hash != context.admission.scope_hash
            or binding.tenant_id != context.admission.tenant_id
            or binding.customer_id != context.admission.customer_id
            or binding.scope_hash != context.admission.scope_hash
        ):
            return None, "proposal_observation_scope_mismatch"
        observed_hash = str(
            observation.get("observation_content_hash")
            or observation.get("content_hash")
            or _canonical_hash(observation)
        )
        if observed_hash != binding.observation_content_hash:
            return None, "proposal_observation_content_mismatch"
        sources = _source_ids(observation)
        if not binding.source_ids or not set(binding.source_ids) <= sources:
            return None, "proposal_observation_source_mismatch"
        if any(data.get(field) is None for field in obligation_spec.required_data_fields) or any(
            not data.get(field) for field in obligation_spec.required_truthy_data_fields
        ):
            return None, "proposal_observation_data_incomplete"
        if (
            obligation_spec.allowed_resource_statuses
            and str(data.get("status")) not in obligation_spec.allowed_resource_statuses
        ):
            return None, "proposal_resource_status_conflict"
        if obligation_spec.require_freshness:
            fresh_until = _parse_time(observation.get("fresh_until"))
            if (
                observation.get("freshness_status") != "fresh"
                or binding.freshness_status != "fresh"
                or fresh_until is None
                or fresh_until < context.effective_now
                or binding.fresh_until is None
                or binding.fresh_until < context.effective_now
            ):
                return None, "proposal_resource_observation_stale"
        if obligation_spec.resource_ref_argument is not None:
            admitted_ref = str(
                context.admission.extracted_arguments.get(obligation_spec.resource_ref_argument, "")
            )
            equivalent_refs = {
                str(data.get(obligation_spec.observed_resource_field or "", "")),
                str(data.get("fingerprint", "")),
            }
            if not admitted_ref or admitted_ref.casefold() not in {
                item.casefold() for item in equivalent_refs if item
            }:
                return None, "proposal_resource_observation_mismatch"
        projected_binding = {
            "tool_name": binding.tool_name,
            "tool_call_id": observation.get("tool_call_id"),
            "invocation_id": binding.invocation_id,
            "observation_id": binding.observation_id,
            "observation_content_hash": binding.observation_content_hash,
            "turn_group_id": observation.get("turn_group_id"),
            "status": "ok",
            "observed_at": observation.get("observed_at"),
            "source_refs": observation.get("source_refs", []),
        }
        if obligation_spec.kind == "knowledge":
            projected_binding.update(
                {
                    "index_version": data.get("index_version"),
                    "citation_binding_ids": sorted(context.claim_citations),
                }
            )
        elif obligation_spec.observed_resource_field is not None:
            resource_id = str(data.get(obligation_spec.observed_resource_field) or "")
            try:
                resource_version = int(binding.resource_version or "")
            except ValueError:
                return None, "proposal_resource_version_missing"
            projected_binding.update(
                {
                    "resource_field": obligation_spec.observed_resource_field,
                    "resource_id": resource_id,
                    "resource_version": resource_version,
                }
            )
            if primary_resource_id is None:
                primary_resource_type = obligation_spec.observed_resource_field
                primary_resource_id = resource_id
                primary_resource_version = resource_version
        values.append(projected_binding)

    if primary_resource_type is None or not primary_resource_id or primary_resource_version is None:
        return None, "proposal_resource_identity_missing"
    return (
        _ValidatedObservationBindings(
            values=tuple(values),
            resource_type=primary_resource_type,
            resource_id=primary_resource_id,
            resource_version=primary_resource_version,
        ),
        None,
    )


def _proposal_field_binding_error(
    *,
    candidate: CandidateResponse,
    context: _EligibilityContext,
    observations: Sequence[dict[str, Any]],
) -> str | None:
    for field in context.action_spec.proposal_fields:
        actual = candidate.proposed_arguments.get(field.target_field)
        if field.source == "admission":
            expected = context.admission.extracted_arguments.get(field.source_path)
        elif field.source == "request_reason":
            expected = context.admission.request_reason
        else:
            entry = next(
                item
                for item in context.ledger.obligations
                if item.obligation_id == field.obligation_id
            )
            if entry.binding is None:
                return "proposal_observation_binding_missing"
            observation = _bound_observation(
                ledger=context.ledger,
                observation_id=entry.binding.observation_id,
                tool_name=entry.binding.tool_name,
                observations=observations,
            )
            expected = (
                observation.get("data", {}).get(field.source_path)
                if observation is not None
                else None
            )
        if actual != expected:
            return f"proposal_field_binding_mismatch:{field.target_field}"
    return None


def evaluate_action_candidate_eligibility(
    *,
    candidate: CandidateResponse,
    admission_payload: dict[str, Any] | None,
    ledger_payload: dict[str, Any] | None,
    observations: Sequence[dict[str, Any]],
    now: datetime | None = None,
) -> ProposalEligibility:
    """Revalidate one assembled proposal through the ActionSpec-owned contract."""

    context, denied = _validate_candidate_context(
        candidate=candidate,
        admission_payload=admission_payload,
        ledger_payload=ledger_payload,
        now=now,
    )
    if denied is not None:
        return denied
    if context is None:  # pragma: no cover - the pair is an internal invariant
        raise RuntimeError("proposal_eligibility_context_missing")
    bindings, error_code = _validate_observation_bindings(
        context=context,
        observations=observations,
    )
    if error_code is not None:
        return _denied_eligibility(context.action_spec, error_code)
    if bindings is None:  # pragma: no cover - the pair is an internal invariant
        raise RuntimeError("proposal_observation_bindings_missing")
    error_code = _proposal_field_binding_error(
        candidate=candidate,
        context=context,
        observations=observations,
    )
    if error_code is not None:
        return _denied_eligibility(context.action_spec, error_code)

    return ProposalEligibility(
        eligible=True,
        action_type=context.action_spec.action_type,
        resource_type=bindings.resource_type,
        resource_id=bindings.resource_id,
        resource_version=bindings.resource_version,
        trusted_arguments=dict(candidate.proposed_arguments),
        observation_binding=list(bindings.values),
        citation_binding_ids=sorted(context.claim_citations),
    )


def provider_synthesis_binding_error_paths(
    *,
    synthesis: ProviderBoundEvidenceSynthesis,
    evidence: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    require_knowledge_source: bool = True,
    require_business_source: bool = True,
    required_knowledge_groups: Sequence[str] = (),
    required_answer_markers: Sequence[str] = (),
) -> tuple[str, ...]:
    """Validate provider-proposed references against the current context membership.

    The provider may select evidence that was actually injected into its context,
    but it cannot create citation, locator, chunk, or business-source authority.
    Returning stable schema-like paths lets the single bounded structure-repair
    attempt correct reference placement without weakening the Runtime boundary.
    """

    citation_membership = {
        str(item["citation_binding_id"]): {
            "chunk_id": str(item.get("chunk_id") or ""),
            "locator_hash": str(item.get("source_locator_hash") or ""),
            "evidence_group": str(item.get("evidence_group") or "current"),
        }
        for item in evidence
        if item.get("citation_binding_id") and item.get("supporting_span_eligible") is True
    }
    allowed_citations = set(citation_membership)
    allowed_business_sources = {
        str(source["source_id"])
        for observation in observations
        if observation.get("status") == "ok" and observation.get("tool_name") != "search_knowledge"
        for source in observation.get("source_refs", [])
        if isinstance(source, dict) and source.get("source_id")
    }
    errors: list[str] = []
    used_citations: set[str] = set()
    used_sources: set[str] = set()

    for index, claim in enumerate(synthesis.material_claims):
        claim_citations = set(claim.citation_binding_ids)
        claim_sources = set(claim.observation_source_ids)
        unknown_citations = claim_citations - allowed_citations
        unknown_sources = claim_sources - allowed_business_sources
        if unknown_citations:
            errors.append(f"material_claims.{index}.citation_binding_ids:unknown_context_citation")
        if unknown_sources:
            errors.append(f"material_claims.{index}.observation_source_ids:unknown_business_source")
        if not claim_citations and not claim_sources:
            errors.append(f"material_claims.{index}:support_reference_required")
        used_citations.update(claim_citations & allowed_citations)
        used_sources.update(claim_sources & allowed_business_sources)

    if require_knowledge_source and not used_citations:
        errors.append("material_claims:citation_binding_required")
    if require_business_source and not used_sources:
        errors.append("material_claims:business_source_required")
    used_knowledge_groups = {
        citation_membership[binding_id]["evidence_group"]
        for binding_id in used_citations
        if binding_id in citation_membership
    }
    for group in dict.fromkeys(str(item) for item in required_knowledge_groups if str(item)):
        if group not in used_knowledge_groups:
            errors.append(f"material_claims:citation_group_required:{group}")
    public_claim_text = (
        "\n".join(claim.text.strip() for claim in synthesis.material_claims if claim.text.strip())
        or synthesis.answer
    ).casefold()
    for marker in dict.fromkeys(str(item) for item in required_answer_markers if str(item)):
        if marker.casefold() not in public_claim_text:
            errors.append(f"material_claims:required_marker_missing:{marker}")
    return tuple(dict.fromkeys(errors))


def provider_synthesis_reference_contract(
    *,
    evidence: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    require_knowledge_source: bool = True,
    require_business_source: bool = True,
    required_knowledge_groups: Sequence[str] = (),
    required_answer_markers: Sequence[str] = (),
) -> dict[str, Any]:
    """Project the exact current-context reference namespace for bounded repair."""

    citations_by_group: dict[str, set[str]] = {}
    for item in evidence:
        if not item.get("citation_binding_id") or item.get("supporting_span_eligible") is not True:
            continue
        citations_by_group.setdefault(str(item.get("evidence_group") or "current"), set()).add(
            str(item["citation_binding_id"])
        )
    required_groups = list(
        dict.fromkeys(str(item) for item in required_knowledge_groups if str(item))
    )
    required_markers = list(
        dict.fromkeys(str(item) for item in required_answer_markers if str(item))
    )
    return {
        "allowed_citation_binding_ids": sorted(
            {
                str(item["citation_binding_id"])
                for item in evidence
                if item.get("citation_binding_id") and item.get("supporting_span_eligible") is True
            }
        ),
        "allowed_observation_source_ids": sorted(
            {
                str(source["source_id"])
                for observation in observations
                if observation.get("status") == "ok"
                and observation.get("tool_name") != "search_knowledge"
                for source in observation.get("source_refs", [])
                if isinstance(source, dict) and source.get("source_id")
            }
        ),
        "allowed_citation_binding_ids_by_group": {
            group: sorted(binding_ids) for group, binding_ids in sorted(citations_by_group.items())
        },
        "required_knowledge_groups": required_groups,
        "required_answer_markers": required_markers,
        "contract_instruction": (
            "Satisfy every global_rule using only the exact allowed IDs. "
            "When required_knowledge_groups is non-empty, cite at least one ID "
            "from each matching group. When required_answer_markers is non-empty, "
            "include every marker directly in the public material claim text."
        ),
        "per_claim_rule": (
            "each material claim must contain at least one exact allowed "
            "citation_binding_id or observation_source_id; otherwise omit the claim"
        ),
        "global_rules": [
            rule
            for required, rule in (
                (
                    require_knowledge_source,
                    "at least one material claim must use an allowed citation_binding_id",
                ),
                (
                    require_business_source,
                    "at least one material claim must use an allowed observation_source_id",
                ),
                *(
                    (
                        True,
                        (
                            "material claims must cite at least one allowed "
                            f"citation_binding_id from evidence_group={group}"
                        ),
                    )
                    for group in required_groups
                ),
                (
                    bool(required_markers),
                    (
                        "the public material claim text must directly include every "
                        "required_answer_marker"
                    ),
                ),
            )
            if required
        ],
    }


def bind_provider_synthesis(
    *,
    synthesis: ProviderBoundEvidenceSynthesis,
    evidence: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    require_knowledge_source: bool = True,
    require_business_source: bool = True,
    required_knowledge_groups: Sequence[str] = (),
    required_answer_markers: Sequence[str] = (),
) -> BoundEvidenceSynthesis:
    """Bind provider-selected claims to deterministic current-context identities."""

    errors = provider_synthesis_binding_error_paths(
        synthesis=synthesis,
        evidence=evidence,
        observations=observations,
        require_knowledge_source=require_knowledge_source,
        require_business_source=require_business_source,
        required_knowledge_groups=required_knowledge_groups,
        required_answer_markers=required_answer_markers,
    )
    if errors:
        raise SynthesisBindingError(errors)

    citation_membership = {
        str(item["citation_binding_id"]): {
            "chunk_id": str(item.get("chunk_id") or ""),
            "locator_hash": str(item.get("source_locator_hash") or ""),
            "evidence_group": str(item.get("evidence_group") or "current"),
        }
        for item in evidence
        if item.get("citation_binding_id") and item.get("supporting_span_eligible") is True
    }
    material_claims: list[MaterialClaim] = []
    used_citations: set[str] = set()
    used_sources: set[str] = set()
    for claim in synthesis.material_claims:
        claim_citations = sorted(set(claim.citation_binding_ids))
        claim_sources = sorted(set(claim.observation_source_ids))
        used_citations.update(claim_citations)
        used_sources.update(claim_sources)
        material_claims.append(
            MaterialClaim(
                text=claim.text,
                citation_binding_ids=claim_citations,
                knowledge_locator_hashes=sorted(
                    {
                        citation_membership[binding_id]["locator_hash"]
                        for binding_id in claim_citations
                        if citation_membership[binding_id]["locator_hash"]
                    }
                ),
                observation_source_ids=claim_sources,
            )
        )
    used_chunks = sorted(
        {
            citation_membership[binding_id]["chunk_id"]
            for binding_id in used_citations
            if citation_membership[binding_id]["chunk_id"]
        }
    )
    return BoundEvidenceSynthesis(
        answer=synthesis.answer,
        knowledge_chunk_ids=used_chunks,
        knowledge_citations=[
            CandidateCitation(citation_binding_id=binding_id)
            for binding_id in sorted(used_citations)
        ],
        business_source_ids=sorted(used_sources),
        material_claims=material_claims,
    )


def canonicalize_unreferenced_provider_claims(
    *,
    synthesis: ProviderBoundEvidenceSynthesis,
    evidence: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    require_knowledge_source: bool = True,
    require_business_source: bool = True,
    required_knowledge_groups: Sequence[str] = (),
    required_answer_markers: Sequence[str] = (),
) -> tuple[ProviderBoundEvidenceSynthesis, BoundEvidenceSynthesis, tuple[int, ...]] | None:
    """Delete only binding-proven unsupported claims and fully rebind the result.

    This is a fail-closed canonicalization for the run's already-consumed
    structure-repair response.  It never assigns references or keeps the
    provider's top-level answer, because either could preserve unsupported
    content.  Every initial binding error must identify a claim whose two
    reference namespaces are both empty; the public answer is then derived
    only from the retained claim text and the complete binding contract is run
    again.
    """

    errors = provider_synthesis_binding_error_paths(
        synthesis=synthesis,
        evidence=evidence,
        observations=observations,
        require_knowledge_source=require_knowledge_source,
        require_business_source=require_business_source,
        required_knowledge_groups=required_knowledge_groups,
        required_answer_markers=required_answer_markers,
    )
    if not errors:
        return None

    prefix = "material_claims."
    suffix = ":support_reference_required"
    indices: list[int] = []
    for error in errors:
        if not error.startswith(prefix) or not error.endswith(suffix):
            return None
        raw_index = error[len(prefix) : -len(suffix)]
        if not raw_index.isdigit():
            return None
        index = int(raw_index)
        if index >= len(synthesis.material_claims) or index in indices:
            return None
        claim = synthesis.material_claims[index]
        if claim.citation_binding_ids or claim.observation_source_ids:
            return None
        indices.append(index)

    pruned_indices = tuple(sorted(indices))
    retained_claims = [
        claim
        for index, claim in enumerate(synthesis.material_claims)
        if index not in pruned_indices
    ]
    if not retained_claims:
        return None
    derived_answer = "\n".join(claim.text for claim in retained_claims)
    try:
        canonical = ProviderBoundEvidenceSynthesis(
            schema_version=synthesis.schema_version,
            answer=derived_answer,
            material_claims=retained_claims,
        )
        bound = bind_provider_synthesis(
            synthesis=canonical,
            evidence=evidence,
            observations=observations,
            require_knowledge_source=require_knowledge_source,
            require_business_source=require_business_source,
            required_knowledge_groups=required_knowledge_groups,
            required_answer_markers=required_answer_markers,
        )
    except (SynthesisBindingError, TypeError, ValueError):
        return None
    return canonical, bound, pruned_indices


def _observation_by_binding(
    *,
    field: ProposalFieldBinding,
    ledger: ActionObligationLedger,
    observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    entry = next(
        (item for item in ledger.obligations if item.obligation_id == field.obligation_id),
        None,
    )
    if entry is None or entry.binding is None or entry.status != "satisfied":
        raise ActionAssemblyError("proposal_obligation_not_satisfied")
    matches = [
        item
        for item in observations
        if item.get("run_id") == ledger.run_id
        and (
            item.get("observation_id") == entry.binding.observation_id
            or (
                not item.get("observation_id")
                and item.get("tool_call_id") == entry.binding.observation_id
            )
        )
    ]
    if len(matches) != 1:
        raise ActionAssemblyError("proposal_observation_binding_ambiguous")
    data = matches[0].get("data")
    if not isinstance(data, dict):
        raise ActionAssemblyError("proposal_observation_data_missing")
    return data


def _proposal_value(
    *,
    field: ProposalFieldBinding,
    admission: ActionAdmissionV2,
    ledger: ActionObligationLedger,
    observations: Sequence[dict[str, Any]],
) -> Any:
    if field.source == "request_reason":
        value: Any = admission.request_reason
    elif field.source == "admission":
        value = admission.extracted_arguments.get(field.source_path)
    else:
        value = _observation_by_binding(
            field=field,
            ledger=ledger,
            observations=observations,
        ).get(field.source_path)
    if value is None or value == "":
        raise ActionAssemblyError(f"proposal_field_missing:{field.target_field}")
    return value


def assemble_action_candidate(
    *,
    action_spec: ActionSpec,
    admission: ActionAdmissionV2,
    ledger: ActionObligationLedger,
    observations: Sequence[dict[str, Any]],
    synthesis: BoundEvidenceSynthesis,
) -> CandidateResponse:
    """Assemble one typed proposal without action-specific Graph branches."""

    if (
        admission.status != "admitted"
        or admission.action_type != action_spec.action_type
        or ledger.action_type != action_spec.action_type
        or not ledger.all_obligations_satisfied
    ):
        raise ActionAssemblyError("proposal_preconditions_not_satisfied")
    knowledge_bindings = {
        binding_id
        for item in ledger.obligations
        if item.kind == "knowledge" and item.binding is not None
        for binding_id in item.binding.citation_binding_ids
    }
    business_sources = {
        source_id
        for item in ledger.obligations
        if item.kind != "knowledge" and item.binding is not None
        for source_id in item.binding.source_ids
    }
    if not knowledge_bindings or not business_sources or not synthesis.material_claims:
        raise ActionAssemblyError("proposal_claim_binding_incomplete")
    used_citations = {
        binding_id
        for claim in synthesis.material_claims
        for binding_id in claim.citation_binding_ids
    }
    used_sources = {
        source_id
        for claim in synthesis.material_claims
        for source_id in claim.observation_source_ids
    }
    if not used_citations or not used_citations <= knowledge_bindings:
        raise ActionAssemblyError("proposal_citation_binding_invalid")
    if not used_sources or not used_sources <= business_sources:
        raise ActionAssemblyError("proposal_business_binding_invalid")
    proposed_arguments = {
        field.target_field: _proposal_value(
            field=field,
            admission=admission,
            ledger=ledger,
            observations=observations,
        )
        for field in action_spec.proposal_fields
    }
    return CandidateResponse.model_validate(
        {
            "answer": synthesis.answer,
            "action": action_spec.proposal_action,
            "knowledge_chunk_ids": synthesis.knowledge_chunk_ids,
            "knowledge_citations": [
                CandidateCitation(citation_binding_id=binding_id).model_dump(mode="json")
                for binding_id in sorted(used_citations)
            ],
            "business_source_ids": sorted(used_sources),
            "material_claims": [item.model_dump(mode="json") for item in synthesis.material_claims],
            "proposed_arguments": proposed_arguments,
        }
    )
