from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.actions.service import get_action_spec
from supportguard.agent.api_diagnostics import api_rate_limit_diagnostic_reads_complete
from supportguard.agent.comparison_publication import canonicalize_comparison_citation_groups
from supportguard.agent.constants import MAX_LLM_CALLS, MAX_TOOL_ATTEMPTS, MAX_TOOL_ROUNDS
from supportguard.agent.context import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetExceeded,
    build_trusted_task_state,
    latest_assistant_history_message,
    usable_current_knowledge_observation,
)
from supportguard.agent.current_facts import (
    requested_current_fact_contract_valid,
    requested_current_fact_reads_complete,
    requested_current_fact_requirements,
    requested_current_fact_status,
)
from supportguard.agent.evidence import (
    EvidenceAssessment,
    applicability_dimension_answered,
    applicability_scope_claim,
    assess_terminal_evidence,
    candidate_public_claim_text,
    comparison_transition_claim,
    comparison_transition_markers,
    comparison_transition_roles_explicit,
    explicit_applicability_conditions,
    generic_applicability_dimension_claim,
    missing_comparison_transition_markers,
    missing_referential_applicability_requirements,
    referential_applicability_contract,
    requested_generic_applicability_dimensions,
    supported_referential_facets,
)
from supportguard.agent.evidence_contracts import EvidenceDecision
from supportguard.agent.freshness import prune_stale_business_claims
from supportguard.agent.legacy_recovery import recover_legacy_action_admission
from supportguard.agent.nodes.finalization import (
    SafeStopHost,
    failed_current_tool_observation,
    safe_stop,
)
from supportguard.agent.obligations import (
    ActionObligationLedger,
    ContextCitationBinding,
    TerminalBusinessOutcome,
    evaluate_action_obligations,
    qualified_knowledge_evidence_ids,
)
from supportguard.agent.patterns import KNOWLEDGE_CONTEXT_REFERENCE
from supportguard.agent.policy import (
    PolicyInput,
    PolicyRoute,
    PublicationDecision,
    evaluate_policy,
)
from supportguard.agent.proposal_assembler import (
    ActionAssemblyError,
    SynthesisBindingError,
    assemble_action_candidate,
    bind_provider_synthesis,
    evaluate_action_candidate_eligibility,
    provider_synthesis_reference_contract,
)
from supportguard.agent.responses import (
    render_terminal_business_outcome,
    safe_applicability_condition_answer,
)
from supportguard.agent.schemas import (
    AgentDecision,
    BoundEvidenceSynthesis,
    CandidateResponse,
    MaterialClaim,
    ProposalEligibility,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.agent.state import AgentState
from supportguard.contracts.action_preconditions import ActionAdmission, ActionAdmissionV2
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    AgentRun,
    CitationBinding,
    ClaimRecord,
    new_id,
)
from supportguard.prompts.registry import load_prompt
from supportguard.providers.base import (
    RawProviderDecision,
    StructuredProvider,
    normalize_provider_result,
)
from supportguard.providers.deepseek import ProviderStructuredOutputError
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository


