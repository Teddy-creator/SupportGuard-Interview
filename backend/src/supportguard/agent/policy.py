from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from supportguard.agent.responses import safe_failure_answer
from supportguard.agent.schemas import CandidateResponse


class PolicyRoute(StrEnum):
    ANSWER = "answer"
    AWAIT_APPROVAL = "await_human_approval"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """Pure, already-observed facts used to select a publication route.

    The carrier contains no service handle, persistence object, Provider
    envelope, or MCP capability.  It can only select a route and a reason;
    mutation authorization remains owned by the Action pipeline.
    """

    candidate: CandidateResponse
    evidence_conflict: bool
    citation_integrity: bool
    proposal_eligible: bool | None
    finish_reason: str | None
    safe_stop_reason: str | None
    requested_action_unresolved: bool
    evidence_assessment_result: Literal["accept", "replan", "terminal"]
    evidence_assessment_error_code: str | None
    has_secret_redaction: bool
    policy_boundary: str
    knowledge_comparison_requested: bool
    knowledge_comparison_complete: bool
    explainable_comparison: bool
    comparison_citations_complete: bool
    missing_transition_markers: tuple[str, ...]
    grounded_conflict_clarification: bool
    requested_current_fact_missing: bool
    mixed_account_applicability_missing: bool


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    candidate_json: str = field(repr=False)
    route: PolicyRoute
    finish_reason: str | None
    unsafe_terminal_reason: str | None
    grants_mutation: Literal[False] = field(default=False, init=False)
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        candidate = CandidateResponse.model_validate_json(self.candidate_json)
        canonical = json.dumps(
            candidate.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        object.__setattr__(self, "candidate_json", canonical)
        object.__setattr__(
            self,
            "candidate_sha256",
            hashlib.sha256(canonical.encode()).hexdigest(),
        )

    @property
    def candidate(self) -> CandidateResponse:
        """Return a fresh projection of the hash-bound candidate."""

        return CandidateResponse.model_validate_json(self.candidate_json)


def decide_policy(
    candidate: CandidateResponse,
    *,
    evidence_conflict: bool,
    citation_integrity: bool = True,
    proposal_eligible: bool | None = None,
) -> PolicyRoute:
    """Select only product-supported routes; never grant a mutation."""

    if not citation_integrity:
        return PolicyRoute.ANSWER
    if candidate.action in {
        "refund_proposal",
        "api_key_revocation_proposal",
        "entitlement_change_proposal",
    }:
        return PolicyRoute.AWAIT_APPROVAL if proposal_eligible is True else PolicyRoute.ANSWER
    if candidate.action == "escalate":
        return PolicyRoute.ANSWER
    if candidate.action == "reject":
        return PolicyRoute.REJECT
    if evidence_conflict or candidate.action == "manual_takeover":
        return PolicyRoute.ANSWER
    return PolicyRoute.ANSWER


def evaluate_policy(policy_input: PolicyInput) -> PublicationDecision:
    """Resolve one deterministic publication decision from bounded facts."""

    route = decide_policy(
        policy_input.candidate,
        evidence_conflict=policy_input.evidence_conflict,
        citation_integrity=policy_input.citation_integrity,
        proposal_eligible=policy_input.proposal_eligible,
    )
    finish_reason = policy_input.finish_reason
    if policy_input.safe_stop_reason:
        route = PolicyRoute.ANSWER
        finish_reason = policy_input.safe_stop_reason
    elif policy_input.requested_action_unresolved:
        route = PolicyRoute.ANSWER
        finish_reason = "requested_action_unresolved"
    elif policy_input.evidence_assessment_result == "terminal":
        route = PolicyRoute.ANSWER
        finish_reason = policy_input.evidence_assessment_error_code or "evidence_group_incomplete"

    if policy_input.has_secret_redaction:
        route = PolicyRoute.ANSWER
        finish_reason = "credential_redaction_guidance"
    elif policy_input.policy_boundary in {"out_of_scope", "prohibited"}:
        route = PolicyRoute.REJECT
        finish_reason = "rejected"
    elif finish_reason == "needs_clarification":
        route = PolicyRoute.ANSWER
    elif policy_input.proposal_eligible is False:
        route = PolicyRoute.ANSWER
        finish_reason = "proposal_eligibility_failed"
    elif route == PolicyRoute.AWAIT_APPROVAL:
        finish_reason = "proposal_policy_approved"
    elif route == PolicyRoute.REJECT:
        finish_reason = "rejected"

    unsafe_terminal_reason: str | None = None
    if policy_input.candidate.action in {"escalate", "manual_takeover"}:
        unsafe_terminal_reason = "human_handoff_unavailable"
    elif (
        policy_input.knowledge_comparison_requested
        and not policy_input.knowledge_comparison_complete
    ):
        unsafe_terminal_reason = "comparison_evidence_incomplete"
    elif policy_input.knowledge_comparison_complete and not policy_input.explainable_comparison:
        unsafe_terminal_reason = (
            "comparison_transition_incomplete"
            if policy_input.comparison_citations_complete
            and policy_input.missing_transition_markers
            else "comparison_citation_incomplete"
        )
    elif policy_input.evidence_conflict and not (
        (policy_input.grounded_conflict_clarification and policy_input.citation_integrity)
        or policy_input.explainable_comparison
    ):
        unsafe_terminal_reason = "evidence_conflict"
    elif (
        not policy_input.citation_integrity
        and not policy_input.safe_stop_reason
        and finish_reason
        not in {
            "needs_clarification",
            "proposal_eligibility_failed",
            "requested_action_unresolved",
            "credential_redaction_guidance",
        }
    ):
        unsafe_terminal_reason = "citation_binding_incomplete"
    elif policy_input.requested_current_fact_missing:
        unsafe_terminal_reason = "explicit_current_fact_incomplete"
    elif policy_input.mixed_account_applicability_missing:
        unsafe_terminal_reason = "mixed_account_applicability_incomplete"

    if unsafe_terminal_reason is not None:
        route = PolicyRoute.ANSWER
        finish_reason = unsafe_terminal_reason
    candidate = policy_input.candidate
    if unsafe_terminal_reason is not None:
        candidate = CandidateResponse(
            answer=safe_failure_answer(unsafe_terminal_reason),
            action="answer",
            knowledge_chunk_ids=[],
            knowledge_citations=[],
            business_source_ids=[],
            material_claims=[],
            proposed_arguments={},
        )
    return PublicationDecision(
        candidate_json=candidate.model_dump_json(),
        route=route,
        finish_reason=finish_reason,
        unsafe_terminal_reason=unsafe_terminal_reason,
    )


__all__ = [
    "PolicyInput",
    "PolicyRoute",
    "PublicationDecision",
    "decide_policy",
    "evaluate_policy",
]
