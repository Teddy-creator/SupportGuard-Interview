from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from supportguard.agent.constants import MAX_LLM_CALLS, MAX_TOOL_ATTEMPTS, MAX_TOOL_ROUNDS
from supportguard.agent.context import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetExceeded,
    build_trusted_task_state,
)
from supportguard.agent.evidence import comparison_transition_markers
from supportguard.agent.proposal_assembler import (
    SynthesisBindingError,
    bind_provider_synthesis,
    canonicalize_unreferenced_provider_claims,
    provider_synthesis_reference_contract,
)
from supportguard.agent.schemas import (
    AgentDecision,
    CandidateResponse,
    GroundedRepairEligibility,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.agent.state import AgentState
from supportguard.db.models import RawProviderDecisionEnvelope, new_id
from supportguard.prompts.registry import load_prompt
from supportguard.providers.base import (
    ProviderCallResult,
    RawProviderDecision,
    StructuredProvider,
    normalize_decision_result,
    normalize_provider_result,
    raw_decision_from_typed,
)
from supportguard.providers.deepseek import ProviderStructuredOutputError
from supportguard.services.attempts import ReservedAttempt
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict

RepairReservation = tuple[JobLease, ReservedAttempt] | None
RepairResult = tuple[
    AgentDecision,
    ProviderCallResult[RawProviderDecision],
    list[dict[str, Any]],
    str | None,
    RepairReservation,
    RawProviderDecisionEnvelope | None,
    AssembledContext,
    list[dict[str, Any]],
    str,
]


class DecisionRepairHost(Protocol):
    provider: StructuredProvider
    context_assembler: ContextAssembler

    def _canonicalize_bound_synthesis_extra_fields(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_repair_extra_fields(self, *args: Any, **kwargs: Any) -> Any: ...
    def _decision_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    def _exception_transport_attempts(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _finish_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _grounded_terminal_repair_eligibility(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_context_ledger(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_raw_provider_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _prepare_context_evidence_bindings(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _prepare_context_observation_memberships(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_component_manifest(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_failure_error_code(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _terminal_reference_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trace(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class _RepairAudit:
    deterministic_extra_field_prune: bool = False
    deterministic_unbound_claim_prune: bool = False
    pruned_claim_indices: list[int] = field(default_factory=list)
    repair_extra_error_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _RepairContext:
    state: AgentState
    original_attempt_id: str
    repair: RepairReservation
    context_ledger_id_hint: str
    evidence: list[dict[str, Any]]
    binding_plans: list[dict[str, Any]]
    membership_root_hash: str
    observation_membership_root_hash: str
    context_observations: list[dict[str, Any]]
    grounded_eligibility: GroundedRepairEligibility
    grounded_requirements: tuple[bool, bool] | None
    repair_tools: list[dict[str, Any]]
    assembled: AssembledContext
    error_paths: list[str]
    repair_system: str
    repair_user: str
    repair_trace_metadata: dict[str, Any]
    required_knowledge_groups: tuple[str, ...]
    required_answer_markers: tuple[str, ...]
    audit: _RepairAudit

    @property
    def node(self) -> str:
        return "grounded_terminal_repair" if self.grounded_requirements else "structure_repair"


@dataclass(frozen=True, slots=True)
class _RepairProviderOutcome:
    decision: AgentDecision
    typed_result: ProviderCallResult[Any]
    raw: RawProviderDecision


@dataclass(frozen=True, slots=True)
class DecisionRepair:
    """Own the run's single bounded terminal structure-repair attempt."""

    host: DecisionRepairHost

    async def repair(
        self,
        state: AgentState,
        *,
        original_attempt_id: str,
        parse_error: Exception,
        prompt_hash: str,
        tools: list[dict[str, Any]],
        evidence_lineage: list[dict[str, Any]],
        context_observations: list[dict[str, Any]],
    ) -> RepairResult | str | None:
        context = await self._prepare(
            state,
            original_attempt_id=original_attempt_id,
            parse_error=parse_error,
            prompt_hash=prompt_hash,
            tools=tools,
            evidence_lineage=evidence_lineage,
            context_observations=context_observations,
        )
        if context is None:
            return None
        try:
            outcome = await self._call_and_validate(context)
        except Exception as exc:
            return await self._settle_failure(context, exc)
        return await self._settle_success(context, outcome)

    async def _prepare(
        self,
        state: AgentState,
        *,
        original_attempt_id: str,
        parse_error: Exception,
        prompt_hash: str,
        tools: list[dict[str, Any]],
        evidence_lineage: list[dict[str, Any]],
        context_observations: list[dict[str, Any]],
    ) -> _RepairContext | None:
        repair = await self.host._reserve_external(
            state,
            "structure_repair",
            repair_of_attempt_id=original_attempt_id,
        )
        repair_attempt_id = repair[1].id if repair is not None else new_id("attempt")
        context_ledger_id_hint = new_id("context")
        observation_lineage = list(state.get("tool_observations", []))
        try:
            (
                evidence,
                binding_plans,
                membership_root_hash,
            ) = await self.host._prepare_context_evidence_bindings(
                state,
                evidence_lineage,
                provider_attempt_id=repair_attempt_id,
                context_ledger_id=context_ledger_id_hint,
            )
            (
                observation_plans,
                observation_root_hash,
            ) = await self.host._prepare_context_observation_memberships(
                state,
                observation_lineage,
                context_observations,
                provider_attempt_id=repair_attempt_id,
                context_ledger_id=context_ledger_id_hint,
                payload_ordinal_offset=len(evidence_lineage),
            )
            binding_plans.extend(observation_plans)
            eligibility = self.host._grounded_terminal_repair_eligibility(
                state,
                evidence=evidence,
                observations=context_observations,
            )
            grounded = (
                (eligibility.require_knowledge_source, eligibility.require_business_source)
                if eligibility.selected
                else None
            )
            repair_tools = [] if grounded is not None else tools
            assembled = self.host.context_assembler.assemble(
                run_id=state["run_id"],
                step_index=state.get("step_index", 0) + 1,
                user_goal=state["redacted_message"],
                trusted_task_state=build_trusted_task_state(state),
                tools=repair_tools,
                latest_observations=context_observations,
                evidence=evidence,
                evidence_lineage=evidence_lineage,
                history=state.get("relevant_history", []),
                remaining_budget={
                    "llm_calls": MAX_LLM_CALLS - state["llm_calls"] - 1,
                    "tool_rounds": MAX_TOOL_ROUNDS - state["tool_rounds"],
                    "tool_attempts": MAX_TOOL_ATTEMPTS - state["tool_attempts"],
                },
                prior_turns=state.get("provider_turns", []),
            )
        except (ContextBudgetExceeded, RuntimeConflict) as exc:
            await self.host._finish_external(
                repair,
                status="failed",
                error_code=f"structure_repair_context:{type(exc).__name__}",
            )
            return None
        error_paths = self.host._decision_error_paths(parse_error)
        grounded_prompt = (
            load_prompt("bound_evidence_synthesis", version="v1") if grounded else None
        )
        repair_system = (
            grounded_prompt.content if grounded_prompt else self._generic_system_prompt()
        )
        required_groups = (
            ("current", "historical")
            if grounded and state.get("knowledge_comparison_complete", False)
            else ()
        )
        required_markers = tuple(comparison_transition_markers(evidence)) if required_groups else ()
        repair_payload: dict[str, Any] = {
            "error_paths": error_paths,
            "same_redacted_context": assembled.content,
        }
        if grounded is not None:
            repair_payload["reference_contract"] = provider_synthesis_reference_contract(
                evidence=evidence,
                observations=context_observations,
                require_knowledge_source=grounded[0],
                require_business_source=grounded[1],
                required_knowledge_groups=required_groups,
                required_answer_markers=required_markers,
            )
        trace = {
            **self.host._trace(
                grounded_prompt.content_hash if grounded_prompt else prompt_hash,
                state,
            ),
            "repair_of_attempt_id": original_attempt_id,
        }
        return _RepairContext(
            state=state,
            original_attempt_id=original_attempt_id,
            repair=repair,
            context_ledger_id_hint=context_ledger_id_hint,
            evidence=evidence,
            binding_plans=binding_plans,
            membership_root_hash=membership_root_hash,
            observation_membership_root_hash=observation_root_hash,
            context_observations=context_observations,
            grounded_eligibility=eligibility,
            grounded_requirements=grounded,
            repair_tools=repair_tools,
            assembled=assembled,
            error_paths=error_paths,
            repair_system=repair_system,
            repair_user=json.dumps(repair_payload, ensure_ascii=False),
            repair_trace_metadata=trace,
            required_knowledge_groups=required_groups,
            required_answer_markers=required_markers,
            audit=_RepairAudit(),
        )

    @staticmethod
    def _generic_system_prompt() -> str:
        return (
            "Repair one terminal AgentDecision to the supplied strict schema. "
            "Return only the complete JSON object. Do not call tools, add facts, "
            "change resource identities, or infer missing business fields. Obey exact "
            "field nesting and remove every field named by an extra_forbidden error. "
            "A MaterialClaim permits only text, citation_binding_ids, "
            "knowledge_locator_hashes, and observation_source_ids; "
            "business_source_ids belongs only to CandidateResponse."
        )

    async def _call_and_validate(self, context: _RepairContext) -> _RepairProviderOutcome:
        if context.grounded_requirements is not None:
            return await self._call_grounded(context)
        return await self._call_generic(context)

    async def _call_grounded(self, context: _RepairContext) -> _RepairProviderOutcome:
        try:
            typed = normalize_provider_result(
                await self.host.provider.generate(
                    system=context.repair_system,
                    user=context.repair_user,
                    output_schema=ProviderBoundEvidenceSynthesis,
                    trace_metadata=context.repair_trace_metadata,
                )
            )
        except ProviderStructuredOutputError as exc:
            canonical = self.host._canonicalize_bound_synthesis_extra_fields(exc)
            if canonical is None:
                raise
            context.audit.deterministic_extra_field_prune = True
            context.audit.repair_extra_error_paths = self.host._decision_error_paths(exc)
            typed = ProviderCallResult(
                output=canonical,
                attempts=1,
                usage=exc.usage,
                trace_metadata=context.repair_trace_metadata,
                transport=exc.transport,
                transport_attempts=exc.transport_attempts,
            )
        grounded_requirements = context.grounded_requirements
        if grounded_requirements is None:
            raise RuntimeConflict("grounded_repair_contract_missing")
        require_knowledge, require_business = grounded_requirements
        try:
            synthesis = bind_provider_synthesis(
                synthesis=typed.output,
                evidence=context.evidence,
                observations=context.context_observations,
                require_knowledge_source=require_knowledge,
                require_business_source=require_business,
                required_knowledge_groups=context.required_knowledge_groups,
                required_answer_markers=context.required_answer_markers,
            )
        except SynthesisBindingError as exc:
            canonicalized = canonicalize_unreferenced_provider_claims(
                synthesis=typed.output,
                evidence=context.evidence,
                observations=context.context_observations,
                require_knowledge_source=require_knowledge,
                require_business_source=require_business,
                required_knowledge_groups=context.required_knowledge_groups,
                required_answer_markers=context.required_answer_markers,
            )
            if canonicalized is None:
                if typed.transport is None:
                    raise RuntimeConflict("provider_transport_missing") from exc
                raise ProviderStructuredOutputError(
                    error_paths=exc.error_paths,
                    transport=typed.transport,
                    usage=typed.usage,
                    transport_attempts=typed.transport_attempts,
                ) from exc
            canonical_output, synthesis, indices = canonicalized
            context.audit.deterministic_unbound_claim_prune = True
            context.audit.pruned_claim_indices = list(indices)
            typed = ProviderCallResult(
                output=canonical_output,
                attempts=typed.attempts,
                usage=typed.usage,
                trace_metadata=typed.trace_metadata,
                transport=typed.transport,
                transport_attempts=typed.transport_attempts,
            )
        decision = AgentDecision(
            decision_type="final_candidate",
            decision_summary="Answer bound to current Runtime evidence.",
            candidate=CandidateResponse(
                answer=synthesis.answer,
                action="answer",
                knowledge_chunk_ids=synthesis.knowledge_chunk_ids,
                knowledge_citations=synthesis.knowledge_citations,
                business_source_ids=synthesis.business_source_ids,
                material_claims=synthesis.material_claims,
                proposed_arguments={},
            ),
        )
        raw = RawProviderDecision(
            finish_reason="stop",
            content=typed.output.model_dump_json(),
            tool_calls=(),
        )
        return _RepairProviderOutcome(decision=decision, typed_result=typed, raw=raw)

    async def _call_generic(self, context: _RepairContext) -> _RepairProviderOutcome:
        try:
            typed = normalize_provider_result(
                await self.host.provider.generate(
                    system=context.repair_system,
                    user=context.repair_user,
                    output_schema=AgentDecision,
                    trace_metadata=context.repair_trace_metadata,
                )
            )
            decision = typed.output
        except ProviderStructuredOutputError as exc:
            decision = self.host._canonicalize_repair_extra_fields(exc)
            if decision is None:
                raise
            context.audit.deterministic_extra_field_prune = True
            context.audit.repair_extra_error_paths = self.host._decision_error_paths(exc)
            typed = ProviderCallResult(
                output=decision,
                attempts=1,
                usage=exc.usage,
                trace_metadata=context.repair_trace_metadata,
                transport=exc.transport,
                transport_attempts=exc.transport_attempts,
            )
        if decision.decision_type == "tool_calls" or decision.tool_calls:
            raise ValueError("structure repair cannot return tool calls")
        error_paths = self.host._terminal_reference_error_paths(
            decision,
            evidence=context.evidence,
            observations=context.context_observations,
        )
        if error_paths:
            if typed.transport is None:
                raise RuntimeConflict("provider_transport_missing")
            raise ProviderStructuredOutputError(
                error_paths=tuple(error_paths),
                transport=typed.transport,
                usage=typed.usage,
                transport_attempts=typed.transport_attempts,
            )
        return _RepairProviderOutcome(
            decision=decision,
            typed_result=typed,
            raw=raw_decision_from_typed(decision),
        )

    def _manifest(
        self,
        context: _RepairContext,
        *,
        structured_error_paths: list[str] | None = None,
        include_success_contract: bool,
    ) -> dict[str, Any]:
        payload = {
            **self.host._provider_component_manifest(
                context.assembled,
                tools=context.repair_tools,
                node=context.node,
            ),
            "repair_of_attempt_id": context.original_attempt_id,
            "error_paths": context.error_paths,
            "grounded_answer_only": context.grounded_requirements is not None,
            "grounded_repair_eligibility": context.grounded_eligibility.model_dump(mode="json"),
            "deterministic_extra_field_prune": (context.audit.deterministic_extra_field_prune),
            "deterministic_unbound_claim_prune": (context.audit.deterministic_unbound_claim_prune),
            "pruned_claim_indices": context.audit.pruned_claim_indices,
            "repair_extra_error_paths": context.audit.repair_extra_error_paths,
            "observation_membership_root_hash": context.observation_membership_root_hash,
        }
        if structured_error_paths is not None:
            payload["structured_error_paths"] = structured_error_paths
        if include_success_contract:
            payload["ordered_membership_root_hash"] = context.membership_root_hash
            payload["required_reference_namespaces"] = (
                {
                    "knowledge": context.grounded_requirements[0],
                    "business": context.grounded_requirements[1],
                    "knowledge_groups": list(context.required_knowledge_groups),
                    "answer_markers": list(context.required_answer_markers),
                }
                if context.grounded_requirements is not None
                else None
            )
        return payload

    async def _settle_failure(
        self,
        context: _RepairContext,
        exc: Exception,
    ) -> str | None:
        structured_paths = (
            self.host._decision_error_paths(exc)
            if isinstance(exc, ProviderStructuredOutputError)
            else []
        )
        failure_code = (
            "provider_terminal_schema_invalid"
            if isinstance(exc, ProviderStructuredOutputError)
            else self.host._provider_failure_error_code(exc)
        )
        transport = exc.transport if isinstance(exc, ProviderStructuredOutputError) else None
        await self.host._persist_context_ledger(
            context.state,
            context.repair,
            component_manifest=self._manifest(
                context,
                structured_error_paths=structured_paths,
                include_success_contract=False,
            ),
            transport=transport,
            require_capture=False,
            ledger_id=context.context_ledger_id_hint,
            binding_plans=context.binding_plans if transport is not None else None,
        )
        await self.host._finish_external(
            context.repair,
            status="failed",
            error_code=failure_code,
            prompt_tokens=(
                exc.usage.prompt_tokens if isinstance(exc, ProviderStructuredOutputError) else 0
            ),
            completion_tokens=(
                exc.usage.completion_tokens if isinstance(exc, ProviderStructuredOutputError) else 0
            ),
            provider_transport_attempts=self.host._exception_transport_attempts(exc),
            structured_error_paths=(structured_paths or None),
        )
        return None if failure_code == "provider_terminal_schema_invalid" else failure_code

    async def _settle_success(
        self,
        context: _RepairContext,
        outcome: _RepairProviderOutcome,
    ) -> RepairResult:
        typed = outcome.typed_result
        ledger_id = await self.host._persist_context_ledger(
            context.state,
            context.repair,
            component_manifest=self._manifest(
                context,
                include_success_contract=True,
            ),
            transport=typed.transport,
            ledger_id=context.context_ledger_id_hint,
            binding_plans=context.binding_plans,
        )
        raw_envelope = await self.host._persist_raw_provider_decision(
            context.state,
            context.repair,
            outcome.raw,
        )
        if raw_envelope is not None:
            raw_envelope.intake_status = "parsed"
        await self.host._finish_external(
            context.repair,
            status="succeeded",
            prompt_tokens=typed.usage.prompt_tokens,
            completion_tokens=typed.usage.completion_tokens,
            provider_transport_attempts=typed.transport_attempts,
        )
        normalized = normalize_decision_result(
            ProviderCallResult(
                output=outcome.raw,
                attempts=1,
                usage=typed.usage,
                trace_metadata=typed.trace_metadata,
                transport=typed.transport,
                transport_attempts=typed.transport_attempts,
            )
        )
        return (
            outcome.decision,
            normalized,
            context.evidence,
            ledger_id,
            context.repair,
            raw_envelope,
            context.assembled,
            context.repair_tools,
            context.node,
        )
