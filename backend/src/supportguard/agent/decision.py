from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import ValidationError

from supportguard.agent.api_diagnostics import required_api_rate_limit_diagnostic_reads
from supportguard.agent.constants import MAX_LLM_CALLS, MAX_TOOL_ATTEMPTS, MAX_TOOL_ROUNDS
from supportguard.agent.context import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetExceeded,
    build_trusted_task_state,
)
from supportguard.agent.current_facts import (
    requested_current_fact_requirements,
    resolve_referential_billing_reference,
)
from supportguard.agent.decision_repair import DecisionRepair, DecisionRepairHost
from supportguard.agent.evidence import (
    EvidenceAssessment,
    decide_evidence,
    derive_evidence_requirements,
)
from supportguard.agent.evidence_contracts import EvidenceDecision, EvidenceGroup
from supportguard.agent.nodes.finalization import SafeStopHost, safe_stop
from supportguard.agent.obligations import ActionObligationLedger
from supportguard.agent.policy import PolicyRoute
from supportguard.agent.responses import safe_failure_answer
from supportguard.agent.schemas import AgentDecision, CandidateResponse
from supportguard.agent.state import AgentState
from supportguard.agent.tool_loop import open_tool_turn
from supportguard.agent.tool_loop_contracts import ToolTurnHost
from supportguard.contracts.action_preconditions import ActionAdmission, ActionAdmissionV2
from supportguard.db.models import RawProviderDecisionEnvelope, new_id
from supportguard.observability.metrics import AGENT_DECISIONS
from supportguard.prompts.registry import PromptAsset, load_prompt
from supportguard.providers.base import (
    ProviderCallResult,
    RawProviderDecision,
    StructuredProvider,
    normalize_decision_result,
)
from supportguard.providers.deepseek import ProviderStructuredOutputError
from supportguard.services.attempts import ReservedAttempt
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict
from supportguard.tools.gateway import native_read_tool_schemas


