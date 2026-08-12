from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.constants import (
    CLASSIFICATION_HISTORY_CHARACTER_BUDGET,
    MAX_CLASSIFICATION_HISTORY_MESSAGES,
    MAX_LLM_CALLS,
)
from supportguard.agent.context import ContextBudgetExceeded
from supportguard.agent.conversation_semantics import (
    canonicalize_non_material_classification,
    resolve_action_state_query,
)
from supportguard.agent.nodes.finalization import SafeStopHost, safe_stop
from supportguard.agent.schemas import Classification
from supportguard.agent.state import AgentState
from supportguard.config import Settings
from supportguard.contracts.action_preconditions import (
    ActionAdmission,
    ActionAdmissionV2,
    explicit_action_with_immediate_domain,
    resolve_action_admission_v2,
    resolve_missing_action_preconditions,
)
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.context import worker_execution_context
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    TicketMessage,
    TicketSummary,
    new_id,
)
from supportguard.policies.pii import redact_pii
from supportguard.prompts.registry import PromptAsset, load_prompt
from supportguard.providers.base import (
    ProviderCallResult,
    StructuredProvider,
    normalize_provider_result,
)
from supportguard.providers.deepseek import ProviderStructuredOutputError
from supportguard.services.conversation_action_state import (
    ConversationActionStateProjectionError,
    ConversationActionStateProjector,
    ConversationActionStateV1,
    conversation_action_sources_from_mapping,
    project_conversation_action_state,
)
from supportguard.services.runtime_jobs import RuntimeConflict