class ActionFlowHost(SafeStopHost, Protocol):
    provider: StructuredProvider
    context_assembler: ContextAssembler
    session: AsyncSession | None
    test_capability: TestRuntimeCapability | None

    def _action_state_contract_valid(self, *args: Any, **kwargs: Any) -> Any: ...
    def _authoritative_read_only_fact_contract_valid(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_grounded_conflict_clarification(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_pending_action_policy_candidate(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_publication_references(self, *args: Any, **kwargs: Any) -> Any: ...
    def _clarification_requires_knowledge_first(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _current_lease(self, *args: Any, **kwargs: Any) -> Any: ...
    def _decision_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _event(self, *args: Any, **kwargs: Any) -> Any: ...
    def _exception_transport_attempts(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _finish_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _has_active_action_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def _has_secret_redaction(self, *args: Any, **kwargs: Any) -> Any: ...
    def _message_specifies_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def _mixed_account_applicability_missing_groups(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_context_ledger(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_raw_provider_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _prepare_context_evidence_bindings(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _prepare_context_observation_memberships(self, *args: Any, **kwargs: Any) -> Any: ...
    def _project_context_observation(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_component_manifest(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_failure_error_code(self, *args: Any, **kwargs: Any) -> Any: ...
    def _render_validated_answer(self, *args: Any, **kwargs: Any) -> Any: ...
    def _requests_explicit_first_step(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trace(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trusted_platform_answer(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _PolicyContracts:
    candidate: CandidateResponse
    grounded_conflict_clarification: bool
    platform_contract: bool
    terminal_contract: bool
    action_state_contract: bool
    authoritative_current_fact_contract: bool
    authoritative_requested_current_fact_contract: bool


@dataclass(frozen=True, slots=True)
class _PolicyEvidenceContext:
    evidence_items: list[dict[str, Any]]
    allowed_chunks: set[str]
    allowed_sources: set[str]
    binding_map: dict[str, dict[str, Any]]
    candidate_binding_ids: set[str]
    bound_evidence: dict[str, dict[str, Any] | None]
    citation_ids: set[str]
    citation_bindings_valid: bool
    applicability_conditions: tuple[str, ...]
    generic_applicability_dimensions: tuple[Literal["region", "plan"], ...]
    referential_requirements: list[str]
    ordered_bound_evidence: list[tuple[str, dict[str, Any] | None]]
    comparison_citation_bindings_canonicalized: int


@dataclass(frozen=True, slots=True)
class _PolicyComparisonContext:
    candidate: CandidateResponse
    applicability_scope_claims: list[MaterialClaim]
    transition_markers: list[str]
    comparison_transition_claims: list[MaterialClaim]
    comparison_version_role_claims_canonicalized: int


@dataclass(frozen=True, slots=True)
class _PolicyIntegrityContext:
    integrity_checks: dict[str, bool]
    integrity_failure_codes: list[str]
    integrity: bool
    cited_evidence_groups: set[str]
    available_evidence_groups: set[str]
    missing_transition_markers: list[str]
    comparison_citations_complete: bool
    explainable_comparison: bool
    proposal_eligibility: ProposalEligibility
    finish_reason: str | None
    safe_stop_reason: str | None


@dataclass(frozen=True, slots=True)
class _PolicyRequirements:
    has_replan_budget: bool
    can_replan: bool
    missing_applicability_conditions: list[str]
    missing_referential_requirements: list[str]
    mixed_account_missing_groups: list[str]
    requested_current_fact_missing_groups: list[str]
    requested_current_fact_stale_groups: list[str]
    requested_action: str
    requested_action_unresolved: bool
    allowed_reject: bool


@dataclass(frozen=True, slots=True)
class _PolicyCandidateResolution:
    candidate: CandidateResponse
    integrity: bool
    finish_reason: str | None
    applicability_condition_unresolved: bool
    replan_update: AgentState | None = None


@dataclass(frozen=True, slots=True)
class _PolicyRouteOutcome:
    candidate: CandidateResponse
    decision: PublicationDecision
    evidence_assessment: EvidenceAssessment
    canonical_safe_decision: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _PolicyFreshnessOutcome:
    candidate: CandidateResponse
    decision: PublicationDecision
    evidence_assessment: EvidenceAssessment
    finish_reason: str | None
    freshness_limited_claims_removed: int
    candidate_binding_ids: set[str]


@dataclass(frozen=True, slots=True)
class _BoundSynthesisContext:
    reserved: Any
    provider_attempt_id: str
    context_ledger_id_hint: str
    evidence: list[dict[str, Any]]
    binding_plans: list[Any]
    membership_root_hash: str
    observation_membership_root_hash: str
    context_observations: list[dict[str, Any]]
    assembled: AssembledContext


@dataclass(frozen=True, slots=True)
class ActionFlowNodes:
    """Own evidence obligations, synthesis and deterministic policy orchestration."""

    host: ActionFlowHost

    @staticmethod
    def _current_evidence_decision_assessment(
        state: AgentState,
    ) -> EvidenceAssessment | None:
        payload = state.get("evidence_decision")
        if not payload:
            return None
        try:
            decision = EvidenceDecision.model_validate(payload)
        except ValidationError:
            return EvidenceAssessment(
                sufficient=False,
                required_groups=[],
                missing_groups=["valid_evidence_decision"],
                result="terminal",
                error_code="evidence_decision_invalid",
            )
        scope_matches = bool(
            decision.run_id == state.get("run_id")
            and decision.tenant_id == state.get("tenant_id")
            and decision.customer_id == state.get("customer_id")
        )
        if not scope_matches:
            return EvidenceAssessment(
                sufficient=False,
                required_groups=list(decision.requirements.required_groups),
                missing_groups=["current_evidence_decision"],
                result="terminal",
                error_code="evidence_decision_identity_mismatch",
            )
        if decision.provider_attempt_id != state.get("latest_provider_attempt_id"):
            # Action synthesis owns a later Provider attempt and has not yet
            # adopted this Decision-stage snapshot. Fall back to its existing
            # candidate-bound evidence check until that stage is migrated.
            return None
        return EvidenceAssessment(
            sufficient=decision.sufficient,
            required_groups=list(decision.requirements.required_groups),
            satisfied_groups=list(decision.satisfied_groups),
            missing_groups=list(decision.missing_groups),
            stale_groups=list(decision.stale_groups),
            result=decision.result,
            error_code=decision.error_code,
        )

    async def evaluate_obligations(self, state: AgentState) -> AgentState:
        # A deterministic stop is terminal even when the admitted action still
        # has pending obligations. Re-entering obligation collection here
        # would erase the stop candidate and create an unbounded graph cycle.
        if state.get("candidate") and state.get("safe_stop_reason"):
            return {}
        payload = state.get("action_admission")
        if not payload:
            return {}
        if payload.get("schema_version") == "action-admission.v1":
            legacy = ActionAdmission.model_validate(payload)
            current_message_id = str(state.get("customer_message_id") or "")
            turn_group_id = str(state.get("conversation_turn_id") or "")
            if self.host.session is not None:
                run = await self.host.session.get(AgentRun, state["run_id"])
                if run is not None:
                    current_message_id = run.message_id
                    turn_group_id = run.turn_id or turn_group_id
            recovery = recover_legacy_action_admission(
                legacy=legacy,
                redacted_message=str(state.get("redacted_message") or ""),
                classification=state.get("classification", {}),
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                current_message_id=current_message_id,
                turn_group_id=turn_group_id,
                observations=state.get("tool_observations", []),
                run_id=state["run_id"],
            )
            await self.host._event(
                state,
                "legacy_action_admission_recovery",
                {
                    "action_type": legacy.action_type,
                    "status": "recovered" if recovery.recovered else "failed_closed",
                    "reason_code": recovery.reason_code,
                },
                visibility="customer",
                status="completed" if recovery.recovered else "failed",
            )
            if not recovery.recovered or recovery.admission is None:
                return await safe_stop(self.host, state, recovery.reason_code)
            admission = recovery.admission
            state = cast(
                AgentState,
                {
                    **state,
                    "action_admission": admission.model_dump(mode="json"),
                },
            )
        else:
            admission = ActionAdmissionV2.model_validate(payload)
        if admission.status != "admitted" or admission.action_type is None:
            return {"action_obligation_ledger": {}}
        action_spec = get_action_spec(admission.action_type)
        bindings = [
            ContextCitationBinding.model_validate(item)
            for item in state.get("context_citation_bindings", [])
        ]
        provider_attempt_id = (
            state.get("latest_provider_attempt_id")
            if state.get("obligation_synthesis_mode")
            else None
        )
        ledger = evaluate_action_obligations(
            action_spec=action_spec,
            admission=admission,
            observations=state.get("tool_observations", []),
            run_id=state["run_id"],
            citation_bindings=bindings,
            provider_attempt_id=provider_attempt_id,
        )
        await self.host._event(
            state,
            "action_obligations_evaluated",
            {
                "action_type": ledger.action_type,
                "next_state": ledger.next_state,
                "reason_code": ledger.reason_code,
                "obligations": [
                    {
                        "obligation_id": item.obligation_id,
                        "kind": item.kind,
                        "status": item.status,
                        "reason_code": item.reason_code,
                    }
                    for item in ledger.obligations
                ],
                "unsatisfied_capabilities": list(ledger.unsatisfied_capabilities),
            },
            visibility="customer",
        )
        if ledger.terminal_outcome is not None:
            outcome = ledger.terminal_outcome
            await self.host._event(
                state,
                "terminal_business_outcome_derived",
                {
                    "action_type": outcome.action_type,
                    "terminal_class": outcome.terminal_class,
                    "outcome_code": outcome.outcome_code,
                    "outcome_hash": outcome.outcome_hash,
                    "obligation_id": outcome.obligation_id,
                    "observation_id": outcome.binding.observation_id,
                    "source_count": len(outcome.binding.source_ids),
                },
                visibility="customer",
            )
        update: AgentState = {
            "action_admission": admission.model_dump(mode="json"),
            "action_obligation_ledger": ledger.model_dump(mode="json"),
        }
        if ledger.next_state in {"collect_reads", "synthesize"}:
            update.update(
                {
                    "candidate": {},
                    "agent_finish_reason": "",
                    "evidence_replan_required": False,
                }
            )
        if ledger.next_state == "safe_stop":
            update.update(
                await safe_stop(
                    self.host,
                    cast(AgentState, {**state, **update}),
                    ledger.reason_code,
                )
            )
        return update

    @staticmethod
    def route_obligations(state: AgentState) -> str:
        if state.get("candidate") and state.get("safe_stop_reason"):
            return "policy"
        payload = state.get("action_admission")
        if not payload:
            return "decide"
        admission = ActionAdmissionV2.model_validate(payload)
        if admission.status != "admitted":
            return "decide"
        ledger_payload = state.get("action_obligation_ledger")
        if not ledger_payload:
            return "decide"
        ledger = ActionObligationLedger.model_validate(ledger_payload)
        return {
            "collect_reads": "decide",
            "synthesize": "synthesize",
            "assemble_candidate": "assemble",
            "clarify": "decide",
            "safe_stop": "policy",
            "explain_terminal": "terminal",
        }[ledger.next_state]

    async def explain_terminal_business_outcome(
        self,
        state: AgentState,
    ) -> AgentState:
        """Project a validated business terminal without asking the model to guess."""

        ledger = ActionObligationLedger.model_validate(state.get("action_obligation_ledger") or {})
        outcome = ledger.terminal_outcome
        if ledger.next_state != "explain_terminal" or outcome is None:
            return await safe_stop(
                self.host,
                state,
                "terminal_business_outcome_missing",
            )
        rendering = render_terminal_business_outcome(outcome)
        source_ids = list(outcome.binding.source_ids)
        candidate = CandidateResponse(
            answer=rendering.answer,
            action="answer",
            knowledge_chunk_ids=[],
            business_source_ids=source_ids,
            # This answer is Runtime-derived, not Provider-generated. Its
            # Terminal Outcome and Observation binding are persisted directly
            # instead of fabricating Provider/Context claim lineage.
            material_claims=[],
            proposed_arguments={},
        )
        await self.host._event(
            state,
            "terminal_business_outcome_projected",
            {
                "action_type": outcome.action_type,
                "terminal_class": outcome.terminal_class,
                "outcome_code": outcome.outcome_code,
                "outcome_hash": outcome.outcome_hash,
                "obligation_id": outcome.obligation_id,
                "source_count": len(source_ids),
                "proposal_allowed": outcome.proposal_allowed,
                "approval_allowed": outcome.approval_allowed,
                "execution_allowed": outcome.execution_allowed,
            },
            visibility="customer",
        )
        return {
            "terminal_business_outcome": outcome.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "agent_finish_reason": "terminal_business_outcome",
            "policy_route": PolicyRoute.ANSWER.value,
            "validated_answer": candidate.answer,
            "obligation_synthesis_mode": False,
            "evidence_replan_required": False,
        }

    async def bind_evidence_and_synthesize(self, state: AgentState) -> AgentState:
        if state["llm_calls"] >= MAX_LLM_CALLS:
            return await safe_stop(
                self.host,
                state,
                "action_synthesis_budget_exhausted",
            )
        repair_used = False
        try:
            (
                synthesis,
                evidence,
                provider_attempt_id,
                context_ledger_id,
            ) = await self._call_bound_evidence_synthesis(state)
            calls = state["llm_calls"] + 1
        except Exception as first_error:
            can_repair = (
                isinstance(first_error, ProviderStructuredOutputError)
                and not state.get("structure_repair_used")
                and state["llm_calls"] + 2 <= MAX_LLM_CALLS
            )
            if not can_repair:
                stopped = await safe_stop(
                    self.host,
                    state,
                    (
                        "provider_terminal_schema_invalid"
                        if isinstance(first_error, ProviderStructuredOutputError)
                        else "provider_failed"
                    ),
                    error_code=(
                        "provider_terminal_schema_invalid"
                        if isinstance(first_error, ProviderStructuredOutputError)
                        else self.host._provider_failure_error_code(first_error)
                    ),
                )
                stopped["llm_calls"] = state["llm_calls"] + 1
                stopped["obligation_synthesis_mode"] = True
                return stopped
            repair_used = True
            try:
                (
                    synthesis,
                    evidence,
                    provider_attempt_id,
                    context_ledger_id,
                ) = await self._call_bound_evidence_synthesis(
                    state,
                    repair_of_attempt_id=str(
                        getattr(
                            first_error,
                            "supportguard_provider_attempt_id",
                            "bound-synthesis-primary",
                        )
                    ),
                    error_paths=self.host._decision_error_paths(first_error),
                )
                calls = state["llm_calls"] + 2
            except Exception as repair_error:
                stopped = await safe_stop(
                    self.host,
                    state,
                    (
                        "provider_terminal_schema_invalid"
                        if isinstance(repair_error, ProviderStructuredOutputError)
                        else "provider_failed"
                    ),
                    error_code=(
                        "provider_terminal_schema_invalid"
                        if isinstance(repair_error, ProviderStructuredOutputError)
                        else self.host._provider_failure_error_code(repair_error)
                    ),
                )
                stopped["llm_calls"] = state["llm_calls"] + 2
                stopped["structure_repair_used"] = True
                stopped["obligation_synthesis_mode"] = True
                return stopped
        redundant_claim_source_bindings_removed = self.host._canonicalize_publication_references(
            synthesis,
            evidence,
        )
        used_binding_ids = {
            binding_id
            for claim in synthesis.material_claims
            for binding_id in claim.citation_binding_ids
        }
        context_bindings = [
            {
                "citation_binding_id": str(item["citation_binding_id"]),
                "provider_attempt_id": provider_attempt_id,
                "evidence_id": str(item.get("evidence_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "content_hash": str(item.get("content_hash") or ""),
                "locator_hash": str(item.get("source_locator_hash") or ""),
            }
            for item in evidence
            if item.get("citation_binding_id") in used_binding_ids
        ]
        publication_evidence_ids = {str(item.get("evidence_id") or "") for item in evidence}
        publication_evidence = [
            item
            for item in state.get("evidence", [])
            if str(item.get("evidence_id") or "") in publication_evidence_ids
        ]
        await self.host._event(
            state,
            "evidence_synthesized",
            {
                "schema_version": synthesis.schema_version,
                "provider_attempt_id": provider_attempt_id,
                "context_ledger_id": context_ledger_id,
                "tool_surface": [],
                "material_claim_count": len(synthesis.material_claims),
                "citation_binding_count": len(context_bindings),
                "redundant_claim_source_bindings_removed": (
                    redundant_claim_source_bindings_removed
                ),
            },
            visibility="customer",
        )
        return {
            "candidate": synthesis.model_dump(mode="json"),
            # Preserve the rich retrieval records (including SourceLocator)
            # while narrowing them to the exact provider-context membership.
            # The independent publication validator projects these same rows
            # again and compares their hashes with durable memberships.
            "evidence": publication_evidence,
            "context_citation_bindings": context_bindings,
            "citation_binding_map": {
                str(item["citation_binding_id"]): {
                    "evidence_id": item.get("evidence_id"),
                    "evidence_group": item.get("evidence_group"),
                    "chunk_id": item.get("chunk_id"),
                    "document_id": item.get("document_id"),
                    "version": item.get("version"),
                    "content_hash": item.get("content_hash"),
                    "locator_hash": item.get("source_locator_hash"),
                }
                for item in evidence
            },
            "latest_provider_attempt_id": provider_attempt_id,
            "latest_context_ledger_id": context_ledger_id or "",
            "llm_calls": calls,
            "step_index": state.get("step_index", 0) + 1,
            "structure_repair_used": bool(state.get("structure_repair_used", False) or repair_used),
            "agent_finish_reason": "evidence_synthesized",
            "obligation_synthesis_mode": True,
        }

    async def _prepare_bound_synthesis_context(
        self,
        state: AgentState,
        *,
        repair_of_attempt_id: str | None,
    ) -> _BoundSynthesisContext:
        call_kind = "structure_repair" if repair_of_attempt_id else "llm"
        reserved = await self.host._reserve_external(
            state,
            call_kind,
            repair_of_attempt_id=repair_of_attempt_id,
        )
        provider_attempt_id = reserved[1].id if reserved is not None else new_id("attempt")
        context_ledger_id_hint = new_id("context")
        ledger = ActionObligationLedger.model_validate(state.get("action_obligation_ledger", {}))
        qualified_evidence_ids = set(qualified_knowledge_evidence_ids(ledger))
        evidence_lineage = [
            item
            for item in state.get("evidence", [])
            if item.get("evidence_id") in qualified_evidence_ids
        ]
        if not evidence_lineage:
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code="bound_synthesis_qualified_evidence_missing",
            )
            raise RuntimeConflict("bound_synthesis_qualified_evidence_missing")
        observation_lineage = [
            observation
            for observation in state.get("tool_observations", [])
            if observation.get("status") == "ok"
        ]
        context_observations = [
            self.host._project_context_observation(observation)
            for observation in observation_lineage
        ]
        try:
            (
                evidence,
                binding_plans,
                membership_root_hash,
            ) = await self.host._prepare_context_evidence_bindings(
                state,
                evidence_lineage,
                provider_attempt_id=provider_attempt_id,
                context_ledger_id=context_ledger_id_hint,
            )
            (
                observation_plans,
                observation_root_hash,
            ) = await self.host._prepare_context_observation_memberships(
                state,
                observation_lineage,
                context_observations,
                provider_attempt_id=provider_attempt_id,
                context_ledger_id=context_ledger_id_hint,
                payload_ordinal_offset=len(evidence_lineage),
            )
            binding_plans.extend(observation_plans)
            assembled = self.host.context_assembler.assemble(
                run_id=state["run_id"],
                step_index=state.get("step_index", 0) + 1,
                user_goal=state["redacted_message"],
                trusted_task_state=build_trusted_task_state(state),
                tools=[],
                latest_observations=context_observations,
                evidence=evidence,
                evidence_lineage=evidence_lineage,
                history=state.get("relevant_history", []),
                remaining_budget={
                    "llm_calls": max(
                        0,
                        MAX_LLM_CALLS - state["llm_calls"] - (1 if repair_of_attempt_id else 0),
                    ),
                    "tool_rounds": MAX_TOOL_ROUNDS - state["tool_rounds"],
                    "tool_attempts": MAX_TOOL_ATTEMPTS - state["tool_attempts"],
                },
                prior_turns=state.get("provider_turns", []),
            )
        except (ContextBudgetExceeded, RuntimeConflict) as exc:
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code=f"bound_synthesis_context:{type(exc).__name__}",
            )
            raise
        return _BoundSynthesisContext(
            reserved=reserved,
            provider_attempt_id=provider_attempt_id,
            context_ledger_id_hint=context_ledger_id_hint,
            evidence=evidence,
            binding_plans=binding_plans,
            membership_root_hash=membership_root_hash,
            observation_membership_root_hash=observation_root_hash,
            context_observations=context_observations,
            assembled=assembled,
        )

    async def _call_bound_evidence_synthesis(
        self,
        state: AgentState,
        *,
        repair_of_attempt_id: str | None = None,
        error_paths: list[str] | None = None,
    ) -> tuple[BoundEvidenceSynthesis, list[dict[str, Any]], str, str | None]:
        """Run one authority-free synthesis attempt with an explicitly empty tool surface."""

        attempt = await self._prepare_bound_synthesis_context(
            state,
            repair_of_attempt_id=repair_of_attempt_id,
        )
        reserved = attempt.reserved
        provider_attempt_id = attempt.provider_attempt_id
        context_ledger_id_hint = attempt.context_ledger_id_hint
        evidence = attempt.evidence
        binding_plans = attempt.binding_plans
        membership_root_hash = attempt.membership_root_hash
        observation_membership_root_hash = attempt.observation_membership_root_hash
        context_observations = attempt.context_observations
        assembled = attempt.assembled
        prompt = load_prompt("bound_evidence_synthesis", version="v1")
        user_payload = (
            assembled.content
            if not error_paths
            else json.dumps(
                {
                    "error_paths": error_paths,
                    "reference_contract": provider_synthesis_reference_contract(
                        evidence=evidence,
                        observations=context_observations,
                    ),
                    "same_redacted_context": assembled.content,
                },
                ensure_ascii=False,
            )
        )
        try:
            result = normalize_provider_result(
                await self.host.provider.generate(
                    system=prompt.content,
                    user=user_payload,
                    output_schema=ProviderBoundEvidenceSynthesis,
                    trace_metadata={
                        **self.host._trace(prompt.content_hash, state),
                        "synthesis_schema": "bound-evidence-synthesis.v1",
                    },
                )
            )
            try:
                synthesis = bind_provider_synthesis(
                    synthesis=result.output,
                    evidence=evidence,
                    observations=context_observations,
                )
            except SynthesisBindingError as binding_error:
                if result.transport is None:
                    raise RuntimeConflict("provider_transport_missing") from binding_error
                raise ProviderStructuredOutputError(
                    error_paths=binding_error.error_paths,
                    transport=result.transport,
                    usage=result.usage,
                    transport_attempts=result.transport_attempts,
                ) from binding_error
        except Exception as exc:
            transport = exc.transport if isinstance(exc, ProviderStructuredOutputError) else None
            structured_error_paths = (
                self.host._decision_error_paths(exc)
                if isinstance(exc, ProviderStructuredOutputError)
                else []
            )
            await self.host._persist_context_ledger(
                state,
                reserved,
                component_manifest={
                    **self.host._provider_component_manifest(
                        assembled,
                        tools=[],
                        node=(
                            "bound_evidence_synthesis_repair"
                            if repair_of_attempt_id
                            else "bound_evidence_synthesis"
                        ),
                    ),
                    "ordered_membership_root_hash": membership_root_hash,
                    "observation_membership_root_hash": observation_membership_root_hash,
                    "repair_of_attempt_id": repair_of_attempt_id,
                    "error_paths": structured_error_paths,
                },
                transport=transport,
                require_capture=False,
                ledger_id=context_ledger_id_hint,
                binding_plans=binding_plans if transport is not None else None,
            )
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code=(
                    "provider_terminal_schema_invalid"
                    if isinstance(exc, ProviderStructuredOutputError)
                    else self.host._provider_failure_error_code(exc)
                ),
                prompt_tokens=(
                    exc.usage.prompt_tokens if isinstance(exc, ProviderStructuredOutputError) else 0
                ),
                completion_tokens=(
                    exc.usage.completion_tokens
                    if isinstance(exc, ProviderStructuredOutputError)
                    else 0
                ),
                provider_transport_attempts=self.host._exception_transport_attempts(exc),
                structured_error_paths=(structured_error_paths or None),
            )
            exc.supportguard_provider_attempt_id = provider_attempt_id  # type: ignore[attr-defined]
            raise
        context_ledger_id = await self.host._persist_context_ledger(
            state,
            reserved,
            component_manifest={
                **self.host._provider_component_manifest(
                    assembled,
                    tools=[],
                    node=(
                        "bound_evidence_synthesis_repair"
                        if repair_of_attempt_id
                        else "bound_evidence_synthesis"
                    ),
                ),
                "ordered_membership_root_hash": membership_root_hash,
                "observation_membership_root_hash": observation_membership_root_hash,
                "repair_of_attempt_id": repair_of_attempt_id,
            },
            transport=result.transport,
            ledger_id=context_ledger_id_hint,
            binding_plans=binding_plans,
        )
        await self.host._persist_raw_provider_decision(
            state,
            reserved,
            RawProviderDecision(
                finish_reason="stop",
                content=result.output.model_dump_json(),
                tool_calls=(),
            ),
        )
        await self.host._finish_external(
            reserved,
            status="succeeded",
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            provider_transport_attempts=result.transport_attempts,
        )
        return (
            synthesis,
            evidence,
            provider_attempt_id,
            context_ledger_id,
        )

    async def assemble_action(self, state: AgentState) -> AgentState:
        if state.get("candidate") and state.get("safe_stop_reason"):
            return {}
        payload = state.get("action_admission")
        synthesis_payload = state.get("candidate")
        if not payload or not synthesis_payload:
            return await safe_stop(self.host, state, "action_synthesis_missing")
        admission = ActionAdmissionV2.model_validate(payload)
        if admission.status != "admitted" or admission.action_type is None:
            return await safe_stop(self.host, state, "action_admission_not_admitted")
        action_spec = get_action_spec(admission.action_type)
        try:
            ledger = evaluate_action_obligations(
                action_spec=action_spec,
                admission=admission,
                observations=state.get("tool_observations", []),
                run_id=state["run_id"],
                citation_bindings=[
                    ContextCitationBinding.model_validate(item)
                    for item in state.get("context_citation_bindings", [])
                ],
                provider_attempt_id=state.get("latest_provider_attempt_id"),
            )
            candidate = assemble_action_candidate(
                action_spec=action_spec,
                admission=admission,
                ledger=ledger,
                observations=state.get("tool_observations", []),
                synthesis=BoundEvidenceSynthesis.model_validate(synthesis_payload),
            )
        except (ActionAssemblyError, ValidationError, ValueError) as exc:
            return await safe_stop(
                self.host,
                state,
                "action_candidate_assembly_failed",
                error_code=str(exc)[:100],
            )
        await self.host._event(
            state,
            "action_candidate_assembled",
            {
                "action_type": admission.action_type,
                "proposal_action": candidate.action,
                "obligation_statuses": {
                    item.obligation_id: item.status for item in ledger.obligations
                },
            },
            visibility="customer",
        )
        return {
            "action_obligation_ledger": ledger.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "agent_finish_reason": "action_candidate_assembled",
            "obligation_synthesis_mode": False,
        }

    def _prepare_policy_contracts(self, state: AgentState) -> _PolicyContracts:
        candidate = CandidateResponse.model_validate(state["candidate"])
        grounded_candidate = self.host._canonicalize_grounded_conflict_clarification(
            state, candidate
        )
        grounded_conflict_clarification = grounded_candidate is not candidate
        candidate = self.host._canonicalize_pending_action_policy_candidate(
            state, grounded_candidate
        )
        platform_contract = (
            self.host._trusted_platform_answer(
                str(state.get("classification", {}).get("support_subject", "customer_problem"))
            )
            is not None
        )
        return _PolicyContracts(
            candidate=candidate,
            grounded_conflict_clarification=grounded_conflict_clarification,
            platform_contract=platform_contract,
            terminal_contract=self._terminal_business_contract_valid(state, candidate),
            action_state_contract=self.host._action_state_contract_valid(state, candidate),
            authoritative_current_fact_contract=(
                self.host._authoritative_read_only_fact_contract_valid(state, candidate)
            ),
            authoritative_requested_current_fact_contract=requested_current_fact_contract_valid(
                state, candidate
            ),
        )

    @staticmethod
    def _evidence_for_binding(
        binding_id: str,
        *,
        binding_map: dict[str, dict[str, Any]],
        evidence_items: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve one binding by its exact supporting-span identity."""

        details = binding_map.get(binding_id)
        if not isinstance(details, dict):
            return None
        for item in evidence_items:
            if (
                item.get("supporting_span_eligible") is True
                and item.get("chunk_id") == details.get("chunk_id")
                and item.get("document_id") == details.get("document_id")
                and item.get("version") == details.get("version")
                and item.get("content_hash") == details.get("content_hash")
                and item.get("source_locator", {}).get("locator_hash")
                == details.get("locator_hash")
                and (
                    not details.get("evidence_id")
                    or item.get("evidence_id") == details.get("evidence_id")
                )
                and (
                    not details.get("evidence_group")
                    or (item.get("evidence_group") or "current") == details.get("evidence_group")
                )
            ):
                return item
        return None

    def _prepare_policy_evidence_context(
        self,
        state: AgentState,
        candidate: CandidateResponse,
    ) -> tuple[CandidateResponse, _PolicyEvidenceContext]:
        evidence_items = [item for item in state.get("evidence", []) if item.get("chunk_id")]
        allowed_chunks = {str(item["chunk_id"]) for item in evidence_items}
        allowed_sources = {
            str(source["source_id"])
            for observation in state.get("tool_observations", [])
            if observation.get("tool_name") != "search_knowledge"
            for source in observation.get("source_refs", [])
        }
        binding_map = state.get("citation_binding_map", {})
        candidate, canonicalized = canonicalize_comparison_citation_groups(
            candidate,
            evidence=evidence_items,
            binding_map=binding_map,
            comparison_complete=bool(state.get("knowledge_comparison_complete", False)),
            evidence_replan_count=int(state.get("evidence_replan_count", 0)),
        )
        candidate_binding_ids = {item.citation_binding_id for item in candidate.knowledge_citations}
        bound_evidence = {
            binding_id: self._evidence_for_binding(
                binding_id,
                binding_map=binding_map,
                evidence_items=evidence_items,
            )
            for binding_id in candidate_binding_ids
        }
        citation_ids = {
            str(binding_map[binding_id].get("chunk_id"))
            for binding_id in candidate_binding_ids
            if binding_id in binding_map and binding_map[binding_id].get("chunk_id")
        }
        citation_bindings_valid = candidate_binding_ids <= set(binding_map) and all(
            bound_evidence[binding_id] is not None for binding_id in candidate_binding_ids
        )
        classification = state.get("classification", {})
        current_message = str(state.get("redacted_message", "")).strip()
        applicability_conditions = explicit_applicability_conditions(
            current_message,
            issue_type=str(classification.get("issue_type", "unknown")),
            policy_boundary=str(classification.get("policy_boundary", "allowed")),
            requested_action=str(classification.get("requested_action", "none")),
        )
        generic_dimensions = requested_generic_applicability_dimensions(
            current_message,
            issue_type=str(classification.get("issue_type", "unknown")),
            policy_boundary=str(classification.get("policy_boundary", "allowed")),
            requested_action=str(classification.get("requested_action", "none")),
        )
        reference_contract = (
            referential_applicability_contract(
                previous_assistant_answer=latest_assistant_history_message(state),
                evidence=evidence_items,
            )
            if applicability_conditions and KNOWLEDGE_CONTEXT_REFERENCE.search(current_message)
            else None
        )
        referential_requirements = (
            reference_contract.required_facets if reference_contract is not None else []
        )
        ordered_bound_evidence = [
            (item.citation_binding_id, bound_evidence.get(item.citation_binding_id))
            for item in candidate.knowledge_citations
            if item.citation_binding_id in candidate_binding_ids
        ]
        return candidate, _PolicyEvidenceContext(
            evidence_items=evidence_items,
            allowed_chunks=allowed_chunks,
            allowed_sources=allowed_sources,
            binding_map=binding_map,
            candidate_binding_ids=candidate_binding_ids,
            bound_evidence=bound_evidence,
            citation_ids=citation_ids,
            citation_bindings_valid=citation_bindings_valid,
            applicability_conditions=applicability_conditions,
            generic_applicability_dimensions=generic_dimensions,
            referential_requirements=referential_requirements,
            ordered_bound_evidence=ordered_bound_evidence,
            comparison_citation_bindings_canonicalized=canonicalized,
        )

    def _canonicalize_policy_applicability(
        self,
        candidate: CandidateResponse,
        evidence: _PolicyEvidenceContext,
    ) -> tuple[CandidateResponse, list[MaterialClaim]]:
        scope_claims: list[MaterialClaim] = []
        if (
            candidate.action == "answer"
            and evidence.candidate_binding_ids
            and evidence.citation_bindings_valid
            and evidence.applicability_conditions
        ):
            existing_claim_text = "\n".join(
                claim.text for claim in candidate.material_claims
            ).casefold()
            for condition in evidence.applicability_conditions:
                scope_candidates: list[tuple[int, str, dict[str, Any], list[str]]] = []
                for binding_id, item in evidence.ordered_bound_evidence:
                    if item is None:
                        continue
                    facets = supported_referential_facets([item], evidence.referential_requirements)
                    scope_candidates.append((len(facets), binding_id, item, facets))
                scope_candidates.sort(key=lambda item: item[0], reverse=True)
                for _, binding_id, selected, facets in scope_candidates:
                    scope_text = applicability_scope_claim(
                        condition, [selected], topic_facets=facets
                    )
                    locator_hash = str(
                        evidence.binding_map.get(binding_id, {}).get("locator_hash") or ""
                    )
                    if scope_text is None or len(locator_hash) != 64:
                        continue
                    if scope_text.casefold() not in existing_claim_text:
                        scope_claims.append(
                            MaterialClaim(
                                text=scope_text,
                                citation_binding_ids=[binding_id],
                                knowledge_locator_hashes=[locator_hash],
                            )
                        )
                    break
        if (
            candidate.action == "answer"
            and evidence.candidate_binding_ids
            and evidence.citation_bindings_valid
            and evidence.generic_applicability_dimensions
        ):
            published_text = "\n".join([candidate.answer, *(claim.text for claim in scope_claims)])
            for dimension in evidence.generic_applicability_dimensions:
                if applicability_dimension_answered(published_text, dimension):
                    continue
                for binding_id, item in evidence.ordered_bound_evidence:
                    if item is None:
                        continue
                    scope_text = generic_applicability_dimension_claim(dimension, [item])
                    locator_hash = str(
                        evidence.binding_map.get(binding_id, {}).get("locator_hash") or ""
                    )
                    if scope_text is None or len(locator_hash) != 64:
                        continue
                    scope_claims.append(
                        MaterialClaim(
                            text=scope_text,
                            citation_binding_ids=[binding_id],
                            knowledge_locator_hashes=[locator_hash],
                        )
                    )
                    published_text = "\n".join([scope_text, published_text])
                    break
        if scope_claims:
            candidate = candidate.model_copy(
                update={
                    "answer": "\n".join(
                        [*(claim.text for claim in scope_claims), candidate.answer]
                    ),
                    "material_claims": [*scope_claims, *candidate.material_claims],
                }
            )
        return candidate, scope_claims

    @staticmethod
    def _canonicalize_policy_comparison(
        state: AgentState,
        candidate: CandidateResponse,
        evidence: _PolicyEvidenceContext,
        applicability_scope_claims: list[MaterialClaim],
    ) -> _PolicyComparisonContext:
        transition_markers = comparison_transition_markers(evidence.evidence_items)
        transition_claims: list[MaterialClaim] = []
        role_claims_canonicalized = 0
        missing_markers = missing_comparison_transition_markers(candidate, transition_markers)
        roles_missing = not comparison_transition_roles_explicit(candidate, transition_markers)
        close_missing_after_replan = bool(
            missing_markers and state.get("evidence_replan_count", 0) >= 1
        )
        close_ambiguous_roles = bool(transition_markers and not missing_markers and roles_missing)
        if (
            candidate.action == "answer"
            and state.get("knowledge_comparison_complete", False)
            and evidence.candidate_binding_ids
            and evidence.citation_bindings_valid
            and (close_missing_after_replan or close_ambiguous_roles)
        ):
            transition_sources: list[tuple[str, str, str]] = []
            for citation in candidate.knowledge_citations:
                binding_id = citation.citation_binding_id
                transition_evidence = evidence.bound_evidence.get(binding_id)
                transition_text = (
                    comparison_transition_claim([transition_evidence])
                    if transition_evidence is not None
                    else None
                )
                locator_hash = str(
                    evidence.binding_map.get(binding_id, {}).get("locator_hash") or ""
                )
                if transition_text is None or len(locator_hash) != 64:
                    continue
                required_markers = transition_markers if close_ambiguous_roles else missing_markers
                compact_transition = re.sub(r"\s+", "", transition_text).casefold()
                if any(marker not in compact_transition for marker in required_markers):
                    continue
                transition_sources.append((binding_id, locator_hash, transition_text))
            if transition_sources:
                transition_text = transition_sources[0][2]
                transition_claims.append(
                    MaterialClaim(
                        text=transition_text,
                        citation_binding_ids=[item[0] for item in transition_sources],
                        knowledge_locator_hashes=list(
                            dict.fromkeys(item[1] for item in transition_sources)
                        ),
                    )
                )
                candidate = candidate.model_copy(
                    update={
                        "answer": "\n".join([transition_text, candidate.answer]),
                        "material_claims": [*transition_claims, *candidate.material_claims],
                    }
                )
                if close_ambiguous_roles:
                    role_claims_canonicalized = 1
        return _PolicyComparisonContext(
            candidate=candidate,
            applicability_scope_claims=applicability_scope_claims,
            transition_markers=transition_markers,
            comparison_transition_claims=transition_claims,
            comparison_version_role_claims_canonicalized=role_claims_canonicalized,
        )

    async def _assess_policy_integrity(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        contracts: _PolicyContracts,
        evidence: _PolicyEvidenceContext,
        comparison: _PolicyComparisonContext,
    ) -> _PolicyIntegrityContext:
        durable_bindings_valid = True
        if self.host.session is not None and self.host.test_capability is None:
            rows = list(
                (
                    await self.host.session.scalars(
                        select(CitationBinding).where(
                            CitationBinding.id.in_(evidence.candidate_binding_ids),
                            CitationBinding.tenant_id == state["tenant_id"],
                            CitationBinding.run_id == state["run_id"],
                            CitationBinding.provider_attempt_id
                            == state.get("latest_provider_attempt_id"),
                            CitationBinding.context_ledger_id
                            == state.get("latest_context_ledger_id"),
                        )
                    )
                ).all()
            )
            durable_bindings_valid = (
                len(rows) == len(evidence.candidate_binding_ids)
                and {row.id for row in rows} == evidence.candidate_binding_ids
            )
        claim_support_present = bool(candidate.material_claims) and all(
            bool(claim.knowledge_locator_hashes or claim.observation_source_ids)
            for claim in candidate.material_claims
        )
        claim_bindings_selected = all(
            set(claim.citation_binding_ids) <= evidence.candidate_binding_ids
            for claim in candidate.material_claims
        )
        claim_locators_allowed = all(
            set(claim.citation_binding_ids) <= set(evidence.binding_map)
            and all(
                self._evidence_for_binding(
                    binding_id,
                    binding_map=evidence.binding_map,
                    evidence_items=evidence.evidence_items,
                )
                is not None
                for binding_id in claim.citation_binding_ids
            )
            and set(claim.knowledge_locator_hashes)
            == {
                str(evidence.binding_map[binding_id].get("locator_hash"))
                for binding_id in claim.citation_binding_ids
                if evidence.binding_map[binding_id].get("locator_hash")
            }
            for claim in candidate.material_claims
        )
        claim_sources_allowed = all(
            set(claim.observation_source_ids) <= evidence.allowed_sources
            for claim in candidate.material_claims
        )
        trusted_terminal_contract = bool(
            contracts.platform_contract
            or contracts.terminal_contract
            or contracts.action_state_contract
            or candidate.action in {"manual_takeover", "reject"}
        )
        checks = {
            "knowledge_chunks_allowed": (
                set(candidate.knowledge_chunk_ids) <= evidence.allowed_chunks
            ),
            "citation_chunks_match": (evidence.citation_ids == set(candidate.knowledge_chunk_ids)),
            "citation_bindings_current": evidence.citation_bindings_valid,
            "citation_bindings_durable": durable_bindings_valid,
            "business_sources_allowed": (
                set(candidate.business_source_ids) <= evidence.allowed_sources
            ),
            "claim_support_present": trusted_terminal_contract or claim_support_present,
            "claim_bindings_selected": trusted_terminal_contract or claim_bindings_selected,
            "claim_locators_allowed": trusted_terminal_contract or claim_locators_allowed,
            "claim_sources_allowed": trusted_terminal_contract or claim_sources_allowed,
        }
        failure_codes = [code for code, passed in checks.items() if not passed]
        integrity = not failure_codes
        cited_groups = {
            str(item.get("evidence_group") or "current")
            for binding_id in evidence.candidate_binding_ids
            if (item := evidence.bound_evidence.get(binding_id)) is not None
            and item.get("supporting_span_eligible") is True
            and len(str(item.get("source_locator", {}).get("locator_hash") or "")) == 64
        }
        available_groups = {
            str(item.get("evidence_group") or "current")
            for item in evidence.evidence_items
            if item.get("supporting_span_eligible") is True
        }
        missing_markers = missing_comparison_transition_markers(
            candidate, comparison.transition_markers
        )
        comparison_citations_complete = bool(
            candidate.action == "answer" and integrity and {"current", "historical"} <= cited_groups
        )
        explainable_comparison = bool(
            state.get("knowledge_comparison_complete", False)
            and comparison_citations_complete
            and not missing_markers
        )
        proposal_eligibility = evaluate_action_candidate_eligibility(
            candidate=candidate,
            admission_payload=state.get("action_admission"),
            ledger_payload=state.get("action_obligation_ledger"),
            observations=state.get("tool_observations", []),
        )
        finish_reason = (
            "action_candidate_assembled"
            if candidate.action
            in {
                "refund_proposal",
                "api_key_revocation_proposal",
                "entitlement_change_proposal",
            }
            else state.get("agent_finish_reason")
        )
        return _PolicyIntegrityContext(
            integrity_checks=checks,
            integrity_failure_codes=failure_codes,
            integrity=integrity,
            cited_evidence_groups=cited_groups,
            available_evidence_groups=available_groups,
            missing_transition_markers=missing_markers,
            comparison_citations_complete=comparison_citations_complete,
            explainable_comparison=explainable_comparison,
            proposal_eligibility=proposal_eligibility,
            finish_reason=finish_reason,
            safe_stop_reason=state.get("safe_stop_reason"),
        )

    def _prepare_policy_requirements(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        contracts: _PolicyContracts,
        evidence: _PolicyEvidenceContext,
        integrity: _PolicyIntegrityContext,
    ) -> _PolicyRequirements:
        requested_reads_complete = requested_current_fact_reads_complete(state)
        has_replan_budget = (
            state.get("evidence_replan_count", 0) < 1
            and state["llm_calls"] < MAX_LLM_CALLS
            and (
                (
                    state["tool_rounds"] < MAX_TOOL_ROUNDS
                    and state["tool_attempts"] < MAX_TOOL_ATTEMPTS
                )
                or requested_reads_complete
            )
        )
        can_replan = bool(
            not integrity.safe_stop_reason
            and integrity.finish_reason != "needs_clarification"
            and has_replan_budget
        )
        public_text = candidate_public_claim_text(candidate).casefold()
        missing_applicability = [
            condition
            for condition in evidence.applicability_conditions
            if condition.casefold() not in public_text
        ]
        mixed_account_missing = self.host._mixed_account_applicability_missing_groups(
            state, candidate
        )
        current_missing, current_stale = requested_current_fact_status(state, candidate)
        requested_action = str(state.get("classification", {}).get("requested_action", "none"))
        expected_candidate = {
            "refund": "refund_proposal",
            "api_key_revocation": "api_key_revocation_proposal",
            "entitlement_change": "entitlement_change_proposal",
        }.get(requested_action)
        action_unresolved = bool(
            expected_candidate
            and candidate.action != expected_candidate
            and integrity.finish_reason != "needs_clarification"
            and not state.get("evidence_conflict", False)
            and not self.host._has_active_action_context(state)
            and not contracts.terminal_contract
            and not contracts.action_state_contract
        )
        allowed_reject = bool(
            state.get("classification", {}).get("policy_boundary", "allowed") == "allowed"
            and candidate.action == "reject"
            and not contracts.platform_contract
        )
        return _PolicyRequirements(
            has_replan_budget=has_replan_budget,
            can_replan=can_replan,
            missing_applicability_conditions=missing_applicability,
            missing_referential_requirements=missing_referential_applicability_requirements(
                candidate, evidence.referential_requirements
            ),
            mixed_account_missing_groups=mixed_account_missing,
            requested_current_fact_missing_groups=current_missing,
            requested_current_fact_stale_groups=current_stale,
            requested_action=requested_action,
            requested_action_unresolved=action_unresolved,
            allowed_reject=allowed_reject,
        )

    async def _policy_replan_update(
        self,
        state: AgentState,
        assessment: EvidenceAssessment,
    ) -> AgentState:
        await self.host._event(
            state,
            "evidence_group_incomplete",
            assessment.model_dump(mode="json"),
            visibility="customer",
        )
        return {
            "candidate": {},
            "agent_finish_reason": "",
            "policy_route": "replan",
            "evidence_assessment": assessment.model_dump(mode="json"),
            "evidence_replan_required": True,
            "evidence_replan_count": state.get("evidence_replan_count", 0) + 1,
        }

    async def _required_policy_replan(
        self,
        state: AgentState,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
    ) -> AgentState | None:
        assessment: EvidenceAssessment | None = None
        if (
            integrity.finish_reason == "needs_clarification"
            and requirements.has_replan_budget
            and self.host._clarification_requires_knowledge_first(state)
        ):
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=["knowledge"],
                missing_groups=["knowledge"],
                result="replan",
                error_code="conflict_evidence_required",
            )
        elif (
            state.get("knowledge_comparison_requested", False)
            and not state.get("knowledge_comparison_complete", False)
            and requirements.has_replan_budget
        ):
            missing_groups = sorted({"current", "historical"} - integrity.available_evidence_groups)
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=["current", "historical"],
                missing_groups=missing_groups or ["complete_comparison_observation"],
                result="replan",
                error_code="comparison_evidence_incomplete",
            )
        elif (
            state.get("knowledge_comparison_complete", False)
            and not integrity.explainable_comparison
            and requirements.has_replan_budget
        ):
            missing_groups = sorted({"current", "historical"} - integrity.cited_evidence_groups)
            transition_incomplete = bool(
                integrity.comparison_citations_complete and integrity.missing_transition_markers
            )
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=["current", "historical"],
                missing_groups=(
                    ["material_comparison_transition"]
                    if transition_incomplete
                    else missing_groups or ["valid_comparison_citations"]
                ),
                result="replan",
                error_code=(
                    "comparison_transition_incomplete"
                    if transition_incomplete
                    else "comparison_citation_incomplete"
                ),
            )
        elif requirements.requested_current_fact_missing_groups and requirements.can_replan:
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=list(requested_current_fact_requirements(state)),
                missing_groups=requirements.requested_current_fact_missing_groups,
                stale_groups=requirements.requested_current_fact_stale_groups,
                result="replan",
                error_code="explicit_current_fact_incomplete",
            )
        elif requirements.mixed_account_missing_groups and requirements.can_replan:
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=["knowledge_claim", "current_account_claim"],
                missing_groups=requirements.mixed_account_missing_groups,
                result="replan",
                error_code="mixed_account_applicability_incomplete",
            )
        return (
            await self._policy_replan_update(state, assessment) if assessment is not None else None
        )

    async def _resolve_policy_candidate(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        evidence: _PolicyEvidenceContext,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
    ) -> _PolicyCandidateResolution:
        applicability_unresolved = False
        finish_reason = integrity.finish_reason
        citation_integrity = integrity.integrity
        missing_applicability = requirements.missing_applicability_conditions
        missing_referential = requirements.missing_referential_requirements
        if (
            (missing_applicability or missing_referential)
            and usable_current_knowledge_observation(state) is not None
            and candidate.action == "answer"
        ):
            if requirements.can_replan:
                assessment = EvidenceAssessment(
                    sufficient=False,
                    required_groups=(
                        [
                            f"applicability:{condition}"
                            for condition in evidence.applicability_conditions
                        ]
                        + [f"topic_facet:{facet}" for facet in evidence.referential_requirements]
                    ),
                    missing_groups=(
                        [f"applicability:{item}" for item in missing_applicability]
                        + missing_referential
                    ),
                    result="replan",
                    error_code=(
                        "referential_applicability_incomplete"
                        if missing_referential
                        else "applicability_condition_omitted"
                    ),
                )
                return _PolicyCandidateResolution(
                    candidate=candidate,
                    integrity=citation_integrity,
                    finish_reason=finish_reason,
                    applicability_condition_unresolved=False,
                    replan_update=await self._policy_replan_update(state, assessment),
                )
            candidate = CandidateResponse(
                answer=safe_applicability_condition_answer(list(evidence.applicability_conditions)),
                action="answer",
                knowledge_chunk_ids=[],
                knowledge_citations=[],
                business_source_ids=[],
                material_claims=[],
                proposed_arguments={},
            )
            citation_integrity = True
            finish_reason = "applicability_condition_unresolved"
            applicability_unresolved = True
        if requirements.can_replan and (
            requirements.requested_action_unresolved or requirements.allowed_reject
        ):
            missing_group = (
                {
                    "refund": "billing_record",
                    "api_key_revocation": "api_key_metadata",
                    "entitlement_change": "subscription",
                }.get(requirements.requested_action, "supported_answer")
                if requirements.requested_action_unresolved
                else "supported_answer"
            )
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=[missing_group],
                missing_groups=[missing_group],
                result="replan",
                error_code=(
                    "requested_action_unresolved"
                    if requirements.requested_action_unresolved
                    else "allowed_request_rejected"
                ),
            )
            return _PolicyCandidateResolution(
                candidate=candidate,
                integrity=citation_integrity,
                finish_reason=finish_reason,
                applicability_condition_unresolved=applicability_unresolved,
                replan_update=await self._policy_replan_update(state, assessment),
            )
        return _PolicyCandidateResolution(
            candidate=candidate,
            integrity=citation_integrity,
            finish_reason=finish_reason,
            applicability_condition_unresolved=applicability_unresolved,
        )

    async def _terminal_policy_evidence(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        contracts: _PolicyContracts,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
        resolution: _PolicyCandidateResolution,
    ) -> tuple[EvidenceAssessment, AgentState | None]:
        if (
            requirements.requested_current_fact_missing_groups
            or requirements.requested_current_fact_stale_groups
        ):
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=list(requested_current_fact_requirements(state)),
                missing_groups=requirements.requested_current_fact_missing_groups,
                stale_groups=requirements.requested_current_fact_stale_groups,
                result="terminal",
                error_code=(
                    "explicit_current_fact_incomplete"
                    if requirements.requested_current_fact_missing_groups
                    else "evidence_freshness_insufficient"
                ),
            )
        elif requirements.mixed_account_missing_groups:
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=["knowledge_claim", "current_account_claim"],
                missing_groups=requirements.mixed_account_missing_groups,
                result="terminal",
                error_code="mixed_account_applicability_incomplete",
            )
        elif (
            resolution.finish_reason == "needs_clarification"
            or integrity.safe_stop_reason
            or contracts.platform_contract
            or contracts.terminal_contract
            or contracts.action_state_contract
            or contracts.authoritative_current_fact_contract
            or contracts.authoritative_requested_current_fact_contract
            or resolution.applicability_condition_unresolved
            or candidate.action in {"reject", "manual_takeover", "escalate"}
        ):
            assessment = EvidenceAssessment(sufficient=True, result="accept")
        else:
            current_assessment = self._current_evidence_decision_assessment(state)
            if current_assessment is None:
                assessment = assess_terminal_evidence(
                    issue_type=str(state.get("classification", {}).get("issue_type", "unknown")),
                    candidate=candidate,
                    observations=state.get("tool_observations", []),
                    evidence_conflict=bool(state.get("evidence_conflict", False)),
                    specified_request=self.host._message_specifies_request(
                        state["redacted_message"]
                    ),
                    can_replan=requirements.can_replan,
                    explainable_comparison=integrity.explainable_comparison,
                )
            else:
                assessment = current_assessment
        replan = (
            await self._policy_replan_update(state, assessment)
            if assessment.result == "replan"
            else None
        )
        return assessment, replan

    def _apply_pure_policy(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        contracts: _PolicyContracts,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
        resolution: _PolicyCandidateResolution,
        evidence_assessment: EvidenceAssessment,
    ) -> _PolicyRouteOutcome:
        policy_boundary = str(state.get("classification", {}).get("policy_boundary", "allowed"))
        decision = evaluate_policy(
            PolicyInput(
                candidate=candidate,
                evidence_conflict=bool(state.get("evidence_conflict", False)),
                citation_integrity=resolution.integrity,
                proposal_eligible=(
                    integrity.proposal_eligibility.eligible
                    if integrity.proposal_eligibility.action_type is not None
                    else None
                ),
                finish_reason=resolution.finish_reason,
                safe_stop_reason=integrity.safe_stop_reason,
                requested_action_unresolved=requirements.requested_action_unresolved,
                evidence_assessment_result=evidence_assessment.result,
                evidence_assessment_error_code=evidence_assessment.error_code,
                has_secret_redaction=bool(self.host._has_secret_redaction(state)),
                policy_boundary=policy_boundary,
                knowledge_comparison_requested=bool(
                    state.get("knowledge_comparison_requested", False)
                ),
                knowledge_comparison_complete=bool(
                    state.get("knowledge_comparison_complete", False)
                ),
                explainable_comparison=integrity.explainable_comparison,
                comparison_citations_complete=integrity.comparison_citations_complete,
                missing_transition_markers=tuple(integrity.missing_transition_markers),
                grounded_conflict_clarification=(contracts.grounded_conflict_clarification),
                requested_current_fact_missing=bool(
                    requirements.requested_current_fact_missing_groups
                ),
                mixed_account_applicability_missing=bool(requirements.mixed_account_missing_groups),
            )
        )
        canonical_safe_decision: dict[str, Any] | None = None
        if decision.unsafe_terminal_reason is not None:
            canonical_safe_decision = AgentDecision(
                decision_type="final_candidate",
                decision_summary="Runtime applied the supported no-handoff terminal boundary.",
                candidate=decision.candidate,
            ).model_dump(mode="json")
            if decision.unsafe_terminal_reason in {
                "comparison_citation_incomplete",
                "comparison_transition_incomplete",
            }:
                evidence_assessment = EvidenceAssessment(
                    sufficient=False,
                    required_groups=["current", "historical"],
                    missing_groups=(
                        ["material_comparison_transition"]
                        if decision.unsafe_terminal_reason == "comparison_transition_incomplete"
                        else sorted({"current", "historical"} - integrity.cited_evidence_groups)
                        or ["valid_comparison_citations"]
                    ),
                    result="terminal",
                    error_code=decision.unsafe_terminal_reason,
                )
        return _PolicyRouteOutcome(
            candidate=decision.candidate,
            decision=decision,
            evidence_assessment=evidence_assessment,
            canonical_safe_decision=canonical_safe_decision,
        )

    def _apply_policy_freshness(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        decision: PublicationDecision,
        evidence: _PolicyEvidenceContext,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
        evidence_assessment: EvidenceAssessment,
    ) -> _PolicyFreshnessOutcome:
        removed = 0
        candidate_binding_ids = evidence.candidate_binding_ids
        if decision.finish_reason == "evidence_freshness_insufficient":
            candidate, removed = prune_stale_business_claims(
                candidate,
                observations=state.get("tool_observations", []),
                citation_binding_map=evidence.binding_map,
            )
            decision = replace(
                decision,
                candidate_json=candidate.model_dump_json(),
            )
            candidate_binding_ids = {
                item.citation_binding_id for item in candidate.knowledge_citations
            }
            if (
                removed
                and state.get("classification", {}).get("needs_realtime_facts") is False
                and not requirements.requested_current_fact_missing_groups
                and not requirements.requested_current_fact_stale_groups
            ):
                reassessed = assess_terminal_evidence(
                    issue_type=str(state.get("classification", {}).get("issue_type", "unknown")),
                    candidate=candidate,
                    observations=state.get("tool_observations", []),
                    evidence_conflict=bool(state.get("evidence_conflict", False)),
                    specified_request=self.host._message_specifies_request(
                        state["redacted_message"]
                    ),
                    can_replan=False,
                    explainable_comparison=integrity.explainable_comparison,
                )
                if reassessed.sufficient:
                    evidence_assessment = reassessed
                    decision = replace(
                        decision,
                        finish_reason="answered",
                    )
        return _PolicyFreshnessOutcome(
            candidate=candidate,
            decision=decision,
            evidence_assessment=evidence_assessment,
            finish_reason=decision.finish_reason,
            freshness_limited_claims_removed=removed,
            candidate_binding_ids=candidate_binding_ids,
        )

    async def _publish_policy_decision(
        self,
        state: AgentState,
        contracts: _PolicyContracts,
        evidence: _PolicyEvidenceContext,
        comparison: _PolicyComparisonContext,
        integrity: _PolicyIntegrityContext,
        requirements: _PolicyRequirements,
        resolution: _PolicyCandidateResolution,
        route_outcome: _PolicyRouteOutcome,
        freshness: _PolicyFreshnessOutcome,
    ) -> AgentState:
        candidate = freshness.candidate
        decision = freshness.decision
        route = decision.route
        finish_reason = freshness.finish_reason
        classification = state.get("classification", {})
        policy_boundary = str(classification.get("policy_boundary", "allowed"))
        validated_answer = self.host._render_validated_answer(
            candidate,
            route=route,
            finish_reason=finish_reason,
            integrity=resolution.integrity,
            issue_type=str(classification.get("issue_type") or "unknown"),
            requested_action=requirements.requested_action,
            conversation_continues=self.host._has_active_action_context(state),
            policy_boundary=policy_boundary,
            trusted_platform_fact=contracts.platform_contract,
            trusted_action_state_fact=contracts.action_state_contract,
            explicit_first_step=self.host._requests_explicit_first_step(
                str(state.get("redacted_message", ""))
            ),
            knowledge_read_failed=failed_current_tool_observation(state, "search_knowledge"),
            rate_limit_diagnostic_reads_complete=api_rate_limit_diagnostic_reads_complete(state),
        )
        if (
            resolution.integrity
            and candidate.material_claims
            and finish_reason
            not in {"proposal_eligibility_failed", "credential_redaction_guidance"}
            and route != PolicyRoute.REJECT
        ):
            await self._persist_validated_claims(
                state, candidate, validated_answer=validated_answer
            )
        await self.host._event(
            state,
            "policy_decision",
            {
                "route": route.value,
                "citation_integrity": resolution.integrity,
                "candidate_sha256": decision.candidate_sha256,
                "citation_integrity_diagnostics": {
                    "checks": integrity.integrity_checks,
                    "failure_codes": integrity.integrity_failure_codes,
                    "claim_count": len(candidate.material_claims),
                    "supported_claim_count": sum(
                        bool(claim.knowledge_locator_hashes or claim.observation_source_ids)
                        for claim in candidate.material_claims
                    ),
                    "selected_binding_count": len(freshness.candidate_binding_ids),
                },
                "knowledge_comparison": {
                    "requested": bool(state.get("knowledge_comparison_requested", False)),
                    "complete": bool(state.get("knowledge_comparison_complete", False)),
                    "publishable": integrity.explainable_comparison,
                    "cited_groups": sorted(integrity.cited_evidence_groups),
                    "required_transition_markers": comparison.transition_markers,
                    "missing_transition_markers": integrity.missing_transition_markers,
                },
                "proposal_eligibility": integrity.proposal_eligibility.model_dump(mode="json"),
                "evidence_assessment": freshness.evidence_assessment.model_dump(mode="json"),
                "freshness_limited_claims_removed": (freshness.freshness_limited_claims_removed),
                "applicability_scope_claims_canonicalized": len(
                    comparison.applicability_scope_claims
                ),
                "comparison_citation_bindings_canonicalized": (
                    evidence.comparison_citation_bindings_canonicalized
                ),
                "comparison_transition_claims_canonicalized": len(
                    comparison.comparison_transition_claims
                ),
                "comparison_version_role_claims_canonicalized": (
                    comparison.comparison_version_role_claims_canonicalized
                ),
                "grants_mutation": decision.grants_mutation,
            },
            visibility="customer",
        )
        update: AgentState = {
            "candidate": candidate.model_dump(mode="json"),
            "policy_route": route.value,
            "citation_integrity": resolution.integrity,
            "agent_finish_reason": finish_reason or state.get("agent_finish_reason", "answered"),
            "validated_answer": validated_answer,
            "proposal_eligibility": integrity.proposal_eligibility.model_dump(mode="json"),
            "evidence_assessment": freshness.evidence_assessment.model_dump(mode="json"),
            "evidence_replan_required": False,
        }
        if route_outcome.canonical_safe_decision is not None:
            update["agent_decision"] = route_outcome.canonical_safe_decision
        return update

    async def policy(self, state: AgentState) -> AgentState:
        contracts = self._prepare_policy_contracts(state)
        candidate = contracts.candidate
        candidate, evidence = self._prepare_policy_evidence_context(state, candidate)
        candidate, applicability_scope_claims = self._canonicalize_policy_applicability(
            candidate,
            evidence,
        )
        comparison = self._canonicalize_policy_comparison(
            state,
            candidate,
            evidence,
            applicability_scope_claims,
        )
        candidate = comparison.candidate
        integrity = await self._assess_policy_integrity(
            state, candidate, contracts, evidence, comparison
        )
        requirements = self._prepare_policy_requirements(
            state, candidate, contracts, evidence, integrity
        )
        required_replan = await self._required_policy_replan(state, integrity, requirements)
        if required_replan is not None:
            return required_replan
        resolution = await self._resolve_policy_candidate(
            state, candidate, evidence, integrity, requirements
        )
        if resolution.replan_update is not None:
            return resolution.replan_update
        candidate = resolution.candidate
        evidence_assessment, evidence_replan = await self._terminal_policy_evidence(
            state, candidate, contracts, integrity, requirements, resolution
        )
        if evidence_replan is not None:
            return evidence_replan
        route_outcome = self._apply_pure_policy(
            state,
            candidate,
            contracts,
            integrity,
            requirements,
            resolution,
            evidence_assessment,
        )
        candidate = route_outcome.candidate
        decision = route_outcome.decision
        evidence_assessment = route_outcome.evidence_assessment
        freshness = self._apply_policy_freshness(
            state,
            candidate,
            decision,
            evidence,
            integrity,
            requirements,
            evidence_assessment,
        )
        return await self._publish_policy_decision(
            state,
            contracts,
            evidence,
            comparison,
            integrity,
            requirements,
            resolution,
            route_outcome,
            freshness,
        )

    @staticmethod
    def _terminal_business_contract_valid(
        state: AgentState,
        candidate: CandidateResponse,
    ) -> bool:
        payload = state.get("terminal_business_outcome")
        ledger_payload = state.get("action_obligation_ledger")
        admission_payload = state.get("action_admission")
        if not payload or not ledger_payload or not admission_payload:
            return False
        try:
            outcome = TerminalBusinessOutcome.model_validate(payload)
            ledger = ActionObligationLedger.model_validate(ledger_payload)
            admission = ActionAdmissionV2.model_validate(admission_payload)
            rendering = render_terminal_business_outcome(outcome)
        except (TypeError, ValidationError, ValueError):
            return False
        if (
            ledger.next_state != "explain_terminal"
            or ledger.terminal_outcome != outcome
            or ledger.run_id != state.get("run_id")
            or ledger.tenant_id != state.get("tenant_id")
            or ledger.customer_id != state.get("customer_id")
            or ledger.scope_hash != admission.scope_hash
            or admission.status != "admitted"
            or admission.action_type != outcome.action_type
            or outcome.binding.tenant_id != state.get("tenant_id")
            or outcome.binding.customer_id != state.get("customer_id")
            or outcome.binding.scope_hash != admission.scope_hash
            or candidate.action != "answer"
            or candidate.answer != rendering.answer
            or candidate.knowledge_chunk_ids
            or candidate.knowledge_citations
            or candidate.material_claims
            or candidate.proposed_arguments
            or set(candidate.business_source_ids) != set(outcome.binding.source_ids)
        ):
            return False
        observation = next(
            (
                item
                for item in state.get("tool_observations", [])
                if item.get("run_id") == state.get("run_id")
                and str(
                    item.get("observation_id") or item.get("id") or item.get("tool_call_id", "")
                )
                == outcome.binding.observation_id
            ),
            None,
        )
        if observation is None:
            return False
        observed_hash = str(
            observation.get("observation_content_hash")
            or observation.get("content_hash")
            or canonical_json_hash(observation)
        )
        observed_sources = {
            str(source.get("source_id"))
            for source in observation.get("source_refs", [])
            if isinstance(source, dict) and source.get("source_id")
        }
        return (
            observed_hash == outcome.binding.observation_content_hash
            and set(outcome.binding.source_ids) <= observed_sources
        )

    def route_policy(self, state: AgentState) -> str:
        if state.get("evidence_replan_required"):
            return "replan"
        raw_route = str(state["policy_route"])
        if raw_route in {"safe_action", "manual_takeover"}:
            return "finalize"
        route = PolicyRoute(raw_route)
        if route == PolicyRoute.AWAIT_APPROVAL:
            return "proposal"
        # Historical route values remain parseable, but they converge directly
        # on safe finalization and cannot enter an operator capability.
        return "finalize"

    async def _persist_validated_claims(
        self,
        state: AgentState,
        candidate: CandidateResponse,
        *,
        validated_answer: str,
    ) -> None:
        if self.host.session is None:
            return
        if self.host.test_capability is not None:
            return
        provider_attempt_id = state.get("latest_provider_attempt_id")
        context_ledger_id = state.get("latest_context_ledger_id")
        if not provider_attempt_id or not context_ledger_id:
            raise RuntimeConflict("claim_lineage_missing")
        lease = await self.host._current_lease(state)
        if lease is None:
            raise RuntimeConflict("claim_fence_missing")
        await RuntimeJobRepository(self.host.session).assert_fence(lease)
        answer_hash = hashlib.sha256(validated_answer.encode()).hexdigest()
        for claim in candidate.material_claims:
            support_refs = {
                "citation_binding_ids": sorted(claim.citation_binding_ids),
                "knowledge_locator_hashes": sorted(claim.knowledge_locator_hashes),
                "observation_source_ids": sorted(claim.observation_source_ids),
            }
            claim_payload = {
                "text": claim.text,
                "support_refs": support_refs,
                "provider_attempt_id": provider_attempt_id,
                "context_ledger_id": context_ledger_id,
            }
            claim_hash = hashlib.sha256(
                json.dumps(
                    claim_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            existing = await self.host.session.scalar(
                select(ClaimRecord).where(
                    ClaimRecord.run_id == state["run_id"],
                    ClaimRecord.claim_hash == claim_hash,
                )
            )
            if existing is not None:
                if (
                    existing.answer_hash != answer_hash
                    or existing.provider_attempt_id != provider_attempt_id
                    or existing.context_ledger_id != context_ledger_id
                ):
                    raise RuntimeConflict("claim_lineage_changed")
                continue
            self.host.session.add(
                ClaimRecord(
                    tenant_id=state["tenant_id"],
                    run_id=state["run_id"],
                    job_id=state["job_id"],
                    provider_attempt_id=provider_attempt_id,
                    context_ledger_id=context_ledger_id,
                    claim_hash=claim_hash,
                    answer_hash=answer_hash,
                    claim_text=claim.text,
                    support_refs=support_refs,
                    status="validated",
                )
            )
        await self.host.session.flush()
        await self.host.session.commit()