class DecisionNodeHost(SafeStopHost, ToolTurnHost, Protocol):
    provider: StructuredProvider
    context_assembler: ContextAssembler

    def _allowlist(self, *args: Any, **kwargs: Any) -> Any: ...
    def _assistant_tool_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_action_read_arguments(self, *args: Any, **kwargs: Any) -> Any: ...
    def _canonicalize_candidate_references(self, *args: Any, **kwargs: Any) -> Any: ...
    def _decision_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _event(self, *args: Any, **kwargs: Any) -> Any: ...
    def _exception_transport_attempts(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _finish_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _finish_reason(self, *args: Any, **kwargs: Any) -> Any: ...
    def _ground_policy_follow_up_query(self, *args: Any, **kwargs: Any) -> Any: ...
    def _has_active_action_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def _message_specifies_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def _normalize_terminal_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    def _parse_raw_provider_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_context_ledger(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_raw_provider_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    def _prepare_context_evidence_bindings(self, *args: Any, **kwargs: Any) -> Any: ...
    def _prepare_context_observation_memberships(self, *args: Any, **kwargs: Any) -> Any: ...
    def _project_context_evidence(self, *args: Any, **kwargs: Any) -> Any: ...
    def _project_context_observation(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_component_manifest(self, *args: Any, **kwargs: Any) -> Any: ...
    def _provider_failure_error_code(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reject_raw_tool_batch(self, *args: Any, **kwargs: Any) -> Any: ...
    def _required_evidence_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _selected_action_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def _terminal_reference_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    def _topic_continuity_event_payload(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trace(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trusted_action_state_answer(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trusted_platform_answer(self, *args: Any, **kwargs: Any) -> Any: ...


DecisionReservation = tuple[JobLease, ReservedAttempt] | None


@dataclass(frozen=True, slots=True)
class ProviderDecisionPreparation:
    state: AgentState
    tools: list[dict[str, Any]]
    evidence_lineage: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    context_observations: list[dict[str, Any]]
    assembled: AssembledContext
    prompt: PromptAsset
    reserved: DecisionReservation
    context_ledger_id_hint: str
    provider_attempt_id: str
    binding_plans: list[dict[str, Any]]
    membership_root_hash: str
    observation_membership_root_hash: str
    evidence_decision: EvidenceDecision


@dataclass(frozen=True, slots=True)
class ProviderDecisionCall:
    preparation: ProviderDecisionPreparation
    result: ProviderCallResult[RawProviderDecision]
    context_ledger_id: str | None
    raw_envelope: RawProviderDecisionEnvelope | None


@dataclass(frozen=True, slots=True)
class ValidatedProviderDecision:
    state: AgentState
    decision: AgentDecision
    provider_result: ProviderCallResult[RawProviderDecision]
    evidence: list[dict[str, Any]]
    evidence_lineage: list[dict[str, Any]]
    context_observations: list[dict[str, Any]]
    context_ledger_id: str | None
    reserved: DecisionReservation
    parsed_envelope: RawProviderDecisionEnvelope | None
    assembled: AssembledContext
    decision_tools: list[dict[str, Any]]
    decision_node: str
    provider_attempt_id: str
    original_tools: list[dict[str, Any]]
    terminal_repaired: bool
    evidence_decision: EvidenceDecision


@dataclass(frozen=True, slots=True)
class NormalizedProviderDecision:
    validated: ValidatedProviderDecision
    decision: AgentDecision
    calls: int
    knowledge_query_grounded: bool
    action_read_arguments_canonicalized: bool
    redundant_claim_source_bindings_removed: int
    provider_turns: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DecisionNodes:
    """Own Provider decision orchestration, validation and route selection."""

    host: DecisionNodeHost

    @staticmethod
    def _current_fact_evidence_groups(state: AgentState) -> tuple[EvidenceGroup, ...]:
        tool_groups: dict[str, EvidenceGroup] = {
            "search_knowledge": "knowledge",
            "query_request_trace": "request_trace",
            "query_billing_record": "billing_record",
            "query_api_key_metadata": "api_key_metadata",
            "query_subscription": "subscription",
            "query_account": "account",
            "query_api_usage": "api_usage",
        }
        groups = [
            tool_groups[tool_name]
            for tool_name, _ in requested_current_fact_requirements(state).values()
            if tool_name in tool_groups
        ]
        groups.extend(
            tool_groups[tool_name]
            for tool_name in required_api_rate_limit_diagnostic_reads(state)
            if tool_name in tool_groups
        )
        referential_billing = resolve_referential_billing_reference(state)
        if referential_billing.status == "resolved":
            groups.append("billing_record")
        return tuple(dict.fromkeys(groups))

    @staticmethod
    def _project_evidence_citation_bindings(
        evidence: list[dict[str, Any]],
        *,
        provider_attempt_id: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "citation_binding_id": str(item.get("citation_binding_id") or ""),
                "provider_attempt_id": provider_attempt_id,
                "evidence_id": str(item.get("evidence_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "content_hash": str(item.get("content_hash") or ""),
                "locator_hash": str(item.get("source_locator_hash") or ""),
            }
            for item in evidence
            if item.get("citation_binding_id")
        ]

    def _decide_pre_candidate_evidence(
        self,
        state: AgentState,
        *,
        evidence: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        provider_attempt_id: str,
    ) -> EvidenceDecision:
        classification = state.get("classification", {})
        requirements = derive_evidence_requirements(
            issue_type=str(classification.get("issue_type", "unknown")),
            requested_action=str(classification.get("requested_action", "none")),
            specified_request=bool(
                self.host._message_specifies_request(str(state.get("redacted_message", "")))
            ),
            additional_groups=self._current_fact_evidence_groups(state),
        )
        can_replan = bool(
            state.get("evidence_replan_count", 0) < 1
            and state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS
            and state.get("tool_attempts", 0) < MAX_TOOL_ATTEMPTS
            and state.get("llm_calls", 0) < MAX_LLM_CALLS
        )
        return decide_evidence(
            requirements=requirements,
            observations=observations,
            citation_bindings=self._project_evidence_citation_bindings(
                evidence,
                provider_attempt_id=provider_attempt_id,
            ),
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            customer_id=state["customer_id"],
            provider_attempt_id=provider_attempt_id,
            evidence_conflict=bool(state.get("evidence_conflict", False)),
            can_replan=can_replan,
            explainable_comparison=bool(state.get("knowledge_comparison_complete", False)),
        )

    async def agent_decide(self, state: AgentState) -> AgentState:
        """Run deterministic prechecks, one bounded Provider call, then settlement."""

        terminal = await self._terminal_predecision(state)
        if terminal is not None:
            return terminal
        evidence_gate = await self._admission_and_evidence_predecision(state)
        if evidence_gate is not None:
            return evidence_gate
        preparation = await self._prepare_provider_decision(state)
        if isinstance(preparation, dict):
            return preparation
        provider_call = await self._call_provider(preparation)
        if isinstance(provider_call, dict):
            return provider_call
        validated = await self._validate_provider_decision(provider_call)
        if isinstance(validated, dict):
            return validated
        normalized = await self._normalize_provider_decision(validated)
        if isinstance(normalized, dict):
            return normalized
        return await self._publish_provider_decision(normalized)

    async def _terminal_predecision(self, state: AgentState) -> AgentState | None:
        if (
            state.get("candidate")
            and state.get("agent_finish_reason")
            and not state.get("evidence_replan_required")
        ):
            return {}
        policy_boundary = str(state.get("classification", {}).get("policy_boundary", "allowed"))
        if policy_boundary in {"out_of_scope", "prohibited"}:
            candidate = CandidateResponse(
                answer=safe_failure_answer(policy_boundary),
                action="reject",
                knowledge_chunk_ids=[],
                business_source_ids=[],
                proposed_arguments={},
            )
            decision = AgentDecision(
                decision_type="final_candidate",
                decision_summary="Deterministic policy boundary refused the request before tools.",
                candidate=candidate,
            )
            await self.host._event(
                state,
                "agent_decision",
                {
                    "decision_type": decision.decision_type,
                    "decision_summary": decision.decision_summary,
                    "tool_names": [],
                    "injected_tool_allowlist": [],
                    "deterministic_policy_boundary": policy_boundary,
                    "llm_calls": state["llm_calls"],
                    "remaining_budget": {
                        "llm_calls": max(0, MAX_LLM_CALLS - state["llm_calls"]),
                        "tool_rounds": max(0, MAX_TOOL_ROUNDS - state["tool_rounds"]),
                        "tool_attempts": max(0, MAX_TOOL_ATTEMPTS - state["tool_attempts"]),
                    },
                },
                visibility="customer",
            )
            return {
                "agent_decision": decision.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
                "agent_finish_reason": "rejected",
                "policy_route": PolicyRoute.REJECT.value,
                "step_index": state.get("step_index", 0) + 1,
                "evidence_replan_required": False,
            }
        action_state_answer = self.host._trusted_action_state_answer(state)
        if action_state_answer is not None:
            candidate = CandidateResponse(
                answer=action_state_answer,
                action="answer",
                knowledge_chunk_ids=[],
                business_source_ids=[],
                proposed_arguments={},
            )
            decision = AgentDecision(
                decision_type="final_candidate",
                decision_summary=(
                    "Answer from the current customer-scoped action-state projection."
                ),
                candidate=candidate,
            )
            selected = self.host._selected_action_state(state)
            await self.host._event(
                state,
                "agent_decision",
                {
                    "decision_type": decision.decision_type,
                    "decision_summary": decision.decision_summary,
                    "tool_names": [],
                    "injected_tool_allowlist": [],
                    "deterministic_action_state": {
                        "approval_id": (selected.approval_id if selected is not None else None),
                        "resource_id": (selected.resource_id if selected is not None else None),
                        "projection_status": (
                            selected.projection_status if selected is not None else None
                        ),
                        "grants_action_authority": False,
                    },
                    "llm_calls": state["llm_calls"],
                },
                visibility="customer",
            )
            return {
                "agent_decision": decision.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
                "agent_finish_reason": "action_state_answer",
                "step_index": state.get("step_index", 0) + 1,
                "evidence_replan_required": False,
            }
        platform_answer = self.host._trusted_platform_answer(
            str(state.get("classification", {}).get("support_subject", "customer_problem"))
        )
        if platform_answer is not None:
            candidate = CandidateResponse(
                answer=platform_answer,
                action="answer",
                knowledge_chunk_ids=[],
                business_source_ids=[],
                proposed_arguments={},
            )
            decision = AgentDecision(
                decision_type="final_candidate",
                decision_summary="Answer from the trusted SupportGuard product contract.",
                candidate=candidate,
            )
            await self.host._event(
                state,
                "agent_decision",
                {
                    "decision_type": decision.decision_type,
                    "decision_summary": decision.decision_summary,
                    "tool_names": [],
                    "injected_tool_allowlist": [],
                    "trusted_platform_subject": state.get("classification", {}).get(
                        "support_subject"
                    ),
                    "llm_calls": state["llm_calls"],
                },
                visibility="customer",
            )
            return {
                "agent_decision": decision.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
                "agent_finish_reason": "answered",
                "step_index": state.get("step_index", 0) + 1,
                "evidence_replan_required": False,
            }
        return None

    async def _admission_and_evidence_predecision(
        self,
        state: AgentState,
    ) -> AgentState | None:
        admission_payload = state.get("action_admission")
        if admission_payload:
            question: str | None = None
            if admission_payload.get("schema_version") == "action-admission.v2":
                admission_v2 = ActionAdmissionV2.model_validate(admission_payload)
                if admission_v2.status in {"missing", "mismatch"}:
                    question = admission_v2.clarification_question
                admission_event_payload = admission_v2.model_dump(mode="json")
            else:
                legacy_admission = ActionAdmission.model_validate(admission_payload)
                question = legacy_admission.clarification_question
                admission_event_payload = legacy_admission.model_dump(mode="json")
            if question is not None:
                candidate = CandidateResponse(
                    answer=question,
                    action="answer",
                    knowledge_chunk_ids=[],
                    business_source_ids=[],
                    proposed_arguments={},
                )
                decision = AgentDecision(
                    decision_type="needs_clarification",
                    decision_summary=(
                        "Deterministic action admission requires a typed field before "
                        "tools, proposals, or execution."
                    ),
                    candidate=candidate,
                    clarification_question=question,
                )
                await self.host._event(
                    state,
                    "agent_decision",
                    {
                        "decision_type": decision.decision_type,
                        "decision_summary": decision.decision_summary,
                        "tool_names": [],
                        "injected_tool_allowlist": [],
                        "deterministic_action_admission": admission_event_payload,
                        "llm_calls": state["llm_calls"],
                    },
                    visibility="customer",
                )
                return {
                    "agent_decision": decision.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json"),
                    "agent_finish_reason": "needs_clarification",
                    "step_index": state.get("step_index", 0) + 1,
                    "evidence_replan_required": False,
                }
        required_evidence = self.host._required_evidence_decision(state)
        if required_evidence is not None:
            required_tool_names = [item.call.name for item in required_evidence.tool_calls]
            evidence_requirement = (
                "referential_billing_policy_follow_up"
                if (
                    "query_billing_record" in required_tool_names
                    and resolve_referential_billing_reference(state).status == "resolved"
                    and not self.host._has_active_action_context(state)
                )
                else "explicit_current_fact_closure"
                if "search_knowledge" not in required_tool_names
                else "knowledge_and_current_account_follow_up"
                if state.get("classification", {}).get("needs_realtime_facts") is True
                else "versioned_knowledge_follow_up"
            )
            await self.host._event(
                state,
                "agent_decision",
                {
                    "decision_type": required_evidence.decision_type,
                    "decision_summary": required_evidence.decision_summary,
                    "tool_names": required_tool_names,
                    "injected_tool_allowlist": sorted(self.host._allowlist(state)),
                    "deterministic_evidence_requirement": evidence_requirement,
                    **self.host._topic_continuity_event_payload(state, required_evidence),
                    "llm_calls": state["llm_calls"],
                    "remaining_budget": {
                        "llm_calls": max(0, MAX_LLM_CALLS - state["llm_calls"]),
                        "tool_rounds": max(0, MAX_TOOL_ROUNDS - state["tool_rounds"]),
                        "tool_attempts": max(0, MAX_TOOL_ATTEMPTS - state["tool_attempts"]),
                    },
                },
                visibility="customer",
            )
            deterministic_update: AgentState = {
                "agent_decision": required_evidence.model_dump(mode="json"),
                "provider_turns": [
                    *state.get("provider_turns", []),
                    self.host._assistant_tool_turn(required_evidence),
                ],
                "step_index": state.get("step_index", 0) + 1,
                "evidence_replan_required": False,
            }
            deterministic_update.update(
                await open_tool_turn(
                    self.host,
                    state,
                    required_evidence,
                    {
                        "schema_version": "deterministic-evidence-requirement.v1",
                        "reason": evidence_requirement,
                        "run_id": state["run_id"],
                    },
                )
            )
            return deterministic_update
        return None

    async def _prepare_provider_decision(
        self,
        state: AgentState,
    ) -> ProviderDecisionPreparation | AgentState:
        if state["llm_calls"] >= MAX_LLM_CALLS:
            return await safe_stop(self.host, state, "llm_call_budget_exhausted")
        admission_for_budget = state.get("action_admission", {})
        if (
            admission_for_budget.get("schema_version") == "action-admission.v2"
            and admission_for_budget.get("status") == "admitted"
            and state["llm_calls"] >= MAX_LLM_CALLS - 1
        ):
            return await safe_stop(
                self.host,
                state,
                "action_synthesis_budget_reserved",
            )
        read_budget_exhausted = (
            state["tool_rounds"] >= MAX_TOOL_ROUNDS or state["tool_attempts"] >= MAX_TOOL_ATTEMPTS
        )
        # Once the bounded read budget is exhausted, the final Provider call
        # is evidence synthesis only.  Re-exposing Read Tool schemas invites a
        # redundant call that Runtime must reject, wasting the last LLM call
        # even when the accumulated Observations already answer the request.
        # The Provider still owns the candidate wording, while Policy retains
        # claim/source validation and all action authority remains unchanged.
        allowlist = set() if read_budget_exhausted else self.host._allowlist(state)
        tools = native_read_tool_schemas(allowlist)
        admitted_payload = state.get("action_admission", {})
        if admitted_payload.get("status") == "admitted":
            ledger_payload = state.get("action_obligation_ledger", {})
            await self.host._event(
                state,
                "tool_surface_reduced",
                {
                    "action_type": admitted_payload.get("action_type"),
                    "injected_tools": sorted(allowlist),
                    "unsatisfied_capabilities": (
                        list(ledger_payload.get("unsatisfied_capabilities", []))
                        if isinstance(ledger_payload, dict)
                        else []
                    ),
                },
                visibility="customer",
            )
        # Reuse the canonical evidence projection produced by execute_reads.
        # Re-deriving from the raw Observation would omit the injected
        # index_version and break publication-time context membership checks.
        evidence_lineage = list(state.get("evidence", []))
        evidence = [self.host._project_context_evidence(item) for item in evidence_lineage]
        context_observation_lineage = list(state.get("tool_observations", []))
        context_observations = [
            self.host._project_context_observation(observation)
            for observation in context_observation_lineage
        ]
        try:
            assembled = self.host.context_assembler.assemble(
                run_id=state["run_id"],
                step_index=state.get("step_index", 0) + 1,
                user_goal=state["redacted_message"],
                trusted_task_state=build_trusted_task_state(state),
                tools=tools,
                latest_observations=context_observations,
                evidence=evidence,
                evidence_lineage=evidence_lineage,
                history=state.get("relevant_history", []),
                remaining_budget={
                    "llm_calls": MAX_LLM_CALLS - state["llm_calls"],
                    "tool_rounds": MAX_TOOL_ROUNDS - state["tool_rounds"],
                    "tool_attempts": MAX_TOOL_ATTEMPTS - state["tool_attempts"],
                },
                prior_turns=state.get("provider_turns", []),
            )
        except ContextBudgetExceeded:
            return await safe_stop(self.host, state, "context_budget_exhausted")
        prompt = load_prompt("agent_decide", version="v6")
        reserved = await self.host._reserve_external(state, "llm")
        context_ledger_id_hint = new_id("context")
        provider_attempt_id = reserved[1].id if reserved is not None else new_id("attempt")
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
                observation_binding_plans,
                observation_membership_root_hash,
            ) = await self.host._prepare_context_observation_memberships(
                state,
                context_observation_lineage,
                context_observations,
                provider_attempt_id=provider_attempt_id,
                context_ledger_id=context_ledger_id_hint,
                payload_ordinal_offset=len(evidence_lineage),
            )
            binding_plans.extend(observation_binding_plans)
            evidence_decision = self._decide_pre_candidate_evidence(
                state,
                evidence=evidence,
                observations=context_observation_lineage,
                provider_attempt_id=provider_attempt_id,
            )
            trusted_task_state = {
                **build_trusted_task_state(state),
                "evidence_decision": evidence_decision.model_dump(mode="json"),
            }
            assembled = self.host.context_assembler.assemble(
                run_id=state["run_id"],
                step_index=state.get("step_index", 0) + 1,
                user_goal=state["redacted_message"],
                trusted_task_state=trusted_task_state,
                tools=tools,
                latest_observations=context_observations,
                evidence=evidence,
                evidence_lineage=evidence_lineage,
                history=state.get("relevant_history", []),
                remaining_budget={
                    "llm_calls": MAX_LLM_CALLS - state["llm_calls"],
                    "tool_rounds": MAX_TOOL_ROUNDS - state["tool_rounds"],
                    "tool_attempts": MAX_TOOL_ATTEMPTS - state["tool_attempts"],
                },
                prior_turns=state.get("provider_turns", []),
            )
        except (ContextBudgetExceeded, RuntimeConflict) as exc:
            await self.host._finish_external(reserved, status="failed", error_code=str(exc)[:100])
            return await safe_stop(self.host, state, "citation_binding_incomplete")
        return ProviderDecisionPreparation(
            state=state,
            tools=tools,
            evidence_lineage=evidence_lineage,
            evidence=evidence,
            context_observations=context_observations,
            assembled=assembled,
            prompt=prompt,
            reserved=reserved,
            context_ledger_id_hint=context_ledger_id_hint,
            provider_attempt_id=provider_attempt_id,
            binding_plans=binding_plans,
            membership_root_hash=membership_root_hash,
            observation_membership_root_hash=observation_membership_root_hash,
            evidence_decision=evidence_decision,
        )

    async def _call_provider(
        self,
        preparation: ProviderDecisionPreparation,
    ) -> ProviderDecisionCall | AgentState:
        state = preparation.state
        tools = preparation.tools
        assembled = preparation.assembled
        prompt = preparation.prompt
        reserved = preparation.reserved
        try:
            provider_result = normalize_decision_result(
                await self.host.provider.decide(
                    system=prompt.content,
                    context=assembled.content,
                    tools=tools,
                    prior_turns=state.get("provider_turns", []),
                    trace_metadata=self.host._trace(prompt.content_hash, state),
                )
            )
        except Exception as exc:
            error_code = self.host._provider_failure_error_code(exc)
            await self.host._persist_context_ledger(
                state,
                reserved,
                component_manifest=self.host._provider_component_manifest(
                    assembled,
                    tools=tools,
                    node=(
                        "action_synthesis"
                        if state.get("obligation_synthesis_mode")
                        else "agent_decide"
                    ),
                ),
                transport=None,
                require_capture=False,
            )
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code=error_code,
                provider_transport_attempts=self.host._exception_transport_attempts(exc),
            )
            return await safe_stop(self.host, state, "provider_failed", error_code=error_code)
        context_ledger_id = await self.host._persist_context_ledger(
            state,
            reserved,
            component_manifest={
                **self.host._provider_component_manifest(
                    assembled,
                    tools=tools,
                    node=(
                        "action_synthesis"
                        if state.get("obligation_synthesis_mode")
                        else "agent_decide"
                    ),
                ),
                "ordered_membership_root_hash": preparation.membership_root_hash,
                "observation_membership_root_hash": (preparation.observation_membership_root_hash),
            },
            transport=provider_result.transport,
            ledger_id=preparation.context_ledger_id_hint,
            binding_plans=preparation.binding_plans,
        )
        raw_envelope = await self.host._persist_raw_provider_decision(
            state, reserved, provider_result.output
        )
        return ProviderDecisionCall(
            preparation=preparation,
            result=provider_result,
            context_ledger_id=context_ledger_id,
            raw_envelope=raw_envelope,
        )

    async def _validate_provider_decision(
        self,
        provider_call: ProviderDecisionCall,
    ) -> ValidatedProviderDecision | AgentState:
        preparation = provider_call.preparation
        state = preparation.state
        provider_result = provider_call.result
        evidence = preparation.evidence
        evidence_lineage = preparation.evidence_lineage
        context_observations = preparation.context_observations
        context_ledger_id = provider_call.context_ledger_id
        reserved = preparation.reserved
        assembled = preparation.assembled
        tools = preparation.tools
        prompt = preparation.prompt
        provider_attempt_id = preparation.provider_attempt_id
        raw_envelope = provider_call.raw_envelope
        parsed_envelope = raw_envelope
        terminal_repaired = False
        evidence_decision = preparation.evidence_decision
        decision_tools = tools
        decision_node = (
            "action_synthesis" if state.get("obligation_synthesis_mode") else "agent_decide"
        )
        try:
            decision = self.host._parse_raw_provider_decision(provider_result.output)
            reference_error_paths = self.host._terminal_reference_error_paths(
                decision,
                evidence=evidence,
                observations=context_observations,
            )
            if reference_error_paths:
                if provider_result.transport is None:
                    raise RuntimeConflict("provider_transport_missing")
                raise ProviderStructuredOutputError(
                    error_paths=tuple(reference_error_paths),
                    transport=provider_result.transport,
                    usage=provider_result.usage,
                    transport_attempts=provider_result.transport_attempts,
                )
        except Exception as exc:
            if raw_envelope is not None:
                raw_envelope.intake_status = "rejected"
                raw_envelope.rejection_code = f"invalid:{type(exc).__name__}"[:100]
            (
                rejected_observations,
                rejected_attempts,
                rejected_rounds,
            ) = await self.host._reject_raw_tool_batch(state, provider_result.output)
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code=f"provider_decision_invalid:{type(exc).__name__}",
                prompt_tokens=provider_result.usage.prompt_tokens,
                completion_tokens=provider_result.usage.completion_tokens,
                provider_transport_attempts=provider_result.transport_attempts,
                structured_error_paths=self.host._decision_error_paths(exc),
            )
            repair_result = None
            if (
                not provider_result.output.tool_calls
                and isinstance(
                    exc,
                    (
                        json.JSONDecodeError,
                        ProviderStructuredOutputError,
                        ValidationError,
                        ValueError,
                    ),
                )
                and not state.get("structure_repair_used")
                and state["llm_calls"] + 2 <= MAX_LLM_CALLS
            ):
                repair_result = await DecisionRepair(cast(DecisionRepairHost, self.host)).repair(
                    state,
                    original_attempt_id=(
                        reserved[1].id if reserved is not None else new_id("attempt")
                    ),
                    parse_error=exc,
                    prompt_hash=prompt.content_hash,
                    tools=tools,
                    evidence_lineage=evidence_lineage,
                    context_observations=context_observations,
                )
            if isinstance(repair_result, str):
                stopped = await safe_stop(
                    self.host,
                    state,
                    "provider_failed",
                    error_code=repair_result,
                )
                stopped["llm_calls"] = state["llm_calls"] + 2
                stopped["structure_repair_used"] = True
                return stopped
            if repair_result is None:
                stopped = await safe_stop(
                    self.host,
                    state,
                    (
                        "provider_terminal_schema_invalid"
                        if not provider_result.output.tool_calls
                        else "provider_decision_invalid"
                    ),
                )
                stopped["llm_calls"] = (
                    state["llm_calls"]
                    + 1
                    + int(
                        not provider_result.output.tool_calls
                        and not state.get("structure_repair_used")
                        and state["llm_calls"] + 2 <= MAX_LLM_CALLS
                    )
                )
                stopped["structure_repair_used"] = bool(
                    state.get("structure_repair_used") or not provider_result.output.tool_calls
                )
                if rejected_observations:
                    stopped["latest_observations"] = rejected_observations
                    stopped["tool_observations"] = [
                        *state.get("tool_observations", []),
                        *rejected_observations,
                    ]
                    stopped["tool_rounds"] = state["tool_rounds"] + rejected_rounds
                    stopped["tool_attempts"] = state["tool_attempts"] + rejected_attempts
                return stopped
            (
                decision,
                provider_result,
                evidence,
                context_ledger_id,
                reserved,
                parsed_envelope,
                assembled,
                decision_tools,
                decision_node,
            ) = repair_result
            provider_attempt_id = reserved[1].id if reserved is not None else provider_attempt_id
            evidence_decision = self._decide_pre_candidate_evidence(
                state,
                evidence=evidence,
                observations=list(state.get("tool_observations", [])),
                provider_attempt_id=provider_attempt_id,
            )
            terminal_repaired = True
        else:
            if parsed_envelope is not None:
                parsed_envelope.intake_status = "parsed"
            await self.host._finish_external(
                reserved,
                status="succeeded",
                prompt_tokens=provider_result.usage.prompt_tokens,
                completion_tokens=provider_result.usage.completion_tokens,
                provider_transport_attempts=provider_result.transport_attempts,
            )
        if parsed_envelope is not None:
            parsed_envelope.intake_status = "parsed"
        return ValidatedProviderDecision(
            state=state,
            decision=decision,
            provider_result=provider_result,
            evidence=evidence,
            evidence_lineage=evidence_lineage,
            context_observations=context_observations,
            context_ledger_id=context_ledger_id,
            reserved=reserved,
            parsed_envelope=parsed_envelope,
            assembled=assembled,
            decision_tools=decision_tools,
            decision_node=decision_node,
            provider_attempt_id=provider_attempt_id,
            original_tools=tools,
            terminal_repaired=terminal_repaired,
            evidence_decision=evidence_decision,
        )

    async def _normalize_provider_decision(
        self,
        validated: ValidatedProviderDecision,
    ) -> NormalizedProviderDecision | AgentState:
        state = validated.state
        decision = validated.decision
        provider_result = validated.provider_result
        evidence = validated.evidence
        calls = state["llm_calls"] + (2 if validated.terminal_repaired else 1)
        if state.get("obligation_synthesis_mode") and decision.decision_type == "tool_calls":
            rejected, rejected_attempts, rejected_rounds = await self.host._reject_raw_tool_batch(
                state, provider_result.output
            )
            await self.host._event(
                state,
                "semantic_no_progress",
                {
                    "reason_code": "tool_call_after_tools_empty_synthesis",
                    "rejected_tool_count": len(rejected),
                },
                visibility="customer",
                status="failed",
            )
            stopped = await safe_stop(self.host, state, "semantic_no_progress")
            stopped.update(
                {
                    "latest_observations": rejected,
                    "tool_observations": [
                        *state.get("tool_observations", []),
                        *rejected,
                    ],
                    "llm_calls": calls,
                    "tool_rounds": state["tool_rounds"] + rejected_rounds,
                    "tool_attempts": state["tool_attempts"] + rejected_attempts,
                    "obligation_synthesis_mode": True,
                }
            )
            return stopped
        decision = self.host._normalize_terminal_decision(decision)
        decision, knowledge_query_grounded = self.host._ground_policy_follow_up_query(
            state, decision
        )
        (
            decision,
            action_read_arguments_canonicalized,
        ) = self.host._canonicalize_action_read_arguments(state, decision)
        claim_binding_count_before = sum(
            len(claim.citation_binding_ids)
            for claim in (
                decision.candidate.material_claims if decision.candidate is not None else []
            )
        )
        decision = self.host._canonicalize_candidate_references(decision, evidence)
        claim_binding_count_after = sum(
            len(claim.citation_binding_ids)
            for claim in (
                decision.candidate.material_claims if decision.candidate is not None else []
            )
        )
        redundant_claim_source_bindings_removed = max(
            0,
            claim_binding_count_before - claim_binding_count_after,
        )
        AGENT_DECISIONS.labels(decision_type=decision.decision_type).inc()
        turns = list(state.get("provider_turns", []))
        if decision.decision_type == "tool_calls":
            turns.append(self.host._assistant_tool_turn(decision))
        return NormalizedProviderDecision(
            validated=validated,
            decision=decision,
            calls=calls,
            knowledge_query_grounded=knowledge_query_grounded,
            action_read_arguments_canonicalized=action_read_arguments_canonicalized,
            redundant_claim_source_bindings_removed=(redundant_claim_source_bindings_removed),
            provider_turns=turns,
        )

    async def _publish_provider_decision(
        self,
        normalized: NormalizedProviderDecision,
    ) -> AgentState:
        validated = normalized.validated
        state = validated.state
        decision = normalized.decision
        evidence = validated.evidence
        decision_tools = validated.decision_tools
        assembled = validated.assembled
        decision_node = validated.decision_node
        reserved = validated.reserved
        provider_attempt_id = validated.provider_attempt_id
        context_ledger_id = validated.context_ledger_id
        tools = validated.original_tools
        calls = normalized.calls
        knowledge_query_grounded = normalized.knowledge_query_grounded
        action_read_arguments_canonicalized = normalized.action_read_arguments_canonicalized
        redundant_claim_source_bindings_removed = normalized.redundant_claim_source_bindings_removed
        turns = normalized.provider_turns
        await self.host._event(
            state,
            "agent_decision",
            {
                "decision_type": decision.decision_type,
                "decision_summary": decision.decision_summary,
                "tool_names": [item.call.name for item in decision.tool_calls],
                "knowledge_query_canonicalized": knowledge_query_grounded,
                "knowledge_query_grounded_to_current_turn": bool(
                    knowledge_query_grounded
                    and state.get("classification", {}).get("issue_type") == "billing_refund"
                ),
                **self.host._topic_continuity_event_payload(state, decision),
                "action_read_arguments_canonicalized": (action_read_arguments_canonicalized),
                "redundant_claim_source_bindings_removed": (
                    redundant_claim_source_bindings_removed
                ),
                "injected_tool_allowlist": [
                    str(item.get("function", {}).get("name", "")) for item in decision_tools
                ],
                "context_manifest": self.host._provider_component_manifest(
                    assembled,
                    tools=decision_tools,
                    node=decision_node,
                ),
                "llm_calls": calls,
                "remaining_budget": {
                    "llm_calls": max(0, MAX_LLM_CALLS - calls),
                    "tool_rounds": max(0, MAX_TOOL_ROUNDS - state["tool_rounds"]),
                    "tool_attempts": max(0, MAX_TOOL_ATTEMPTS - state["tool_attempts"]),
                },
                "evidence_decision": {
                    "result": validated.evidence_decision.result,
                    "sufficient": validated.evidence_decision.sufficient,
                    "required_groups": list(
                        validated.evidence_decision.requirements.required_groups
                    ),
                    "error_code": validated.evidence_decision.error_code,
                },
            },
            visibility="customer",
        )
        update: AgentState = {
            "agent_decision": decision.model_dump(mode="json"),
            "provider_turns": turns,
            "llm_calls": calls,
            "step_index": state.get("step_index", 0) + 1,
            "latest_provider_attempt_id": (
                reserved[1].id if reserved is not None else provider_attempt_id
            ),
            "latest_context_ledger_id": context_ledger_id or "",
            "structure_repair_used": bool(
                state.get("structure_repair_used", False) or validated.terminal_repaired
            ),
            "evidence_replan_required": False,
            "evidence_decision": validated.evidence_decision.model_dump(mode="json"),
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
        }
        admission = state.get("action_admission", {})
        premature_action_candidate = bool(
            decision.candidate is not None
            and admission.get("schema_version") == "action-admission.v2"
            and admission.get("status") == "admitted"
            and not state.get("obligation_synthesis_mode")
        )
        if decision.candidate is not None and not premature_action_candidate:
            used_binding_ids = {
                binding_id
                for claim in decision.candidate.material_claims
                for binding_id in claim.citation_binding_ids
            }
            update["context_citation_bindings"] = [
                {
                    "citation_binding_id": str(item["citation_binding_id"]),
                    "provider_attempt_id": (
                        reserved[1].id if reserved is not None else provider_attempt_id
                    ),
                    "evidence_id": str(item.get("evidence_id") or ""),
                    "document_id": str(item.get("document_id") or ""),
                    "chunk_id": str(item.get("chunk_id") or ""),
                    "content_hash": str(item.get("content_hash") or ""),
                    "locator_hash": str(item.get("source_locator_hash") or ""),
                }
                for item in evidence
                if item.get("citation_binding_id") in used_binding_ids
            ]
        if decision.decision_type == "tool_calls":
            turn_binding = await open_tool_turn(
                self.host,
                state,
                decision,
                self.host._provider_component_manifest(
                    assembled,
                    tools=tools,
                    node="agent_decide",
                ),
            )
            update.update(turn_binding)
        if decision.candidate is not None and not premature_action_candidate:
            update["candidate"] = decision.candidate.model_dump(mode="json")
            update["agent_finish_reason"] = self.host._finish_reason(decision)
        elif premature_action_candidate:
            # A Provider terminal candidate cannot waive the deterministic
            # evidence contract for an admitted high-risk action. Give the
            # Provider one bounded corrective decision with the exact remaining
            # capability groups. A repeated terminal candidate is semantic
            # no-progress and fails closed instead of consuming the remaining
            # LLM budget in an unproductive loop.
            ledger_payload = state.get("action_obligation_ledger", {})
            missing_groups = (
                list(ActionObligationLedger.model_validate(ledger_payload).unsatisfied_capabilities)
                if ledger_payload
                else []
            )
            replan_count = int(state.get("evidence_replan_count", 0)) + 1
            assessment = EvidenceAssessment(
                sufficient=False,
                required_groups=missing_groups,
                missing_groups=missing_groups,
                result="replan",
                error_code="premature_action_candidate",
            )
            update["candidate"] = {}
            update["agent_finish_reason"] = ""
            update["evidence_assessment"] = assessment.model_dump(mode="json")
            update["evidence_replan_required"] = True
            update["evidence_replan_count"] = replan_count
            if replan_count > 1:
                await self.host._event(
                    state,
                    "provider_decision_no_progress",
                    {
                        "reason_code": "premature_action_candidate",
                        "missing_evidence_groups": missing_groups,
                        "corrective_decisions": replan_count,
                    },
                    status="failed",
                )
                merged_state = cast(AgentState, {**state, **update})
                update.update(await safe_stop(self.host, merged_state, "semantic_no_progress"))
                return update
            await self.host._event(
                state,
                "provider_decision_rejected",
                {
                    "reason_code": "premature_action_candidate",
                    "missing_evidence_groups": missing_groups,
                    "corrective_decisions_remaining": 1,
                },
            )
        return update

    def route_decision(self, state: AgentState) -> str:
        decision = AgentDecision.model_validate(state["agent_decision"])
        if decision.decision_type == "tool_calls":
            return "tools"
        if state.get("safe_stop_reason"):
            return "policy"
        policy_boundary = str(state.get("classification", {}).get("policy_boundary", "allowed"))
        support_subject = str(
            state.get("classification", {}).get("support_subject", "customer_problem")
        )
        # Deterministic terminal boundaries remain terminal even if an action
        # phrase was also extracted. They never rely on Provider authority.
        if (
            policy_boundary in {"out_of_scope", "prohibited"}
            or self.host._trusted_platform_answer(support_subject) is not None
        ):
            return "policy"
        admission = state.get("action_admission", {})
        if (
            admission.get("schema_version") == "action-admission.v2"
            and admission.get("status") == "admitted"
        ):
            return "obligations"
        if (
            state.get("candidate")
            and state.get("agent_finish_reason")
            and not state.get("evidence_replan_required")
        ):
            return "policy"
        return "policy"