class IntakeNodeHost(SafeStopHost, Protocol):
    provider: StructuredProvider
    session: AsyncSession | None
    history_loader: Any
    settings: Settings

    def _canonical_action_query_classification(self, *args: Any, **kwargs: Any) -> Any: ...
    def _decision_error_paths(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _event(self, *args: Any, **kwargs: Any) -> Any: ...
    def _exception_transport_attempts(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _finish_external(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _persist_context_ledger(self, *args: Any, **kwargs: Any) -> str | None: ...
    def _provider_failure_error_code(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_external(self, *args: Any, **kwargs: Any) -> Any: ...
    def _resolve_existing_action_replay(self, *args: Any, **kwargs: Any) -> Any: ...
    def _trace(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _ClassificationPreparation:
    """Bounded, non-authoritative inputs for one classification attempt."""

    prompt: PromptAsset
    context: list[dict[str, Any]]
    omissions: list[dict[str, str]]
    history_manifest: dict[str, Any]
    action_state_query: dict[str, Any] | None
    missing_action_admission: ActionAdmission | None
    provider_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IntakeNodes:
    """Own ingress, classification, action admission and bounded history loading."""

    host: IntakeNodeHost

    async def ingest_ticket(self, state: AgentState) -> AgentState:
        durable_counts = {
            "llm_calls": 0,
            "tool_rounds": 0,
            "tool_attempts": 0,
        }
        durable_repair_used = False
        if self.host.session is not None and state.get("run_id"):
            run = await self.host.session.get(AgentRun, state["run_id"])
            if run is not None:
                durable_counts = {
                    "llm_calls": run.llm_calls,
                    "tool_rounds": run.tool_rounds,
                    "tool_attempts": run.tool_attempts,
                }
                durable_repair_used = (
                    await self.host.session.scalar(
                        select(AgentCallAttempt.id).where(
                            AgentCallAttempt.run_id == state["run_id"],
                            AgentCallAttempt.call_kind == "structure_repair",
                        )
                    )
                    is not None
                )
        update: AgentState = {
            "llm_calls": max(int(state.get("llm_calls", 0)), durable_counts["llm_calls"]),
            "tool_rounds": max(int(state.get("tool_rounds", 0)), durable_counts["tool_rounds"]),
            "tool_attempts": max(
                int(state.get("tool_attempts", 0)), durable_counts["tool_attempts"]
            ),
            "tool_observations": list(state.get("tool_observations", [])),
            "latest_observations": [],
            "evidence": list(state.get("evidence", [])),
            "evidence_conflict": bool(state.get("evidence_conflict", False)),
            "knowledge_comparison_requested": bool(
                state.get("knowledge_comparison_requested", False)
            ),
            "knowledge_comparison_complete": bool(
                state.get("knowledge_comparison_complete", False)
            ),
            "executed_fingerprints": list(state.get("executed_fingerprints", [])),
            "provider_turns": list(state.get("provider_turns", [])),
            "step_index": int(state.get("step_index", 0)),
            "structure_repair_used": bool(
                state.get("structure_repair_used", False) or durable_repair_used
            ),
            "evidence_assessment": dict(state.get("evidence_assessment", {})),
            "evidence_replan_required": bool(state.get("evidence_replan_required", False)),
            "evidence_replan_count": int(state.get("evidence_replan_count", 0)),
            "context_citation_bindings": list(state.get("context_citation_bindings", [])),
            "obligation_synthesis_mode": False,
        }
        await self.host._event(state, "run_activated", {"checkpoint_stage": "request_created"})
        return update

    async def redact(self, state: AgentState) -> AgentState:
        result = redact_pii(state["user_message"])
        ingress_count = int(state.get("ingress_redaction_count", 0))
        rule_ids = list(
            dict.fromkeys([*state.get("redaction_rule_ids", []), *result.applied_rule_ids])
        )
        await self.host._event(
            state,
            "pii_redacted",
            {
                "ingress_redaction_count": ingress_count,
                "graph_additional_redaction_count": result.redaction_count,
            },
            visibility="customer",
        )
        return {
            "redacted_message": result.text,
            "redaction_count": ingress_count + result.redaction_count,
            "ingress_redaction_count": ingress_count,
            "graph_additional_redaction_count": result.redaction_count,
            "redaction_rule_ids": rule_ids,
        }

    async def load_conversation_action_state(self, state: AgentState) -> AgentState:
        """Fail closed before classification when trusted action truth is invalid."""

        try:
            return await self._load_conversation_action_state(state)
        except (
            ConversationActionStateProjectionError,
            RuntimeConflict,
            TypeError,
            ValidationError,
            ValueError,
        ):
            return await safe_stop(
                self.host,
                state,
                "action_state_unavailable",
                error_code="conversation_action_state_invalid",
            )

    async def _load_conversation_action_state(self, state: AgentState) -> AgentState:
        """Load the current, customer-scoped action truth before classification.

        Production always re-reads the query-time projection. A caller-supplied
        projection is accepted only by the sessionless fixture path and still
        passes the same strict schema validation.
        """

        if self.host.session is None:
            projections = tuple(
                ConversationActionStateV1.model_validate(item)
                for item in state.get("current_actions", [])
            )
        elif (
            self.host.session.get_bind().dialect.name == "postgresql"
            and await self.host.session.scalar(text("SELECT session_user")) == "supportguard_worker"
        ):
            execution = worker_execution_context.get()
            snapshot = await self.host.session.scalar(
                text("SELECT supportguard_worker_claim_job(:job_id,:owner)"),
                {
                    "job_id": execution.job_id,
                    "owner": execution.executor_service_principal,
                },
            )
            if (
                not isinstance(snapshot, dict)
                or str(snapshot.get("job_id", "")) != execution.job_id
                or str(snapshot.get("run_id", "")) != execution.run_id
                or str(snapshot.get("ticket_id", "")) != execution.ticket_id
                or str(snapshot.get("tenant_id", "")) != execution.tenant_id
                or int(snapshot.get("fencing_token", -1)) != execution.fencing_token
            ):
                raise RuntimeConflict("conversation action state lease is stale")
            source_bundles = snapshot.get("conversation_action_sources")
            if not isinstance(source_bundles, list):
                raise RuntimeConflict("conversation action state capability is unavailable")
            if any(not isinstance(item, dict) for item in source_bundles):
                raise RuntimeConflict(
                    "conversation action state capability returned an invalid bundle"
                )
            projections = tuple(
                project_conversation_action_state(conversation_action_sources_from_mapping(item))
                for item in source_bundles
            )
        else:
            projections = await ConversationActionStateProjector(self.host.session).list_for_ticket(
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                ticket_id=state["ticket_id"],
            )
        projections = tuple(
            sorted(
                projections,
                key=lambda item: item.updated_at,
                reverse=True,
            )
        )
        current_actions = [item.model_dump(mode="json") for item in projections]
        await self.host._event(
            state,
            "conversation_action_state_loaded",
            {
                "schema_version": "conversation-action-state.v1",
                "action_count": len(current_actions),
                "approval_ids": [item["approval_id"] for item in current_actions],
                "projection_statuses": [item["projection_status"] for item in current_actions],
                "grants_action_authority": False,
            },
        )
        return {
            "current_actions": current_actions,
            # A checkpoint from an earlier execution must not select an action
            # for a new customer message before that message is interpreted.
            "action_state_query": {},
        }

    async def classify(self, state: AgentState) -> AgentState:
        if state.get("candidate") and state.get("safe_stop_reason"):
            return {}
        if state["llm_calls"] >= MAX_LLM_CALLS:
            return await safe_stop(self.host, state, "llm_call_budget_exhausted")
        try:
            prepared = await self._prepare_classification(state)
        except ContextBudgetExceeded:
            return await safe_stop(
                self.host,
                state,
                "context_budget_exhausted",
                error_code="protected_classification_context_oversized",
            )
        return await self._classify_with_provider(state, prepared)

    async def _prepare_classification(self, state: AgentState) -> _ClassificationPreparation:
        prompt = load_prompt("classify", version="v2")
        context, omissions = await self._select_classification_context(state)
        history_manifest = self._classification_history_manifest(context, omissions)
        action_state_query = resolve_action_state_query(
            state["redacted_message"],
            state.get("current_actions", []),
            recent_action_approval_id=self._recent_action_message_approval_id(context),
        )
        missing_action_admission = (
            None
            if action_state_query is not None
            else resolve_missing_action_preconditions(state["redacted_message"], context)
        )
        provider_input = {
            "current_turn": state["redacted_message"],
            "recent_conversation": context,
            "trusted_current_actions": state.get("current_actions", []),
            "current_actions_grant_action_authority": False,
        }
        return _ClassificationPreparation(
            prompt=prompt,
            context=context,
            omissions=omissions,
            history_manifest=history_manifest,
            action_state_query=action_state_query,
            missing_action_admission=missing_action_admission,
            provider_input=provider_input,
        )

    async def _publish_missing_action_admission(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
    ) -> AgentState:
        admission = prepared.missing_action_admission
        if admission is None:
            raise RuntimeError("missing action admission stage requires an admission")
        result = Classification(
            issue_type=admission.issue_type,
            risk="high",
            policy_boundary="allowed",
            requested_action=admission.action_type,
            requested_concurrency_limit=None,
            needs_realtime_facts=True,
            support_subject="customer_problem",
            rationale=(
                "Deterministic admission identified an explicit high-risk action "
                "with a missing typed precondition."
            ),
        )
        await self.host._event(
            state,
            "classification",
            {
                "classification": result.model_dump(mode="json"),
                "llm_calls": state["llm_calls"],
                "deterministic_action_admission": admission.model_dump(mode="json"),
                "classification_history": prepared.history_manifest,
            },
            visibility="customer",
        )
        return {
            "classification": result.model_dump(mode="json"),
            "classification_context": prepared.context,
            "classification_context_omissions": prepared.omissions,
            "action_admission": admission.model_dump(mode="json"),
            "llm_calls": state["llm_calls"],
            "structure_repair_used": bool(state.get("structure_repair_used", False)),
        }

    async def _classify_with_provider(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
    ) -> AgentState:
        reserved = await self.host._reserve_external(state, "llm")
        try:
            provider_result = normalize_provider_result(
                await self.host.provider.generate(
                    system=prepared.prompt.content,
                    user=json.dumps(prepared.provider_input, ensure_ascii=False),
                    output_schema=Classification,
                    trace_metadata=self.host._trace(prepared.prompt.content_hash, state),
                )
            )
        except ProviderStructuredOutputError as exc:
            return await self._repair_structured_classification(
                state,
                prepared,
                reserved,
                exc,
            )
        except Exception as exc:
            return await self._stop_after_provider_failure(state, prepared, reserved, exc)
        context_ledger_id = await self._finish_successful_classification_attempt(
            state,
            prepared,
            reserved,
            provider_result,
            component_manifest={
                "node": "classify",
                "prompt_hash": prepared.prompt.content_hash,
                "classification_history": prepared.history_manifest,
            },
        )
        return await self._publish_provider_classification(
            state,
            prepared,
            provider_result,
            reservation=reserved,
            context_ledger_id=context_ledger_id,
            calls=state["llm_calls"] + provider_result.attempts,
            structure_repair_used=bool(state.get("structure_repair_used", False)),
            repaired_this_attempt=False,
        )

    async def _repair_structured_classification(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        initial_reservation: Any,
        failure: ProviderStructuredOutputError,
    ) -> AgentState:
        error_paths = self.host._decision_error_paths(failure)
        await self._record_initial_structure_failure(
            state,
            prepared,
            initial_reservation,
            failure,
            error_paths,
        )
        first_attempt_id = (
            initial_reservation[1].id if initial_reservation is not None else new_id("attempt")
        )
        if state.get("structure_repair_used") or state["llm_calls"] + 2 > MAX_LLM_CALLS:
            return await self._stop_after_unrepairable_structure_failure(state, prepared)
        repair_reservation = await self.host._reserve_external(
            state,
            "structure_repair",
            repair_of_attempt_id=first_attempt_id,
        )
        try:
            provider_result = await self._request_structure_repair(
                state,
                prepared,
                first_attempt_id,
                error_paths,
            )
        except Exception as repair_failure:
            return await self._stop_after_repair_failure(
                state,
                prepared,
                repair_reservation,
                first_attempt_id,
                error_paths,
                repair_failure,
            )
        context_ledger_id = await self._finish_successful_classification_attempt(
            state,
            prepared,
            repair_reservation,
            provider_result,
            component_manifest={
                "node": "classify",
                "repair_of_attempt_id": first_attempt_id,
                "error_paths": error_paths,
                "classification_history": prepared.history_manifest,
            },
        )
        return await self._publish_provider_classification(
            state,
            prepared,
            provider_result,
            reservation=repair_reservation,
            context_ledger_id=context_ledger_id,
            calls=state["llm_calls"] + 2,
            structure_repair_used=True,
            repaired_this_attempt=True,
        )

    async def _record_initial_structure_failure(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        reservation: Any,
        failure: ProviderStructuredOutputError,
        error_paths: list[str],
    ) -> None:
        await self.host._persist_context_ledger(
            state,
            reservation,
            component_manifest={
                "node": "classify",
                "prompt_hash": prepared.prompt.content_hash,
                "intake": "strict_schema_rejected",
                "classification_history": prepared.history_manifest,
            },
            transport=failure.transport,
        )
        await self.host._finish_external(
            reservation,
            status="failed",
            prompt_tokens=failure.usage.prompt_tokens,
            completion_tokens=failure.usage.completion_tokens,
            error_code="provider_structured_output_invalid",
            provider_transport_attempts=failure.transport_attempts,
            structured_error_paths=error_paths,
        )

    async def _request_structure_repair(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        first_attempt_id: str,
        error_paths: list[str],
    ) -> ProviderCallResult[Classification]:
        repair_system = (
            "Repair one classification object to the supplied strict schema. "
            "Return only the complete object. Do not add facts, prose, or tool calls."
        )
        repair_user = json.dumps(
            {
                "error_paths": error_paths,
                "same_redacted_context": prepared.provider_input,
            },
            ensure_ascii=False,
        )
        return normalize_provider_result(
            await self.host.provider.generate(
                system=repair_system,
                user=repair_user,
                output_schema=Classification,
                trace_metadata={
                    **self.host._trace(prepared.prompt.content_hash, state),
                    "repair_of_attempt_id": first_attempt_id,
                },
            )
        )

    async def _stop_after_unrepairable_structure_failure(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
    ) -> AgentState:
        stopped = await safe_stop(self.host, state, "provider_terminal_schema_invalid")
        stopped["llm_calls"] = state["llm_calls"] + 1
        return self._attach_classification_context(stopped, prepared)

    async def _stop_after_repair_failure(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        reservation: Any,
        first_attempt_id: str,
        initial_error_paths: list[str],
        failure: Exception,
    ) -> AgentState:
        if isinstance(failure, ProviderStructuredOutputError):
            transport = failure.transport
            error_code = "provider_terminal_schema_invalid"
            error_paths = self.host._decision_error_paths(failure)
        else:
            transport = None
            error_code = self.host._provider_failure_error_code(failure)
            error_paths = []
        await self.host._persist_context_ledger(
            state,
            reservation,
            component_manifest={
                "node": "classify",
                "repair_of_attempt_id": first_attempt_id,
                "error_paths": initial_error_paths,
                "structured_error_paths": error_paths,
                "classification_history": prepared.history_manifest,
            },
            transport=transport,
            require_capture=False,
        )
        await self.host._finish_external(
            reservation,
            status="failed",
            error_code=error_code,
            provider_transport_attempts=self.host._exception_transport_attempts(failure),
            structured_error_paths=(error_paths or None),
        )
        stopped = await safe_stop(
            self.host,
            state,
            (
                "provider_terminal_schema_invalid"
                if error_code == "provider_terminal_schema_invalid"
                else "provider_failed"
            ),
            error_code=error_code,
        )
        stopped["llm_calls"] = state["llm_calls"] + 2
        stopped["structure_repair_used"] = True
        return self._attach_classification_context(stopped, prepared)

    async def _stop_after_provider_failure(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        reservation: Any,
        failure: Exception,
    ) -> AgentState:
        error_code = self.host._provider_failure_error_code(failure)
        await self.host._persist_context_ledger(
            state,
            reservation,
            component_manifest={
                "node": "classify",
                "prompt_hash": prepared.prompt.content_hash,
                "classification_history": prepared.history_manifest,
            },
            transport=None,
            require_capture=False,
        )
        await self.host._finish_external(
            reservation,
            status="failed",
            error_code=error_code,
            provider_transport_attempts=self.host._exception_transport_attempts(failure),
        )
        stopped = await safe_stop(
            self.host,
            state,
            "provider_failed",
            error_code=error_code,
        )
        return self._attach_classification_context(stopped, prepared)

    async def _finish_successful_classification_attempt(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        reservation: Any,
        provider_result: ProviderCallResult[Classification],
        *,
        component_manifest: dict[str, Any],
    ) -> str | None:
        context_ledger_id = await self.host._persist_context_ledger(
            state,
            reservation,
            component_manifest=component_manifest,
            transport=provider_result.transport,
        )
        await self.host._finish_external(
            reservation,
            status="succeeded",
            prompt_tokens=provider_result.usage.prompt_tokens,
            completion_tokens=provider_result.usage.completion_tokens,
            provider_transport_attempts=provider_result.transport_attempts,
        )
        return context_ledger_id

    async def _publish_provider_classification(
        self,
        state: AgentState,
        prepared: _ClassificationPreparation,
        provider_result: ProviderCallResult[Classification],
        *,
        reservation: Any,
        context_ledger_id: str | None,
        calls: int,
        structure_repair_used: bool,
        repaired_this_attempt: bool,
    ) -> AgentState:
        provider_classification = provider_result.output
        action_state_query = prepared.action_state_query
        if action_state_query is None:
            action_state_query = self.host._resolve_existing_action_replay(
                state["redacted_message"],
                provider_classification,
                state.get("current_actions", []),
            )
        missing_admission = prepared.missing_action_admission
        deterministic_action = explicit_action_with_immediate_domain(
            state["redacted_message"],
            prepared.context,
        )
        if missing_admission is not None:
            # The real Provider still classifies the natural-language turn, but
            # it cannot weaken or fill a deterministic high-risk precondition.
            # The typed admission remains the sole authority for the action and
            # the exact field that must be clarified.
            result = Classification(
                issue_type=missing_admission.issue_type,
                risk="high",
                policy_boundary="allowed",
                requested_action=missing_admission.action_type,
                requested_concurrency_limit=None,
                needs_realtime_facts=True,
                support_subject="customer_problem",
                rationale=(
                    "Deterministic admission identified an explicit high-risk action "
                    "with a missing typed precondition."
                ),
            )
        elif (
            deterministic_action is not None
            and action_state_query is None
            and provider_classification.policy_boundary == "allowed"
        ):
            issue_type = {
                "refund": "billing_refund",
                "api_key_revocation": "credential_security",
                "entitlement_change": "entitlement_change",
            }[deterministic_action]
            result = provider_classification.model_copy(
                update={
                    "issue_type": issue_type,
                    "risk": "high",
                    "requested_action": deterministic_action,
                    # ActionAdmissionV2 owns the typed target and will reject
                    # ambiguity; an untrusted Provider target is not copied.
                    "requested_concurrency_limit": None,
                    "needs_realtime_facts": True,
                }
            )
        else:
            result = self.host._canonical_action_query_classification(
                provider_classification,
                action_state_query=action_state_query,
                current_actions=state.get("current_actions", []),
            )
            if action_state_query is None:
                result = canonicalize_non_material_classification(state["redacted_message"], result)
        event_payload: dict[str, Any] = {
            "classification": result.model_dump(),
            "llm_calls": calls,
            "classification_history": prepared.history_manifest,
        }
        if repaired_this_attempt:
            event_payload["structure_repair_used"] = True
        if action_state_query is not None:
            event_payload.update(
                {
                    "provider_semantic_classification": (
                        provider_classification.model_dump(mode="json")
                    ),
                    "deterministic_action_state_query": action_state_query,
                    "grants_action_authority": False,
                }
            )
        if missing_admission is not None:
            event_payload.update(
                {
                    "provider_semantic_classification": (
                        provider_classification.model_dump(mode="json")
                    ),
                    "deterministic_action_admission": missing_admission.model_dump(mode="json"),
                    "grants_action_authority": False,
                }
            )
        elif deterministic_action is not None and result.requested_action == deterministic_action:
            event_payload.update(
                {
                    "provider_semantic_classification": (
                        provider_classification.model_dump(mode="json")
                    ),
                    "deterministic_current_action": deterministic_action,
                    "grants_action_authority": False,
                }
            )
        await self.host._event(state, "classification", event_payload, visibility="customer")
        update: AgentState = {
            "classification": result.model_dump(),
            "llm_calls": calls,
            "structure_repair_used": structure_repair_used,
            "classification_context": prepared.context,
            "classification_context_omissions": prepared.omissions,
            "latest_provider_attempt_id": reservation[1].id if reservation is not None else "",
            "latest_context_ledger_id": context_ledger_id or "",
        }
        if action_state_query is not None:
            update["action_state_query"] = action_state_query
        if missing_admission is not None:
            update["action_admission"] = missing_admission.model_dump(mode="json")
        return update

    @staticmethod
    def _attach_classification_context(
        update: AgentState,
        prepared: _ClassificationPreparation,
    ) -> AgentState:
        update["classification_context"] = prepared.context
        update["classification_context_omissions"] = prepared.omissions
        return update

    async def _load_classification_context(self, state: AgentState) -> list[dict[str, Any]]:
        selected, _omitted = await IntakeNodes._select_classification_context(
            self,
            state,
        )
        return selected

    async def _canonical_current_customer_message(
        self,
        state: AgentState,
    ) -> TicketMessage:
        """Return the Run-bound customer message that defines the causal horizon.

        Multiple customer messages may be accepted before the ticket lane executes
        the earlier Run.  Excluding only ``run.message_id`` would therefore expose
        later, already-persisted messages to that earlier Run.  Every canonical
        history reader must instead bind to this message's monotonic conversation
        sequence and read only strictly earlier rows.
        """

        if self.host.session is None:
            raise ContextBudgetExceeded("canonical current message requires a persisted session")
        current_run = await self.host.session.get(AgentRun, state["run_id"])
        if current_run is None:
            raise ContextBudgetExceeded("canonical current run is missing")
        if (
            current_run.tenant_id != state["tenant_id"]
            or current_run.ticket_id != state["ticket_id"]
            or current_run.customer_id != state["customer_id"]
        ):
            raise ContextBudgetExceeded("canonical current run scope conflicts")
        supplied_message_id = str(state.get("customer_message_id") or "")
        if supplied_message_id and supplied_message_id != current_run.message_id:
            raise ContextBudgetExceeded("canonical current message identity conflicts")
        current_message = await self.host.session.get(
            TicketMessage,
            current_run.message_id,
        )
        if current_message is None:
            raise ContextBudgetExceeded("canonical current message is missing")
        if (
            current_message.tenant_id != state["tenant_id"]
            or current_message.ticket_id != state["ticket_id"]
            or current_message.message_kind != "customer"
            or current_message.role not in {"user", "customer"}
        ):
            raise ContextBudgetExceeded("canonical current message scope conflicts")
        if (
            current_run.turn_id is not None
            and current_message.turn_id is not None
            and current_run.turn_id != current_message.turn_id
        ):
            raise ContextBudgetExceeded("canonical current message turn conflicts")
        if (
            current_message.conversation_sequence is None
            or current_message.conversation_sequence <= 0
        ):
            raise ContextBudgetExceeded(
                "canonical current message is missing conversation sequence"
            )
        return current_message

    async def _select_classification_context(
        self,
        state: AgentState,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Load bounded same-ticket history before classification.

        The projection is deliberately text-only, redacted, and marked historical.
        It may resolve pronouns and a previously supplied opaque resource reference,
        but it carries no authorization, observation freshness, or policy state. Every
        budget omission is returned with the canonical message identity and is later
        persisted in the Provider context ledger. History beyond the explicit hard
        bound fails closed rather than becoming an unrecorded omission.
        """

        # Production always re-reads canonical TicketMessage rows. A supplied
        # list is a sessionless fixture or legacy-checkpoint compatibility path,
        # never a way to replace persisted message identity.
        supplied = state.get("classification_context") if self.host.session is None else None
        legacy_supplied_context = supplied is not None and self.host.session is None
        if supplied is not None:
            rows: list[dict[str, Any]] = supplied
        elif self.host.session is None:
            rows = []
        else:
            current_message = await self._canonical_current_customer_message(state)
            message_rows = list(
                (
                    await self.host.session.scalars(
                        select(TicketMessage)
                        .where(
                            TicketMessage.tenant_id == state["tenant_id"],
                            TicketMessage.ticket_id == state["ticket_id"],
                            TicketMessage.message_kind.in_(
                                ("customer", "assistant", "action_update")
                            ),
                            TicketMessage.conversation_sequence
                            < current_message.conversation_sequence,
                        )
                        .order_by(TicketMessage.conversation_sequence.desc())
                        .limit(MAX_CLASSIFICATION_HISTORY_MESSAGES + 1)
                    )
                ).all()
            )
            if len(message_rows) > MAX_CLASSIFICATION_HISTORY_MESSAGES:
                raise ContextBudgetExceeded("classification history exceeds explicit message bound")
            rows = [
                {
                    "role": (
                        "customer"
                        if item.role in {"user", "customer"}
                        else "action"
                        if item.role == "action" or item.message_kind == "action_update"
                        else "assistant"
                    ),
                    "content": item.content,
                    "message_id": item.id,
                    **(
                        {"approval_id": item.approval_id}
                        if (
                            (item.role == "action" or item.message_kind == "action_update")
                            and item.approval_id
                        )
                        else {}
                    ),
                }
                for item in reversed(message_rows)
            ]
        if len(rows) > MAX_CLASSIFICATION_HISTORY_MESSAGES:
            raise ContextBudgetExceeded("classification history exceeds explicit message bound")
        projected: list[dict[str, Any]] = []
        seen_message_ids: dict[str, str] = {}
        omitted: list[dict[str, str]] = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            role = (
                "customer"
                if item.get("role") in {"user", "customer"}
                else "action"
                if item.get("role") == "action"
                else "assistant"
            )
            content = redact_pii(str(item.get("content", ""))).text.strip()
            if not content:
                continue
            canonical_message_id = item.get("message_id") or item.get("id")
            if (
                canonical_message_id is None
                and item.get("legacy_checkpoint") is not True
                and not legacy_supplied_context
            ):
                raise ContextBudgetExceeded(
                    "classification history message is missing canonical message_id"
                )
            message_id = str(canonical_message_id or f"legacy-classification-message-{index}")
            approval_id = (
                str(item["approval_id"]) if role == "action" and item.get("approval_id") else None
            )
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            identity_hash = hashlib.sha256(
                f"{role}\0{content_hash}\0{approval_id or ''}".encode()
            ).hexdigest()
            previous_identity_hash = seen_message_ids.get(message_id)
            if previous_identity_hash is not None:
                if previous_identity_hash != identity_hash:
                    raise ContextBudgetExceeded(
                        f"classification history message identity conflict: {message_id}"
                    )
                omitted.append(
                    {
                        "section": "classification_history",
                        "message_id": message_id,
                        "reason": "duplicate_message_id",
                    }
                )
                continue
            seen_message_ids[message_id] = identity_hash
            projected.append(
                {
                    "role": role,
                    "content": content,
                    "message_id": message_id,
                    "content_hash": content_hash,
                    "historical": True,
                    "trusted": False,
                    **({"approval_id": approval_id} if approval_id else {}),
                    **(
                        {
                            "legacy_checkpoint": True,
                            "synthetic_message_id": True,
                        }
                        if canonical_message_id is None
                        else {}
                    ),
                }
            )
        IntakeNodes._mark_latest_history_pair(projected)
        protected_indices = {
            index for index, item in enumerate(projected) if item.get("retention") == "latest_pair"
        }
        protected_cost = sum(len(projected[index]["content"]) for index in protected_indices)
        if protected_cost > CLASSIFICATION_HISTORY_CHARACTER_BUDGET:
            raise ContextBudgetExceeded("protected classification history pair exceeds budget")
        selected_indices = set(protected_indices)
        remaining = CLASSIFICATION_HISTORY_CHARACTER_BUDGET - protected_cost
        for index in range(len(projected) - 1, -1, -1):
            if index in selected_indices:
                continue
            cost = len(projected[index]["content"])
            if cost > remaining:
                omitted.append(
                    {
                        "section": "classification_history",
                        "message_id": str(projected[index]["message_id"]),
                        "reason": "classification_budget_older_message",
                    }
                )
                continue
            selected_indices.add(index)
            remaining -= cost
        selected = [item for index, item in enumerate(projected) if index in selected_indices]
        selected_ids = {str(item["message_id"]) for item in selected}
        omitted_ids = {
            item["message_id"]
            for item in omitted
            if item["reason"] == "classification_budget_older_message"
        }
        expected_omitted_ids = {
            str(item["message_id"])
            for item in projected
            if str(item["message_id"]) not in selected_ids
        }
        if omitted_ids != expected_omitted_ids:
            raise ContextBudgetExceeded("classification omission manifest is incomplete")
        return selected, omitted

    @staticmethod
    def _classification_history_manifest(
        selected: list[dict[str, Any]],
        omitted: list[dict[str, str]],
    ) -> dict[str, Any]:
        protected_ids = [
            str(item["message_id"]) for item in selected if item.get("retention") == "latest_pair"
        ]
        return {
            "schema_version": "classification-history-manifest.v1",
            "selected_message_ids": [str(item["message_id"]) for item in selected],
            "protected_latest_pair_message_ids": protected_ids,
            "omitted": [dict(item) for item in omitted],
        }

    @staticmethod
    def _recent_action_message_approval_id(
        classification_context: list[dict[str, Any]],
    ) -> str | None:
        """Return only an adjacent, persisted Action Update conversational referent.

        A bare phrase such as "why was it rejected?" may refer to the immediately
        preceding customer-visible Action Update.  It must not select the only old
        action anywhere on the ticket.  The latest response-like message therefore
        has to be an Action Update carrying its canonical ``approval_id``; an
        intervening assistant response breaks the referent.
        """

        for item in reversed(classification_context):
            role = str(item.get("role") or "")
            if role not in {"assistant", "action"}:
                continue
            if role != "action":
                return None
            approval_id = item.get("approval_id")
            return str(approval_id) if isinstance(approval_id, str) and approval_id else None
        return None

    async def resolve_action_admission(self, state: AgentState) -> AgentState:
        if state.get("candidate") and state.get("safe_stop_reason"):
            return {}
        classification = Classification.model_validate(state["classification"])
        current_message_id = str(state.get("customer_message_id") or f"message:{state['run_id']}")
        conversation_turn_id = str(state.get("conversation_turn_id") or f"turn:{state['run_id']}")
        if self.host.session is not None:
            run = await self.host.session.get(AgentRun, state["run_id"])
            if run is not None:
                current_message_id = run.message_id
                conversation_turn_id = run.turn_id or conversation_turn_id
        is_action_state_query = bool(state.get("action_state_query"))
        continuation_action = (
            None if is_action_state_query else await self._continuation_action(state)
        )
        admission_context = await self._load_admission_context(
            state,
            current_message_id=current_message_id,
        )
        action_state_query = state.get("action_state_query", {})
        if is_action_state_query and action_state_query.get("query_kind") == "repeat_request":
            admission = ActionAdmissionV2(
                status="none",
                planned_action="none",
                issue_type=classification.issue_type,
                source_message_ids=(current_message_id,),
                request_reason=state["redacted_message"],
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                scope_hash=canonical_json_hash(
                    {
                        "customer_id": state["customer_id"],
                        "tenant_id": state["tenant_id"],
                    }
                ),
                classification_version="classification.v2",
                current_message_id=current_message_id,
                turn_group_id=conversation_turn_id,
                reason_code="existing_action_replay_converged",
            )
        else:
            admission = resolve_action_admission_v2(
                state["redacted_message"],
                [] if is_action_state_query else admission_context,
                requested_action=classification.requested_action,
                issue_type=classification.issue_type,
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                current_message_id=current_message_id,
                turn_group_id=conversation_turn_id,
                classification_version="classification.v2",
                requested_concurrency_limit=classification.requested_concurrency_limit,
                continuation_action=continuation_action,
            )
        if is_action_state_query and (
            admission.status != "none" or admission.planned_action != "none"
        ):
            raise RuntimeConflict("conversation action state inquiry attempted action admission")
        await self.host._event(
            state,
            "action_admission_resolved",
            {
                "action_admission": admission.model_dump(mode="json"),
                "continuation_action": continuation_action,
                "conversation_action_state_query": is_action_state_query,
                "grants_action_authority": False,
            },
        )
        if admission.status == "admitted":
            await self.host._event(
                state,
                "action_admitted",
                {
                    "action_type": admission.action_type,
                    "status": admission.status,
                    "reason_code": admission.reason_code,
                    "missing_fields": [],
                },
                visibility="customer",
            )
        return {
            "action_admission": admission.model_dump(mode="json"),
            "customer_message_id": current_message_id,
            "conversation_turn_id": conversation_turn_id,
        }

    async def _load_admission_context(
        self,
        state: AgentState,
        *,
        current_message_id: str,
    ) -> list[dict[str, Any]]:
        if self.host.session is None:
            return [
                item
                for item in state.get("classification_context", [])
                if item.get("role") in {"customer", "user"}
            ][-6:]
        current_message = await self._canonical_current_customer_message(state)
        if current_message.id != current_message_id:
            raise ContextBudgetExceeded("canonical admission message identity conflicts")
        rows = list(
            (
                await self.host.session.scalars(
                    select(TicketMessage)
                    .where(
                        TicketMessage.tenant_id == state["tenant_id"],
                        TicketMessage.ticket_id == state["ticket_id"],
                        TicketMessage.message_kind == "customer",
                        TicketMessage.conversation_sequence < current_message.conversation_sequence,
                    )
                    .order_by(TicketMessage.conversation_sequence.desc())
                    .limit(6)
                )
            ).all()
        )
        return [
            {
                "role": "customer",
                "message_id": item.id,
                "content": redact_pii(item.content).text,
            }
            for item in reversed(rows)
        ]

    async def _continuation_action(self, state: AgentState) -> Any:
        if self.host.session is None:
            return None
        event = await self.host.session.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == state["tenant_id"],
                AgentEvent.ticket_id == state["ticket_id"],
                AgentEvent.run_id != state["run_id"],
                AgentEvent.event_type == "action_admission_resolved",
            )
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        if event is None:
            return None
        payload = event.payload.get("action_admission")
        if not isinstance(payload, dict) or payload.get("status") != "missing":
            return None
        action_type = payload.get("action_type")
        return (
            action_type
            if action_type in {"refund", "api_key_revocation", "entitlement_change"}
            else None
        )

    async def load_history(self, state: AgentState) -> AgentState:
        if state.get("candidate") and state.get("safe_stop_reason"):
            # Classification already produced a typed fail-closed result (for
            # example, the canonical history hard bound was exceeded). Do not
            # turn that terminal result into an uncaught second-stage error.
            return {"relevant_history": []}
        historical_summaries = (
            await self.host.history_loader.load(
                customer_id=state["customer_id"],
                issue_type=str(state.get("classification", {}).get("issue_type", "unknown")),
            )
            if self.host.history_loader is not None and state.get("classification")
            else []
        )
        recent_messages: list[dict[str, Any]] = []
        current_summary: dict[str, Any] | None = None
        if self.host.session is not None:
            current_message = await self._canonical_current_customer_message(state)
            rows = list(
                (
                    await self.host.session.scalars(
                        select(TicketMessage)
                        .where(
                            TicketMessage.tenant_id == state["tenant_id"],
                            TicketMessage.ticket_id == state["ticket_id"],
                            TicketMessage.message_kind.in_(
                                ("customer", "assistant", "action_update")
                            ),
                            TicketMessage.conversation_sequence
                            < current_message.conversation_sequence,
                        )
                        .order_by(TicketMessage.conversation_sequence.desc())
                        .limit(MAX_CLASSIFICATION_HISTORY_MESSAGES + 1)
                    )
                ).all()
            )
            if len(rows) > MAX_CLASSIFICATION_HISTORY_MESSAGES:
                raise ContextBudgetExceeded("relevant history exceeds explicit message bound")
            recent_messages = [
                {
                    "history_kind": "message",
                    "message_id": item.id,
                    "role": (
                        "customer"
                        if item.role in {"user", "customer"}
                        else "action"
                        if item.role == "action" or item.message_kind == "action_update"
                        else "assistant"
                    ),
                    "content": redact_pii(item.content).text,
                    "message_kind": item.message_kind,
                    "conversation_sequence": item.conversation_sequence,
                    "historical": True,
                    "trusted": False,
                }
                for item in reversed(rows)
            ]
            self._mark_latest_history_pair(recent_messages)
            summary = await self.host.session.scalar(
                select(TicketSummary).where(
                    TicketSummary.tenant_id == state["tenant_id"],
                    TicketSummary.ticket_id == state["ticket_id"],
                    TicketSummary.customer_id == state["customer_id"],
                )
            )
            if summary is not None:
                current_summary = {
                    "history_kind": "ticket_summary",
                    "history_item_id": summary.id,
                    "current_ticket": True,
                    "ticket_id": summary.ticket_id,
                    "issue_type": summary.issue_type,
                    "confirmed_facts": [
                        {
                            "fact_type": fact.get("fact_type"),
                            "source_type": fact.get("source_type"),
                            "source_id": fact.get("source_id"),
                            "resource_version": fact.get("resource_version"),
                            "status": fact.get("status"),
                        }
                        for fact in summary.confirmed_facts[:4]
                        if isinstance(fact, dict)
                    ],
                    "open_questions": list(summary.open_questions[:3]),
                    "source_run_id": summary.source_run_id,
                    "event_watermark": summary.event_watermark,
                    "historical": True,
                    "trusted": False,
                    "grants_action_authority": False,
                }
        summary_history = [
            {
                **item,
                "history_kind": "ticket_summary",
                "history_item_id": str(
                    item.get("summary_id") or item.get("ticket_id") or f"historical-summary-{index}"
                ),
                "current_ticket": False,
                "historical": True,
                "trusted": False,
                "grants_action_authority": False,
            }
            for index, item in enumerate(historical_summaries[:3])
        ]
        return {
            "relevant_history": [
                *([current_summary] if current_summary is not None else []),
                *recent_messages,
                *summary_history,
            ]
        }

    @staticmethod
    def _mark_latest_history_pair(messages: list[dict[str, Any]]) -> None:
        response_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") in {"assistant", "action"}
            ),
            None,
        )
        if response_index is None:
            return
        messages[response_index]["retention"] = "latest_pair"
        customer_index = next(
            (
                index
                for index in range(response_index - 1, -1, -1)
                if messages[index].get("role") == "customer"
            ),
            None,
        )
        if customer_index is not None:
            messages[customer_index]["retention"] = "latest_pair"
