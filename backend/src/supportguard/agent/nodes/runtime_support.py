from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.actions.service import get_action_spec, get_action_spec_by_policy_capability
from supportguard.agent.constants import (
    MAX_GROUNDED_KNOWLEDGE_QUERY_CHARACTERS,
    MAX_TOOL_ATTEMPTS,
    MAX_TOOL_ROUNDS,
)
from supportguard.agent.context import (
    ContextAssembler,
    ContextBudget,
    authoritative_current_account_observation,
    authoritative_fact_completes_current_request,
    authoritative_read_only_fact_observation,
    usable_current_knowledge_observation,
)
from supportguard.agent.contracts import CanonicalRuntimeManifest, runtime_provenance
from supportguard.agent.conversation_semantics import is_knowledge_only_api_question
from supportguard.agent.current_facts import (
    current_run_billing_observation,
    current_run_tool_observation,
    requested_current_fact_observation,
    requested_current_fact_requirements,
    resolve_referential_billing_reference,
)
from supportguard.agent.evidence import refers_to_prior_comparison_scope
from supportguard.agent.nodes.finalization import safe_stop
from supportguard.agent.obligations import ActionObligationLedger
from supportguard.agent.patterns import (
    ANAPHORIC_KNOWLEDGE_FOLLOW_UP,
    DISCOURSE_LEADING_KNOWLEDGE_FOLLOW_UP,
    EXPLICIT_CJK_TOPIC_REMAINDER,
    EXPLICIT_ENGLISH_TOPIC_REMAINDER,
    EXPLICIT_TOPIC_IDENTIFIER,
    KNOWLEDGE_APPLICABILITY_QUESTION,
    KNOWLEDGE_CONTEXT_REFERENCE,
    TERSE_HISTORICAL_CONTEXT_FOLLOW_UP,
)
from supportguard.agent.persistence import AgentRunStore
from supportguard.agent.policy import PolicyRoute
from supportguard.agent.responses import safe_clarification_answer, safe_failure_answer
from supportguard.agent.schemas import (
    AgentDecision,
    BoundEvidenceSynthesis,
    CandidateCitation,
    CandidateResponse,
    FinalResponse,
    ProposalEligibility,
)
from supportguard.agent.state import AgentState, TopicContinuityResolution
from supportguard.config import Settings
from supportguard.contracts.action_preconditions import (
    ActionAdmissionV2,
    validate_entitlement_target,
)
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import ProposalCausalDecisionV2
from supportguard.contracts.context import (
    PolicyCapabilityMcpCallContext,
    ReadMcpCallContext,
    worker_execution_context,
)
from supportguard.contracts.freshness import current_fact_freshness_contract
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.contracts.tools import ObservationEnvelope, SourceRef, ToolCallContext
from supportguard.db.models import (
    AgentRun,
    CitationBinding,
    ContextLedger,
    ContextMembership,
    ConversationTurn,
    PolicyCapabilityResult,
    ProposalRecord,
    RawProviderDecisionEnvelope,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    ToolInvocation,
    ToolObservation,
    new_id,
)
from supportguard.policies.pii import redact_pii
from supportguard.providers.base import (
    ProviderTransportRecord,
    RawProviderDecision,
    StructuredProvider,
)
from supportguard.providers.deepseek import provider_error_code
from supportguard.rag.context_projection import EVIDENCE_PROJECTION_V2
from supportguard.rag.intent import RetrievalIntentEnvelope, resolve_retrieval_intent
from supportguard.rag.spans import lexical_query_terms
from supportguard.services.attempts import AttemptLedger, ReservedAttempt
from supportguard.services.capability_ledger import PolicyCapabilityLedger, ReservedCapability
from supportguard.services.commands import activate_next_turn
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger
from supportguard.services.turn_results import turn_result_for
from supportguard.tools.capabilities import registry_hash
from supportguard.tools.gateway import ReadToolCall, ReadToolName, ToolGateway


class GraphRuntimeSupport:
    """Shared durable runtime primitives used by typed Agent node collaborators."""

    provider: StructuredProvider
    gateway: ToolGateway
    budget: ContextBudget
    context_assembler: ContextAssembler
    session: AsyncSession | None
    test_capability: TestRuntimeCapability | None
    settings: Settings
    runtime_manifest: CanonicalRuntimeManifest
    segment_events: list[dict[str, Any]]

    @staticmethod
    def _render_validated_answer(
        candidate: CandidateResponse,
        *,
        route: PolicyRoute,
        finish_reason: str | None,
        integrity: bool,
        issue_type: str | None = None,
        requested_action: str = "none",
        conversation_continues: bool = False,
        policy_boundary: str | None = None,
        trusted_platform_fact: bool = False,
        trusted_action_state_fact: bool = False,
        explicit_first_step: bool = False,
        knowledge_read_failed: bool = False,
    ) -> str:
        if finish_reason == "needs_clarification":
            if requested_action == "refund":
                return safe_clarification_answer(
                    "请提供需要退款的账单 ID（Billing ID / 账单编号）。"
                )
            return safe_clarification_answer(candidate.answer)
        if finish_reason == "credential_redaction_guidance":
            return safe_failure_answer("credential_redaction_guidance")
        if finish_reason == "applicability_condition_unresolved":
            return candidate.answer
        if finish_reason == "terminal_business_outcome":
            return (
                candidate.answer
                if integrity
                else safe_failure_answer("citation_binding_incomplete")
            )
        if finish_reason == "proposal_eligibility_failed":
            return (
                "已完成部分事实核验，但动作申请的证据绑定或资源条件未通过校验。"
                "本次没有创建审批，也没有执行任何变更；请稍后重试或补充可验证的资源信息。"
            )
        if finish_reason == "requested_action_unresolved":
            grounded = list(
                dict.fromkeys(
                    claim.text.strip() for claim in candidate.material_claims if claim.text.strip()
                )
            )
            if integrity and grounded:
                grounded.append("根据当前核验结果，本次没有创建审批，也没有执行任何变更。")
                return "\n".join(grounded)
            return safe_failure_answer("requested_action_unresolved")
        if route == PolicyRoute.REJECT:
            return safe_failure_answer(
                policy_boundary
                if policy_boundary in {"out_of_scope", "prohibited"}
                else (finish_reason or "rejected")
            )
        if str(route) == "manual_takeover" or not integrity:
            return safe_failure_answer(
                finish_reason or "insufficient_evidence",
                failure_domain=(
                    "knowledge"
                    if issue_type == "product_knowledge" and knowledge_read_failed
                    else None
                ),
            )
        if trusted_platform_fact or trusted_action_state_fact:
            return candidate.answer
        texts = list(dict.fromkeys(claim.text.strip() for claim in candidate.material_claims))
        if finish_reason == "evidence_freshness_insufficient":
            texts.append(
                "说明：用于判断当前状态的实时数据已过期，因此无法确认此刻的状态；"
                "请刷新或重新查询当前数据后再判断。"
            )
            if conversation_continues:
                texts.insert(0, "当前动作处理期间不会锁定本会话，你仍可继续咨询。")
            return "\n".join(texts)
        if not texts:
            return safe_failure_answer(finish_reason or "insufficient_evidence")
        if issue_type == "api_diagnostics" and not any(
            marker in "".join(texts) for marker in ("下一步", "建议", "检查", "查看", "重试")
        ):
            texts.append(
                "下一步建议：先降低或排队并发请求，并按 Retry-After 保留原幂等键重试；"
                "如仍失败，请提供 Request ID 和发生区域。"
            )
        if explicit_first_step:
            action_markers = (
                "应",
                "需要",
                "请",
                "先",
                "检查",
                "确认",
                "查看",
                "等待",
                "降低",
                "排队",
                "重试",
                "提供",
                "retry",
                "check",
                "confirm",
                "wait",
                "reduce",
                "provide",
            )
            actionable = [
                (index, text)
                for index, text in enumerate(texts)
                if any(marker in text.lower() for marker in action_markers)
            ]
            if actionable:
                index, first = min(
                    actionable,
                    key=lambda item: (
                        0
                        if any(
                            marker in item[1].lower()
                            for marker in ("第一步", "首先", "first step", "start with")
                        )
                        else 1,
                        sum(item[1].count(marker) for marker in ("，", "；", "并", "然后")),
                        len(item[1]),
                        item[0],
                    ),
                )
                texts.pop(index)
                if not any(
                    marker in first.lower()
                    for marker in ("第一步", "首先", "first step", "start with")
                ):
                    first = f"第一步：{first}"
                texts.insert(0, first)
            else:
                texts.insert(0, "第一步：当前证据尚不足以确定一个可执行动作。")
        if conversation_continues:
            continuation = "当前动作处理期间不会锁定本会话，你仍可继续咨询。"
            if explicit_first_step:
                texts.append(continuation)
            else:
                texts.insert(0, continuation)
        return "\n".join(texts)

    async def _reject_raw_tool_batch(
        self, state: AgentState, raw: RawProviderDecision
    ) -> tuple[list[dict[str, Any]], int, int]:
        lease = await self._current_lease(state)
        if self.session is None or lease is None or not raw.tool_calls:
            return [], 0, 0
        requested_round = state["tool_rounds"] + 1
        round_rejected = requested_round > MAX_TOOL_ROUNDS
        round_index = min(requested_round, MAX_TOOL_ROUNDS)
        if not round_rejected:
            await self._reserve_tool_round(state)
        specs = [
            InvocationSpec(
                provider_tool_call_id=item.provider_tool_call_id,
                tool_name=item.name[:100] or "<empty>",
                arguments={},
                ordinal=item.ordinal,
                arguments_hash=hashlib.sha256(item.arguments_json.encode()).hexdigest(),
            )
            for item in raw.tool_calls
        ]
        decision_manifest = {
            "finish_reason": raw.finish_reason,
            "call_count": len(raw.tool_calls),
            "calls": [
                {
                    "ordinal": item.ordinal,
                    "name": item.name[:100],
                    "arguments_hash": hashlib.sha256(item.arguments_json.encode()).hexdigest(),
                }
                for item in raw.tool_calls
            ],
        }
        turn, invocations = await ToolLedger(self.session).open_turn(
            lease,
            segment_id=state["segment_id"],
            tool_round=round_index,
            decision=decision_manifest,
            context_manifest={"raw_provider_decision": True},
            calls=specs,
        )
        await self.session.commit()
        can_reserve = not round_rejected and (
            len(raw.tool_calls) <= 3
            and len({item.provider_tool_call_id for item in raw.tool_calls}) == len(raw.tool_calls)
            and len(raw.tool_calls) <= MAX_TOOL_ATTEMPTS - state["tool_attempts"]
        )
        observations: list[dict[str, Any]] = []
        for raw_call, invocation in zip(raw.tool_calls, invocations, strict=True):
            error_code = self._raw_call_error(raw_call)
            if round_rejected:
                error_code = "tool_round_budget_exhausted"
            elif error_code is None:
                error_code = "invalid_tool_batch"
            if can_reserve:
                preflight = await self._reserve_external(
                    state,
                    "tool_preflight",
                    logical_invocation_id=invocation.id,
                )
                await self._finish_external(preflight, status="failed", error_code=error_code)
            status = (
                "denied"
                if round_rejected
                else "forbidden_tool"
                if error_code == "forbidden_surface"
                else "invalid_input"
            )
            observation = ObservationEnvelope(
                tool_name=raw_call.name[:100] or "<empty>",
                tool_call_id=raw_call.provider_tool_call_id,
                ticket_id=state["ticket_id"],
                run_id=state["run_id"],
                attempt_index=1,
                status=cast(Any, status),
                retryable=False,
                error_code=error_code,
                safe_error_summary="The provider tool call was rejected before transport.",
                observed_at=datetime.now(UTC),
                duration_ms=0,
            )
            await ToolLedger(self.session).terminalize(
                lease,
                invocation.id,
                outcome=(
                    "budget_exhausted"
                    if round_rejected
                    else "forbidden_tool"
                    if status == "forbidden_tool"
                    else "invalid_input"
                ),
                observation=observation,
            )
            observations.append(observation.model_dump(mode="json"))
        await ToolLedger(self.session).close_turn(lease, turn.id)
        await self.session.commit()
        return observations, len(raw.tool_calls) if can_reserve else 0, 0 if round_rejected else 1

    @staticmethod
    def _raw_call_error(raw_call: Any) -> str | None:
        if raw_call.name in {
            "create_support_escalation",
            "propose_refund",
            "propose_api_key_revocation",
            "propose_entitlement_change",
            "execute_refund",
            "execute_api_key_revocation",
            "execute_entitlement_change",
        }:
            return "forbidden_surface"
        try:
            arguments = json.loads(raw_call.arguments_json)
            if not isinstance(arguments, dict):
                return "arguments_not_object"
            ReadToolCall.model_validate({"name": raw_call.name, "arguments": arguments})
        except json.JSONDecodeError:
            return "arguments_invalid_json"
        except Exception:
            return "tool_schema_invalid"
        return None

    async def _close_tool_batch(
        self,
        state: AgentState,
        calls: list[Any],
        *,
        outcome: str,
        status: str,
        reserve_preflight: bool = False,
        stop_reason: str | None = None,
    ) -> AgentState:
        lease = await self._current_lease(state)
        invocation_ids = state.get("tool_invocation_ids", [])
        observations: list[dict[str, Any]] = []
        for index, item in enumerate(calls):
            if reserve_preflight:
                invocation_id = invocation_ids[index] if index < len(invocation_ids) else None
                reserved = await self._reserve_external(
                    state,
                    "tool_preflight",
                    logical_invocation_id=invocation_id,
                )
                await self._finish_external(reserved, status="failed", error_code=outcome)
            observation = ObservationEnvelope(
                tool_name=item.call.name,
                tool_call_id=item.tool_call_id,
                ticket_id=state["ticket_id"],
                run_id=state["run_id"],
                attempt_index=1,
                status=cast(Any, status),
                retryable=False,
                error_code=outcome,
                safe_error_summary="The tool batch was rejected by deterministic runtime policy.",
                observed_at=datetime.now(UTC),
                duration_ms=0,
            )
            observations.append(observation.model_dump(mode="json"))
            if self.session is not None and lease is not None and index < len(invocation_ids):
                await ToolLedger(self.session).terminalize(
                    lease,
                    invocation_ids[index],
                    outcome=outcome,
                    observation=observation,
                )
        if self.session is not None and lease is not None and state.get("turn_group_id"):
            await ToolLedger(self.session).close_turn(lease, state["turn_group_id"])
            await self.session.commit()
        stopped = await safe_stop(
            self,
            state,
            stop_reason or ("budget_exhausted" if outcome == "budget_exhausted" else outcome),
        )
        stopped["latest_observations"] = observations
        stopped["tool_observations"] = [*state.get("tool_observations", []), *observations]
        if reserve_preflight:
            stopped["tool_attempts"] = state["tool_attempts"] + len(calls)
        return stopped

    @staticmethod
    def _observation_outcome(observation: ObservationEnvelope) -> str:
        mapping = {
            "ok": "succeeded",
            "invalid_input": "invalid_input",
            "forbidden_tool": "forbidden_tool",
            "denied": "denied",
            "timeout": "timed_out",
        }
        return mapping.get(observation.status, "failed")

    def _allowlist(self, state: AgentState) -> set[ReadToolName]:
        if state.get("classification", {}).get("policy_boundary", "allowed") != "allowed":
            return set()
        if state.get("action_state_query"):
            # Current action truth is already supplied by the read-only
            # projector. It can explain status but must never widen the MCP
            # surface or bootstrap a new action.
            return set()
        admission_payload = state.get("action_admission")
        if admission_payload and admission_payload.get("schema_version") == "action-admission.v2":
            admission = ActionAdmissionV2.model_validate(admission_payload)
            if admission.status == "admitted":
                if state.get("obligation_synthesis_mode"):
                    return set()
                ledger_payload = state.get("action_obligation_ledger")
                if ledger_payload:
                    ledger = ActionObligationLedger.model_validate(ledger_payload)
                    return {
                        cast(ReadToolName, capability)
                        for capability in ledger.unsatisfied_capabilities
                    }
                if admission.action_type is not None:
                    return {
                        cast(ReadToolName, capability)
                        for obligation in get_action_spec(admission.action_type).obligations
                        for capability in obligation.capabilities
                    }
        referential_billing = self._referential_billing_pending_reads(state)
        if referential_billing is not None:
            _, pending_tools = referential_billing
            return pending_tools
        referential_resolution = resolve_referential_billing_reference(state)
        if referential_resolution.status == "unresolved" and not self._has_active_action_context(
            state
        ):
            # A policy answer may still use public knowledge, but an ambiguous
            # customer reference must never leave a parameterized billing read
            # available for the model to guess.
            return (
                set()
                if current_run_tool_observation(state, "search_knowledge") is not None
                else {"search_knowledge"}
            )
        current_fact_requirements = requested_current_fact_requirements(state)
        if current_fact_requirements:
            pending_fact_tools = {
                tool_name
                for tool_name, data_fields in current_fact_requirements.values()
                if requested_current_fact_observation(
                    state,
                    tool_name=tool_name,
                    data_fields=data_fields,
                )
                is None
            }
            # The Provider still performs native Tool Calling, while Runtime
            # exposes only the explicit current facts the customer requested.
            # Once every read has returned, even a stale one, the surface closes
            # and Policy either publishes source-bound fresh values or a bounded
            # freshness explanation. Re-reading the same logical snapshot cannot
            # make it fresher and would only consume the final Tool Round.
            return pending_fact_tools
        classification = state.get("classification", {})
        issue = classification.get("issue_type", "unknown")
        if is_knowledge_only_api_question(classification):
            # A definition, policy, or troubleshooting-method question needs
            # versioned product knowledge, not a snapshot of this customer's
            # current usage.  Current-state diagnostics remain available only
            # when semantic intake explicitly marks them as real-time.
            return (
                set()
                if usable_current_knowledge_observation(state) is not None
                else {"search_knowledge"}
            )
        if issue == "product_knowledge" and state.get("knowledge_comparison_complete", False):
            # A deterministic versioned read has already satisfied both
            # published evidence groups. Keeping search_knowledge visible lets
            # a Provider spend the final Tool Round on a redundant current-only
            # read and then fail with budget_exhausted despite complete evidence.
            # An incomplete comparison does not enter this branch, preserving
            # one bounded recovery read.
            return set()
        current_knowledge = usable_current_knowledge_observation(state)
        if (
            current_knowledge is not None
            and state.get("classification", {}).get("needs_realtime_facts") is not True
        ):
            # One clean retrieval completes any read-only, knowledge-only
            # question. Citation or structure correction must rewrite from
            # those bindings with tools=[], not perform another search or add
            # unrelated business reads.
            return set()
        authoritative_current_fact = authoritative_read_only_fact_observation(state)
        if authoritative_current_fact is not None and authoritative_fact_completes_current_request(
            state
        ):
            # A pure current-state question is complete once the scoped,
            # transactional singleton read succeeds. Leaving unrelated read
            # tools visible makes the Provider free to replace an already
            # authoritative answer with an unnecessary knowledge lookup and
            # eventually exhaust the bounded Tool Round budget. Mixed
            # fact-plus-policy questions remain open below.
            return set()
        if issue == "billing_refund" and self._has_active_action_context(state):
            if self._has_current_successful_observation(state, "search_knowledge"):
                return set()
            return {"search_knowledge"}
        mapping: dict[str, set[ReadToolName]] = {
            "api_diagnostics": {
                "search_knowledge",
                "query_account",
                "query_subscription",
                "query_api_usage",
                "query_request_trace",
                "check_service_status",
                "query_incident_impact",
            },
            "credential_security": {
                "search_knowledge",
                "query_account",
                "query_api_key_metadata",
                "query_request_trace",
            },
            "billing_refund": {
                "search_knowledge",
                "query_account",
                "query_billing_record",
            },
            "incident_support": {
                "search_knowledge",
                "check_service_status",
                "query_request_trace",
                "query_incident_impact",
            },
            "product_knowledge": (
                {"search_knowledge", "query_account"}
                if state.get("classification", {}).get("needs_realtime_facts") is True
                else {"search_knowledge"}
            ),
            "entitlement_change": {
                "search_knowledge",
                "query_subscription",
                "query_api_usage",
            },
            "unknown": {"search_knowledge", "query_account"},
        }
        visible = mapping.get(str(issue), {"search_knowledge"})
        if authoritative_current_fact is not None:
            # Singleton resource reads such as query_subscription have no
            # alternate argument that could make an immediate second call
            # informative. Keep genuinely distinct capabilities visible while
            # removing only the already-satisfied read. This preserves the
            # bounded Agent loop without turning a safe duplicate into a
            # customer-visible no-progress failure.
            visible = visible - {cast(ReadToolName, authoritative_current_fact["tool_name"])}
        if current_knowledge is not None:
            # A mixed question may still require transactional business facts,
            # but its successful knowledge capability is complete. Removing
            # only search_knowledge preserves those distinct reads while
            # preventing a semantically duplicate second retrieval.
            visible = visible - {"search_knowledge"}
        if authoritative_current_account_observation(state) is not None:
            visible = visible - {"query_account"}
        return visible

    def _referential_billing_pending_reads(
        self,
        state: AgentState,
    ) -> tuple[str, set[ReadToolName]] | None:
        """Return the exact current-run reads for one safe billing anaphora.

        An active approval already owns a frozen action snapshot and follows the
        existing knowledge-only explanation path. Other read-only policy turns
        may reuse only a customer-authored opaque identifier; the business fact
        itself must come from a matching current-run Observation.
        """

        if self._has_active_action_context(state):
            return None
        resolution = resolve_referential_billing_reference(state)
        billing_record_id = resolution.billing_record_id
        if resolution.status != "resolved" or billing_record_id is None:
            return None
        pending: set[ReadToolName] = set()
        if current_run_tool_observation(state, "search_knowledge") is None:
            pending.add("search_knowledge")
        if current_run_billing_observation(state, billing_record_id) is None:
            pending.add("query_billing_record")
        return billing_record_id, pending

    @classmethod
    def _ground_policy_follow_up_query(
        cls, state: AgentState, decision: AgentDecision
    ) -> tuple[AgentDecision, bool]:
        """Canonicalize contextual retrieval without trusting Provider query text.

        A pending action is useful conversation context, but it can bias a model's
        knowledge query toward the original proposal workflow.  In the structural
        state where the current turn requests no action and the only injected read
        capability is knowledge search, the frozen contract requires the current
        customer question itself to be the retrieval query. Product-knowledge
        anaphora instead receives the nearest explicit customer-authored topic and
        the current customer message. Assistant answers and Provider-proposed query
        text never become retrieval authority. The raw provider envelope remains
        persisted unchanged; this method canonicalizes only the validated Runtime
        call that is recorded in the tool ledger and executed.
        """

        classification = state.get("classification", {})
        current_message = str(state.get("redacted_message", "")).strip()
        if decision.decision_type != "tool_calls" or not current_message:
            return decision, False

        billing_policy_follow_up = bool(
            classification.get("issue_type") == "billing_refund"
            and classification.get("policy_boundary") == "allowed"
            and classification.get("requested_action", "none") == "none"
            and cls._has_active_action_context(state)
        )
        product_knowledge_follow_up = bool(
            classification.get("issue_type") == "product_knowledge"
            and classification.get("policy_boundary") == "allowed"
            and classification.get("requested_action", "none") == "none"
            and cls._is_anaphoric_knowledge_follow_up(current_message)
        )
        if not billing_policy_follow_up and not product_knowledge_follow_up:
            return decision, False

        canonical_query = (
            current_message
            if billing_policy_follow_up
            else cls._ground_versioned_knowledge_query(state, current_message)
        )

        changed = False
        calls: list[dict[str, Any]] = []
        for item in decision.tool_calls:
            payload = item.model_dump(mode="json")
            if item.call.name == "search_knowledge":
                previous_query = str(payload["call"]["arguments"].get("query", ""))
                if previous_query != canonical_query:
                    payload["call"]["arguments"] = {"query": canonical_query}
                    changed = True
            calls.append(payload)
        if not changed:
            return decision, False
        return AgentDecision.model_validate(
            {**decision.model_dump(mode="json"), "tool_calls": calls}
        ), True

    @staticmethod
    def _canonicalize_action_read_arguments(
        state: AgentState,
        decision: AgentDecision,
    ) -> tuple[AgentDecision, bool]:
        """Bind current-usage obligations to the product's shortest fresh window.

        The Provider may validly select any public ``query_api_usage`` window.
        An admitted high-risk action, however, is not asking for a reporting
        aggregate: its deterministic obligation requires the freshest current
        usage snapshot before a proposal can be assembled.  Runtime therefore
        narrows only that read-only argument to ``1m`` while preserving the raw
        Provider envelope for audit.  This grants no proposal or execution
        authority and does not alter ordinary diagnostic queries.
        """

        if decision.decision_type != "tool_calls":
            return decision, False
        admission_payload = state.get("action_admission", {})
        ledger_payload = state.get("action_obligation_ledger", {})
        if (
            admission_payload.get("schema_version") != "action-admission.v2"
            or admission_payload.get("status") != "admitted"
            or not isinstance(ledger_payload, dict)
        ):
            return decision, False
        ledger = ActionObligationLedger.model_validate(ledger_payload)
        if "query_api_usage" not in ledger.unsatisfied_capabilities:
            return decision, False

        changed = False
        calls: list[dict[str, Any]] = []
        for item in decision.tool_calls:
            payload = item.model_dump(mode="json")
            if (
                item.call.name == "query_api_usage"
                and payload["call"]["arguments"].get("window") != "1m"
            ):
                payload["call"]["arguments"] = {"window": "1m"}
                changed = True
            calls.append(payload)
        if not changed:
            return decision, False
        return AgentDecision.model_validate(
            {**decision.model_dump(mode="json"), "tool_calls": calls}
        ), True

    @staticmethod
    def _has_active_action_context(state: AgentState) -> bool:
        if state.get("current_actions"):
            return any(
                item.get("projection_status")
                in {"pending", "approved", "executing", "verification_pending"}
                for item in state["current_actions"]
                if isinstance(item, dict)
            )
        # Read-only checkpoint compatibility for runs created before
        # ConversationActionStateV1. New history loading never writes this
        # independently inferred shape.
        return any(
            bool(item.get("active_action_summaries"))
            for item in state.get("relevant_history", [])
            if isinstance(item, dict)
        )

    @staticmethod
    def _has_current_successful_observation(state: AgentState, tool_name: str) -> bool:
        return any(
            item.get("tool_name") == tool_name
            and item.get("status") == "ok"
            and item.get("run_id") == state.get("run_id")
            for item in state.get("tool_observations", [])
        )

    @classmethod
    def _mixed_account_applicability_missing_groups(
        cls,
        state: AgentState,
        candidate: CandidateResponse,
    ) -> list[str]:
        """Require both evidence namespaces for current-account applicability.

        ``issue_type`` identifies the primary support domain; it is not an
        exclusive evidence namespace. A product requirement question marked as
        needing real-time facts must not publish a knowledge-only conclusion
        about the customer's current account.
        """

        classification = state.get("classification", {})
        if not (
            classification.get("policy_boundary", "allowed") == "allowed"
            and classification.get("issue_type") == "product_knowledge"
            and classification.get("requested_action", "none") == "none"
            and classification.get("needs_realtime_facts") is True
            and candidate.action == "answer"
        ):
            return []
        missing: list[str] = []
        if not candidate.knowledge_citations or not any(
            claim.citation_binding_ids for claim in candidate.material_claims
        ):
            missing.append("knowledge_claim")
        account = authoritative_current_account_observation(state)
        if account is None:
            missing.append("current_account_observation")
            return missing
        account_sources = {
            str(source.get("source_id"))
            for source in account.get("source_refs", [])
            if source.get("source_id")
        }
        claimed_sources = {
            source_id
            for claim in candidate.material_claims
            for source_id in claim.observation_source_ids
        }
        if not account_sources or not (account_sources & claimed_sources):
            missing.append("current_account_claim")
        return missing

    @classmethod
    def _authoritative_read_only_fact_contract_valid(
        cls,
        state: AgentState,
        candidate: CandidateResponse,
    ) -> bool:
        """Bind a fact-only answer to the exact current authoritative read."""

        observation = authoritative_read_only_fact_observation(state)
        if (
            observation is None
            or candidate.action != "answer"
            or not candidate.material_claims
            or candidate.knowledge_chunk_ids
            or candidate.knowledge_citations
        ):
            return False
        allowed_sources = {
            str(source.get("source_id"))
            for source in observation.get("source_refs", [])
            if source.get("source_id")
        }
        claimed_sources = {
            source_id
            for claim in candidate.material_claims
            for source_id in claim.observation_source_ids
        }
        return bool(
            allowed_sources
            and claimed_sources
            and claimed_sources <= allowed_sources
            and set(candidate.business_source_ids) == claimed_sources
            and all(
                claim.observation_source_ids
                and not claim.citation_binding_ids
                and not claim.knowledge_locator_hashes
                for claim in candidate.material_claims
            )
        )

    def _clarification_requires_knowledge_first(self, state: AgentState) -> bool:
        """Require evidence before resolving versioned or anaphoric knowledge turns."""
        classification = state.get("classification", {})
        if (
            classification.get("policy_boundary", "allowed") != "allowed"
            or classification.get("issue_type") != "product_knowledge"
            or "search_knowledge" not in self._allowlist(state)
            or self._has_current_successful_observation(state, "search_knowledge")
        ):
            return False
        message = str(state.get("redacted_message", "")).strip()
        intent = resolve_retrieval_intent(message).intent
        return intent in {"historical", "compare"} or bool(
            KNOWLEDGE_CONTEXT_REFERENCE.search(message)
        )

    @staticmethod
    def _knowledge_comparison_contract(
        observations: list[dict[str, Any]],
    ) -> tuple[bool, bool]:
        """Classify a bounded two-lane comparison from executed read facts.

        A published version difference is explainable only when the executed
        query—not the current message or Provider prose—requested comparison,
        both independently selected evidence groups are present, and each group
        contains a traceable source locator. Candidate publication separately
        requires eligible spans and valid citations from both groups. Other
        conflict classes remain unsafe.
        """

        knowledge = [item for item in observations if item.get("tool_name") == "search_knowledge"]
        requested = any(
            item.get("trusted_retrieval_intent", {}).get("intent") == "compare"
            for item in knowledge
        )
        if not requested:
            return False, False

        def complete(item: dict[str, Any]) -> bool:
            if (
                item.get("status") != "ok"
                or item.get("trusted_retrieval_intent", {}).get("intent") != "compare"
            ):
                return False
            data = item.get("data", {})
            if data.get("conflict") is True or data.get("refusal_reason") is not None:
                return False
            traced_groups = {
                str(evidence.get("evidence_group") or "current")
                for evidence in data.get("evidence", [])
                if len(str(evidence.get("source_locator", {}).get("locator_hash") or "")) == 64
            }
            return traced_groups == {"current", "historical"}

        conflict_results = [
            item
            for item in knowledge
            if item.get("trusted_retrieval_intent", {}).get("intent") == "compare"
        ]
        # A bounded recovery read is specifically allowed to replace an
        # incomplete first comparison result. Requiring every historical
        # attempt to be complete makes successful recovery impossible.
        return requested, bool(conflict_results and complete(conflict_results[-1]))

    @classmethod
    def _knowledge_comparison_state(
        cls,
        observations: list[dict[str, Any]],
        *,
        current_message: str,
    ) -> tuple[bool, bool]:
        """Apply comparison requirements only when the current turn asks for them.

        Conversation history may broaden retrieval to resolve anaphora, but it
        cannot silently change a current-question answer contract into a
        version-comparison contract. Explicit historical or comparison turns
        retain the full two-group requirement.
        """

        effective = cls._effective_knowledge_observations(
            observations,
            current_message=current_message,
        )
        if resolve_retrieval_intent(
            current_message
        ).intent == "current" and not refers_to_prior_comparison_scope(current_message):
            return False, False
        return cls._knowledge_comparison_contract(effective)

    @staticmethod
    def _effective_knowledge_observations(
        observations: list[dict[str, Any]],
        *,
        current_message: str,
    ) -> list[dict[str, Any]]:
        """Use a later focused read to resolve an earlier broad follow-up read.

        A deterministic contextual read can conservatively start with a
        current-versus-historical comparison.  If the customer's literal
        message did not itself request comparison and the Agent then performs a
        narrower, successful current read, that later result is the effective
        evidence state for the answer.  Explicit historical/comparison requests
        never receive this supersession.
        """

        knowledge = [item for item in observations if item.get("tool_name") == "search_knowledge"]
        if not knowledge:
            return []
        direct_intent = resolve_retrieval_intent(current_message).intent
        latest = knowledge[-1]
        latest_data = latest.get("data", {})
        if (
            direct_intent == "current"
            and latest.get("status") == "ok"
            and latest_data.get("evidence")
            and latest_data.get("conflict") is not True
            and latest_data.get("refusal_reason") is None
        ):
            return [latest]
        return knowledge

    @staticmethod
    def _history_customer_messages(state: AgentState) -> list[str]:
        """Return bounded, redacted customer history in canonical conversation order."""

        messages: list[str] = []
        for item in state.get("relevant_history", []):
            if (
                not isinstance(item, dict)
                or item.get("history_kind") != "message"
                or item.get("role") != "customer"
            ):
                continue
            content = str(item.get("content", "")).strip()
            if content:
                messages.append(content)
        return messages

    @staticmethod
    def _is_anaphoric_knowledge_follow_up(message: str) -> bool:
        """Return whether a customer turn requires a prior customer topic anchor."""

        if not message:
            return False
        if (
            ANAPHORIC_KNOWLEDGE_FOLLOW_UP.search(message)
            or TERSE_HISTORICAL_CONTEXT_FOLLOW_UP.fullmatch(message)
            or refers_to_prior_comparison_scope(message)
        ):
            return True
        discourse = DISCOURSE_LEADING_KNOWLEDGE_FOLLOW_UP.match(message)
        if discourse is None:
            return False
        remainder = message[discourse.end() :].strip()
        # A leading “那/那么/then/so” often omits the already established
        # subject.  A structured product/model/version identifier, however,
        # makes the new turn independently retrievable and must win over
        # unrelated history.  This is intentionally syntax based rather than
        # tied to any journey, product name, or corpus document.
        explicit_topic = bool(
            remainder
            and (
                EXPLICIT_TOPIC_IDENTIFIER.search(remainder)
                or EXPLICIT_ENGLISH_TOPIC_REMAINDER.search(remainder)
                or EXPLICIT_CJK_TOPIC_REMAINDER.search(remainder)
            )
        )
        return not explicit_topic

    @classmethod
    def _resolve_knowledge_topic_query(
        cls,
        state: AgentState,
        current_message: str,
    ) -> TopicContinuityResolution:
        """Resolve one bounded Customer-owned topic anchor and safe trace facts."""

        current = current_message.strip()
        anaphoric = cls._is_anaphoric_knowledge_follow_up(current)
        if not anaphoric:
            query = current
            return {
                "query": query,
                "topic_anchor_applied": False,
                "anchor_source": None,
                "anaphoric_chain_length": 0,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "query_length": len(query),
            }

        prior = cls._history_customer_messages(state)
        skipped_anaphoric = 0
        anchor: str | None = None
        for candidate in reversed(prior):
            if cls._is_anaphoric_knowledge_follow_up(candidate):
                skipped_anaphoric += 1
                continue
            anchor = candidate
            break

        current_part = current[:320]
        parts = [current_part]
        if anchor is not None:
            parts.insert(0, anchor[:320])
        current_intent = resolve_retrieval_intent(current)
        if (
            anchor is not None
            and (current_intent.intent == "historical" or refers_to_prior_comparison_scope(current))
            and current_intent.historical_version is None
            and current_intent.as_of is None
        ):
            parts.insert(0, "对比当前版本与旧版本：")
        query = "\n".join(dict.fromkeys(parts))[:MAX_GROUNDED_KNOWLEDGE_QUERY_CHARACTERS]
        return {
            "query": query,
            "topic_anchor_applied": anchor is not None,
            "anchor_source": "customer_message" if anchor is not None else None,
            "anaphoric_chain_length": 1 + skipped_anaphoric,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "query_length": len(query),
        }

    @classmethod
    def _ground_versioned_knowledge_query(
        cls,
        state: AgentState,
        current_message: str,
    ) -> str:
        """Bind an underspecified knowledge follow-up to bounded redacted context.

        Only canonical customer messages define the topic and comparison focus.
        Assistant answers and Provider query text are excluded so generated text
        cannot silently become retrieval input or factual authority.
        """

        return cls._resolve_knowledge_topic_query(state, current_message)["query"]

    @classmethod
    def _topic_continuity_event_payload(
        cls,
        state: AgentState,
        decision: AgentDecision,
    ) -> dict[str, Any]:
        """Return content-free audit metadata for an executed knowledge query."""

        classification = state.get("classification", {})
        current_message = str(state.get("redacted_message", "")).strip()
        if (
            classification.get("issue_type") != "product_knowledge"
            or classification.get("policy_boundary") != "allowed"
            or classification.get("requested_action", "none") != "none"
            or not current_message
            or not cls._is_anaphoric_knowledge_follow_up(current_message)
            or not any(item.call.name == "search_knowledge" for item in decision.tool_calls)
        ):
            return {}
        resolution = cls._resolve_knowledge_topic_query(state, current_message)
        return {
            "topic_anchor_applied": resolution["topic_anchor_applied"],
            "topic_anchor_source": resolution["anchor_source"],
            "topic_anaphoric_chain_length": resolution["anaphoric_chain_length"],
            "topic_query_sha256": resolution["query_sha256"],
            "topic_query_length": resolution["query_length"],
        }

    def _required_evidence_decision(self, state: AgentState) -> AgentDecision | None:
        """Turn a deterministic evidence requirement into one bounded Read Tool decision."""

        fact_requirements = requested_current_fact_requirements(state)
        pending_fact_tools = list(
            dict.fromkeys(
                tool_name
                for tool_name, data_fields in fact_requirements.values()
                if requested_current_fact_observation(
                    state,
                    tool_name=tool_name,
                    data_fields=data_fields,
                )
                is None
            )
        )
        close_partial_fact_plan = bool(
            pending_fact_tools
            and (state.get("tool_rounds", 0) > 0 or state.get("evidence_replan_required", False))
        )
        referential_billing = self._referential_billing_pending_reads(state)
        pending_referential_tools = (
            referential_billing[1] if referential_billing is not None else set()
        )
        require_knowledge = self._clarification_requires_knowledge_first(state)
        if (
            not (close_partial_fact_plan or require_knowledge or pending_referential_tools)
            or state["tool_rounds"] >= MAX_TOOL_ROUNDS
            or state["tool_attempts"] >= MAX_TOOL_ATTEMPTS
        ):
            return None
        query = str(state.get("redacted_message", "")).strip()
        if not query:
            return None
        tool_calls: list[dict[str, Any]] = []
        if referential_billing is not None:
            billing_record_id, _ = referential_billing
            if "search_knowledge" in pending_referential_tools:
                tool_calls.append(
                    {
                        "tool_call_id": f"required_billing_knowledge_{uuid4().hex}",
                        "call": {
                            "name": "search_knowledge",
                            "arguments": {"query": query},
                        },
                    }
                )
            if "query_billing_record" in pending_referential_tools:
                tool_calls.append(
                    {
                        "tool_call_id": f"required_billing_record_{uuid4().hex}",
                        "call": {
                            "name": "query_billing_record",
                            "arguments": {"billing_record_id": billing_record_id},
                        },
                    }
                )
        if require_knowledge:
            query = self._ground_versioned_knowledge_query(state, query)
            tool_calls.append(
                {
                    "tool_call_id": f"required_knowledge_{uuid4().hex}",
                    "call": {
                        "name": "search_knowledge",
                        "arguments": {"query": query},
                    },
                }
            )
            if state.get("classification", {}).get("needs_realtime_facts") is True:
                tool_calls.append(
                    {
                        "tool_call_id": f"required_account_{uuid4().hex}",
                        "call": {"name": "query_account", "arguments": {}},
                    }
                )
        existing_tools = {str(item["call"]["name"]) for item in tool_calls}
        for tool_name in pending_fact_tools:
            if tool_name in existing_tools:
                continue
            tool_calls.append(
                {
                    "tool_call_id": f"required_current_fact_{uuid4().hex}",
                    "call": {
                        "name": tool_name,
                        "arguments": ({"window": "1m"} if tool_name == "query_api_usage" else {}),
                    },
                }
            )
        return AgentDecision.model_validate(
            {
                "decision_type": "tool_calls",
                "decision_summary": (
                    "Runtime requires the remaining grounded evidence obligations "
                    "before a bounded final answer."
                ),
                "tool_calls": tool_calls,
            }
        )

    @staticmethod
    def _has_secret_redaction(state: AgentState) -> bool:
        return "secret.api_key.v1" in state.get("redaction_rule_ids", []) or (
            "[REDACTED_API_KEY]" in state.get("redacted_message", "")
        )

    @staticmethod
    def _trusted_platform_answer(subject: str) -> str | None:
        """Return only stable facts owned by the SupportGuard product contract."""

        if subject == "supportguard_greeting":
            return (
                "你好，我是 SupportGuard。你可以向我咨询产品功能、API 错误、用量、账单、"
                "API Key、配额或服务异常。请告诉我你遇到的具体问题。"
            )

        if subject == "supportguard_identity":
            return (
                "我是 SupportGuard，一个面向 AI SaaS 客服场景的证据优先支持 Agent。"
                "我会在当前租户范围内查询产品资料和实时业务事实；任何高风险操作都只会先形成"
                "申请，通过独立审批后再由系统安全执行。"
            )
        if subject == "supportguard_capabilities":
            return (
                "SupportGuard 可以回答产品能力问题，诊断 API 与事故影响，核验计费、API Key "
                "和配额状态，并为符合条件的退款、Key 撤销或配额变更生成待审批申请。"
                "它不能跨租户读取数据，也不会让模型直接执行高风险操作。"
            )
        return None

    @classmethod
    def _canonicalize_grounded_conflict_clarification(
        cls,
        state: AgentState,
        candidate: CandidateResponse,
    ) -> CandidateResponse:
        """Bind a post-retrieval applicability question to the observed conflict."""

        classification = state.get("classification", {})
        if (
            candidate.action != "answer"
            or classification.get("issue_type") != "product_knowledge"
            or classification.get("policy_boundary") != "allowed"
            or resolve_retrieval_intent(str(state.get("redacted_message", ""))).intent != "compare"
            or not KNOWLEDGE_APPLICABILITY_QUESTION.search(str(state.get("redacted_message", "")))
            or not state.get("evidence_conflict", False)
            or not cls._has_current_successful_observation(state, "search_knowledge")
        ):
            return candidate
        binding_by_chunk = {
            str(details.get("chunk_id", "")): str(binding_id)
            for binding_id, details in state.get("citation_binding_map", {}).items()
            if details.get("chunk_id")
        }
        selected: list[tuple[str, str, str]] = []
        seen_groups: set[str] = set()
        for evidence in state.get("evidence", []):
            chunk_id = str(evidence.get("chunk_id", ""))
            binding_id = binding_by_chunk.get(chunk_id)
            locator_hash = str(evidence.get("source_locator", {}).get("locator_hash", ""))
            group = str(evidence.get("evidence_group") or "current")
            if (
                not binding_id
                or len(locator_hash) != 64
                or evidence.get("supporting_span_eligible") is not True
                or group in seen_groups
            ):
                continue
            selected.append((binding_id, chunk_id, locator_hash))
            seen_groups.add(group)
            if len(selected) == 2:
                break
        if seen_groups != {"current", "historical"}:
            return candidate
        message = str(state.get("redacted_message", ""))
        lowered = message.casefold()
        missing_conditions: list[str] = []
        if any(marker in lowered for marker in ("部署区域", "区域", "region")):
            missing_conditions.append("部署区域")
        if any(marker in lowered for marker in ("套餐", "plan", "subscription tier")):
            missing_conditions.append("套餐")
        if any(marker in lowered for marker in ("模型", "model")):
            missing_conditions.append("模型")
        if missing_conditions:
            clarification = f"请补充{'、'.join(missing_conditions)}"
        else:
            clarification = "请补充能区分适用版本的条件（例如部署区域、套餐或模型）"
        claim = (
            "已检索到的当前与历史发布证据对相关限制存在差异。"
            f"{clarification}；在补充这一适用条件前，不能直接判断是否支持。"
        )
        return CandidateResponse.model_validate(
            {
                "answer": claim,
                "action": "answer",
                "knowledge_chunk_ids": [chunk_id for _, chunk_id, _ in selected],
                "knowledge_citations": [
                    {"citation_binding_id": binding_id} for binding_id, _, _ in selected
                ],
                "business_source_ids": [],
                "material_claims": [
                    {
                        "text": claim,
                        "citation_binding_ids": [binding_id for binding_id, _, _ in selected],
                        "knowledge_locator_hashes": [
                            locator_hash for _, _, locator_hash in selected
                        ],
                        "observation_source_ids": [],
                    }
                ],
                "proposed_arguments": {},
            }
        )

    @classmethod
    def _canonicalize_pending_action_policy_candidate(
        cls, state: AgentState, candidate: CandidateResponse
    ) -> CandidateResponse:
        """Bind a pending-action policy answer to exact selected knowledge spans."""

        classification = state.get("classification", {})
        if (
            classification.get("issue_type") != "billing_refund"
            or classification.get("policy_boundary") != "allowed"
            or classification.get("requested_action", "none") != "none"
            or not cls._has_active_action_context(state)
            or not cls._has_current_successful_observation(state, "search_knowledge")
            or state.get("evidence_conflict", False)
        ):
            return candidate
        evidence_by_chunk = {
            str(item.get("chunk_id")): item
            for item in state.get("evidence", [])
            if item.get("chunk_id")
            and item.get("supporting_span_eligible") is True
            and str(item.get("supporting_span", "")).strip()
        }
        binding_by_chunk = {
            str(details.get("chunk_id", "")): str(binding_id)
            for binding_id, details in state.get("citation_binding_map", {}).items()
        }
        ranked_evidence: list[tuple[int, int, dict[str, Any]]] = []
        query_terms = lexical_query_terms(str(state.get("redacted_message", "")))
        eligible_surfaces = [
            (
                str(item.get("section_path", "")).lower(),
                str(item.get("supporting_span", "")).lower(),
            )
            for item in evidence_by_chunk.values()
        ]
        term_document_frequency = {
            term: sum(
                1
                for section_path, supporting_span in eligible_surfaces
                if term.lower() in f"{section_path}\n{supporting_span}"
            )
            for term in query_terms
        }
        evidence_count = len(eligible_surfaces)
        for rank, item in enumerate(state.get("evidence", [])):
            chunk_id = str(item.get("chunk_id", ""))
            evidence = evidence_by_chunk.get(chunk_id)
            binding_id = binding_by_chunk.get(chunk_id)
            if evidence is not None and binding_id is not None:
                section_path = str(evidence.get("section_path", "")).lower()
                span = str(evidence.get("supporting_span", "")).lower()
                lexical_score = 0
                for term in query_terms:
                    normalized_term = term.lower()
                    document_frequency = term_document_frequency[term]
                    if document_frequency == 0:
                        continue
                    specificity = evidence_count + 1 - document_frequency
                    term_weight = len(normalized_term) ** 2 * specificity
                    if normalized_term in section_path:
                        # Section paths are curated document structure and are
                        # more discriminative than incidental prose overlap.
                        lexical_score += term_weight * 2
                    if normalized_term in span:
                        lexical_score += term_weight
                ranked_evidence.append((lexical_score, -rank, evidence))
        selected: list[tuple[str, str, dict[str, Any]]] = []
        if ranked_evidence:
            # Re-rank only the already selected, claim-eligible evidence by the
            # current user turn. Retrieval rank remains the deterministic tie
            # breaker; citation-map insertion order never affects relevance.
            _score, _rank, evidence = max(ranked_evidence, key=lambda item: item[:2])
            chunk_id = str(evidence["chunk_id"])
            selected.append((binding_by_chunk[chunk_id], chunk_id, evidence))
        claims = []
        for binding_id, _chunk_id, evidence in selected:
            locator_hash = str(evidence.get("source_locator", {}).get("locator_hash", ""))
            if not locator_hash:
                continue
            claims.append(
                {
                    "text": str(evidence["supporting_span"]).strip(),
                    "citation_binding_ids": [binding_id],
                    "knowledge_locator_hashes": [locator_hash],
                    "observation_source_ids": [],
                }
            )
        if not claims:
            return candidate
        claim_binding_ids = [str(item["citation_binding_ids"][0]) for item in claims]
        binding_to_chunk = {binding_id: chunk_id for binding_id, chunk_id, _ in selected}
        chunk_ids = [binding_to_chunk[binding_id] for binding_id in claim_binding_ids]
        answer = str(claims[0]["text"])
        return CandidateResponse.model_validate(
            {
                "answer": answer,
                "action": "answer",
                "knowledge_chunk_ids": chunk_ids,
                "knowledge_citations": [
                    {"citation_binding_id": binding_id} for binding_id in claim_binding_ids
                ],
                "business_source_ids": [],
                "material_claims": claims,
                "proposed_arguments": {},
            }
        )

    @staticmethod
    def _message_specifies_request(message: str) -> bool:
        return bool(
            re.search(
                r"\b(?:req(?:uest)?|trace)[-_:.]?[a-z0-9]{4,}\b",
                message,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _requests_explicit_first_step(message: str) -> bool:
        normalized = " ".join(message.lower().split())
        return bool(
            re.search(
                r"(最先|首先|第一步|先做什么|先应该|应该先|"
                r"first\s+step|what\s+should\s+i\s+do\s+first|where\s+should\s+i\s+start)",
                normalized,
            )
        )

    @staticmethod
    def _parse_raw_provider_decision(raw: RawProviderDecision) -> AgentDecision:
        if raw.tool_calls:
            calls = []
            for item in raw.tool_calls:
                arguments = json.loads(item.arguments_json)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be a JSON object")
                calls.append(
                    {
                        "tool_call_id": item.provider_tool_call_id,
                        "call": ReadToolCall.model_validate(
                            {"name": item.name, "arguments": arguments}
                        ).model_dump(mode="json"),
                    }
                )
            return AgentDecision.model_validate(
                {
                    "decision_type": "tool_calls",
                    "decision_summary": "Requested current read-only evidence.",
                    "tool_calls": calls,
                }
            )
        if not raw.content:
            raise ValueError("provider returned neither tool calls nor content")
        content = raw.content.strip()
        # Some OpenAI-compatible providers wrap an otherwise valid JSON-object
        # response in one Markdown fence despite response_format=json_object.
        # Accept only that exact transport wrapper; schema validation below
        # remains strict and prose-prefixed/suffixed output still fails closed.
        if content.startswith("```json\n") and content.endswith("\n```"):
            content = content[len("```json\n") : -len("\n```")].strip()
        elif content.startswith("```\n") and content.endswith("\n```"):
            content = content[len("```\n") : -len("\n```")].strip()
        payload = json.loads(content)
        if isinstance(payload, dict) and payload.get("decision_type"):
            payload.setdefault("decision_summary", "Returned a bounded terminal decision.")
            return AgentDecision.model_validate(payload)
        candidate = CandidateResponse.model_validate(payload)
        return AgentDecision(
            decision_type="final_candidate",
            decision_summary="Returned a grounded terminal candidate.",
            candidate=candidate,
        )

    @staticmethod
    def _assistant_tool_turn(decision: AgentDecision) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": item.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": item.call.name,
                        "arguments": json.dumps(
                            item.call.arguments.model_dump(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for item in decision.tool_calls
            ],
        }

    @staticmethod
    def _normalize_terminal_decision(decision: AgentDecision) -> AgentDecision:
        if decision.decision_type == "tool_calls":
            if not 1 <= len(decision.tool_calls) <= 3 or decision.candidate is not None:
                raise ValueError("tool decision must contain one to three calls and no candidate")
            return decision
        if decision.decision_type == "needs_clarification":
            question = decision.clarification_question or "请补充完成判断所需的关键信息。"
            decision.candidate = CandidateResponse(
                answer=question,
                action="answer",
                knowledge_chunk_ids=[],
                business_source_ids=[],
            )
            return decision
        if decision.decision_type == "manual_takeover" and decision.candidate is None:
            decision.candidate = CandidateResponse(
                answer="当前证据不足，已转人工处理。",
                action="manual_takeover",
                knowledge_chunk_ids=[],
                business_source_ids=[],
            )
        if decision.candidate is None:
            raise ValueError("terminal decision requires a candidate")
        return decision

    @staticmethod
    def _canonicalize_candidate_references(
        decision: AgentDecision,
        evidence: list[dict[str, Any]],
    ) -> AgentDecision:
        """Derive redundant publication references from material claims.

        The Provider selects support per claim. Top-level citation, chunk and
        business-source arrays are transport conveniences, not independent
        authority. Rebuilding them here prevents an unused model-supplied
        source from passing Policy only to fail the stricter publication gate.
        Unknown claim bindings remain present and therefore fail closed later.
        """

        candidate = decision.candidate
        if candidate is None:
            return decision
        GraphRuntimeSupport._canonicalize_publication_references(candidate, evidence)
        return decision

    @staticmethod
    def _canonicalize_publication_references(
        candidate: CandidateResponse | BoundEvidenceSynthesis,
        evidence: list[dict[str, Any]],
    ) -> int:
        """Keep one exact public binding per claim and product source.

        Durable Context and Citation Binding rows remain untouched. This
        normalization only removes redundant support selected for the same
        material claim from the same document version and evidence group. A
        current/historical comparison or two document versions therefore keep
        their distinct bindings. Unknown identities are never merged so Policy
        can still reject them fail closed.
        """

        evidence_by_binding = {
            str(item["citation_binding_id"]): item
            for item in evidence
            if item.get("citation_binding_id")
        }

        def source_identity(binding_id: str) -> tuple[str, str, str]:
            item = evidence_by_binding.get(binding_id)
            if item is None:
                return (f"unknown:{binding_id}", "", "")
            locator = item.get("source_locator")
            locator_payload = locator if isinstance(locator, dict) else {}
            document_identity = str(
                item.get("document_id")
                or locator_payload.get("document_internal_id")
                or item.get("chunk_id")
                or item.get("source_locator_hash")
                or binding_id
            )
            version = str(item.get("version") or locator_payload.get("document_version") or "")
            evidence_group = str(item.get("evidence_group") or "current")
            return (document_identity, version, evidence_group)

        def retrieval_score(binding_id: str) -> float:
            raw_score = evidence_by_binding.get(binding_id, {}).get("retrieval_score")
            if not isinstance(raw_score, (str, int, float)):
                return float("-inf")
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                return float("-inf")
            return score if math.isfinite(score) else float("-inf")

        removed = 0
        for claim in candidate.material_claims:
            groups: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
            for ordinal, binding_id in enumerate(claim.citation_binding_ids):
                identity = source_identity(binding_id)
                if binding_id not in evidence_by_binding:
                    identity = (f"unknown:{ordinal}:{binding_id}", "", "")
                groups.setdefault(identity, []).append((ordinal, binding_id))
            winners = {
                max(
                    members,
                    key=lambda item: (retrieval_score(item[1]), -item[0]),
                )[1]
                for members in groups.values()
            }
            original = list(claim.citation_binding_ids)
            claim.citation_binding_ids = [
                binding_id for binding_id in original if binding_id in winners
            ]
            removed += len(original) - len(claim.citation_binding_ids)

        binding_chunks = {
            str(item.get("citation_binding_id")): str(item.get("chunk_id"))
            for item in evidence
            if item.get("citation_binding_id") and item.get("chunk_id")
        }
        binding_locators = {
            str(item.get("citation_binding_id")): str(item.get("source_locator_hash"))
            for item in evidence
            if item.get("citation_binding_id") and item.get("source_locator_hash")
        }
        for claim in candidate.material_claims:
            # A Citation Binding already commits to one exact eligible source
            # locator. The Provider chooses the binding; Runtime derives this
            # redundant list so transcription cannot discard a grounded answer.
            # Unknown bindings remain present and fail Policy's subset check.
            claim.knowledge_locator_hashes = list(
                dict.fromkeys(
                    binding_locators[binding_id]
                    for binding_id in claim.citation_binding_ids
                    if binding_id in binding_locators
                )
            )
        binding_ids = list(
            dict.fromkeys(
                binding_id
                for claim in candidate.material_claims
                for binding_id in claim.citation_binding_ids
            )
        )
        candidate.knowledge_citations = [
            CandidateCitation(citation_binding_id=binding_id) for binding_id in binding_ids
        ]
        candidate.knowledge_chunk_ids = list(
            dict.fromkeys(
                binding_chunks[binding_id]
                for binding_id in binding_ids
                if binding_id in binding_chunks
            )
        )
        candidate.business_source_ids = list(
            dict.fromkeys(
                source_id
                for claim in candidate.material_claims
                for source_id in claim.observation_source_ids
            )
        )
        return removed

    @staticmethod
    def _finish_reason(decision: AgentDecision) -> str:
        if decision.decision_type == "needs_clarification":
            return "needs_clarification"
        if decision.decision_type == "manual_takeover":
            return "manual_takeover"
        if decision.candidate and decision.candidate.action in {
            "refund_proposal",
            "api_key_revocation_proposal",
            "entitlement_change_proposal",
        }:
            return "action_candidate_generated"
        if decision.candidate and decision.candidate.action == "manual_takeover":
            return "manual_takeover"
        if decision.candidate and decision.candidate.action == "reject":
            return "rejected"
        return "answered"

    @staticmethod
    def _fingerprint(call: ReadToolCall, state: AgentState) -> str:
        observations = [
            {
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "resource_version": item.get("resource_version"),
                "source_refs": item.get("source_refs", []),
                "data_hash": hashlib.sha256(
                    json.dumps(
                        item.get("data", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest(),
            }
            for item in state.get("tool_observations", [])
        ]
        identity = {
            "tool_contract_hash": registry_hash(),
            "call": call.model_dump(mode="json"),
            "scope": {
                "tenant_id": state.get("tenant_id"),
                "customer_id": state.get("customer_id"),
                "ticket_id": state.get("ticket_id"),
            },
            "freshness_and_versions": observations,
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _payload(result: BaseModel | dict[str, Any]) -> dict[str, Any]:
        if isinstance(result, ObservationEnvelope):
            return dict(result.data)
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        return dict(result)

    @staticmethod
    def _capability_payload(result: BaseModel | dict[str, Any]) -> dict[str, Any]:
        """Rebuild the Action MCP's canonical effect receipt for ledger settlement."""

        if isinstance(result, ObservationEnvelope):
            if result.status != "ok":
                return dict(result.data)
            return {
                "tool_call_id": result.tool_call_id,
                "ticket_id": result.ticket_id,
                **dict(result.data),
                "source_refs": [item.model_dump(mode="json") for item in result.source_refs],
            }
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        return dict(result)

    async def _proposal_is_durable(
        self,
        state: AgentState,
        proposal: dict[str, Any],
        *,
        action_name: str,
        eligibility: ProposalEligibility,
    ) -> bool:
        proposal_id = proposal.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            return False
        if self.session is None:
            return True
        expected_action = {
            "propose_refund": "refund",
            "propose_api_key_revocation": "api_key_revocation",
            "propose_entitlement_change": "entitlement_change",
        }.get(action_name)
        binding = eligibility.observation_binding
        if expected_action is None:
            return False
        action_spec = get_action_spec(cast(Any, expected_action))
        if (
            action_name != action_spec.policy_capability
            or eligibility.action_type != action_spec.action_type
            or len(binding) != len(action_spec.obligations)
        ):
            return False
        for obligation in action_spec.obligations:
            matching_bindings = [
                item for item in binding if item.get("tool_name") in obligation.capabilities
            ]
            if len(matching_bindings) != 1:
                return False
        resource_obligations = [
            item for item in action_spec.obligations if item.observed_resource_field is not None
        ]
        if len(resource_obligations) != 1:
            return False
        resource_field = resource_obligations[0].observed_resource_field
        resources = [item for item in binding if item.get("resource_field") == resource_field]
        if len(resources) != 1:
            return False
        resource_id = eligibility.resource_id
        resource_version = eligibility.resource_version
        if not isinstance(resource_id, str) or not isinstance(resource_version, int):
            return False
        if (
            resources[0].get("resource_id") != resource_id
            or resources[0].get("resource_version") != resource_version
        ):
            return False
        row = await self.session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.id == proposal_id,
                ProposalRecord.tenant_id == state["tenant_id"],
                ProposalRecord.run_id == state["run_id"],
                ProposalRecord.action_type == expected_action,
                ProposalRecord.resource_id == resource_id,
                ProposalRecord.resource_version == resource_version,
                ProposalRecord.status.in_(("draft", "bound")),
            )
        )
        if row is None:
            return False
        payload = dict(row.action_payload)
        if expected_action == "refund":
            if payload.get("billing_record_id") != resource_id:
                return False
        elif expected_action == "api_key_revocation":
            if payload.get("api_key_id") != resource_id:
                return False
        elif expected_action == "entitlement_change":
            if payload.get("subscription_id") != resource_id:
                return False
            change_type = payload.get("change_type")
            if change_type not in {"quota_change", "plan_change"}:
                return False
            try:
                persisted_target = validate_entitlement_target(
                    cast(Any, change_type),
                    payload.get("target"),
                )
                trusted_target = validate_entitlement_target(
                    cast(Any, eligibility.trusted_arguments.get("change_type")),
                    eligibility.trusted_arguments.get("target"),
                )
            except (TypeError, ValueError, ValidationError):
                return False
            if persisted_target != trusted_target:
                return False
        proposal_action_hash = proposal.get("action_hash")
        return not proposal_action_hash or proposal_action_hash == row.action_hash

    @classmethod
    def _normalize_gateway_result(
        cls,
        call: ReadToolCall,
        tool_call_id: str,
        state: AgentState,
        result: BaseModel,
    ) -> ObservationEnvelope:
        scope = {
            "tenant_id": state["tenant_id"],
            "customer_id": state["customer_id"],
            "scope_hash": hashlib.sha256(
                json.dumps(
                    {
                        "customer_id": state["customer_id"],
                        "tenant_id": state["tenant_id"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        if isinstance(result, ObservationEnvelope):
            if result.status != "ok" or result.freshness_status != "unknown":
                return result.model_copy(update=scope)
            freshness_class, freshness_status, ttl_seconds, observed_at = cls._freshness_metadata(
                result.tool_name, result.data, result.observed_at
            )
            return result.model_copy(
                update={
                    **scope,
                    "observed_at": observed_at,
                    "freshness_class": freshness_class,
                    "freshness_status": freshness_status,
                    "fresh_until": observed_at + timedelta(seconds=ttl_seconds),
                }
            )
        payload = result.model_dump(mode="json")
        source_refs = [SourceRef.model_validate(item) for item in payload.pop("source_refs", [])]
        payload.pop("tool_call_id", None)
        payload.pop("ticket_id", None)
        freshness_class, freshness_status, ttl_seconds, observed_at = cls._freshness_metadata(
            call.name, payload, datetime.now(UTC)
        )
        return ObservationEnvelope(
            tool_name=call.name,
            tool_call_id=tool_call_id,
            ticket_id=state["ticket_id"],
            run_id=state["run_id"],
            **scope,
            attempt_index=1,
            status="ok",
            retryable=False,
            source_refs=source_refs,
            observed_at=observed_at,
            freshness_class=cast(Any, freshness_class),
            freshness_status=cast(Any, freshness_status),
            fresh_until=observed_at + timedelta(seconds=ttl_seconds),
            duration_ms=0,
            data=payload,
        )

    @classmethod
    def _freshness_metadata(
        cls, tool_name: str, payload: dict[str, Any], fallback: datetime
    ) -> tuple[str, str, int, datetime]:
        observed_at = fallback
        freshness_class = "transactional"
        freshness_status = "fresh"
        ttl_seconds = 300
        current_fact_contract = current_fact_freshness_contract(tool_name)
        if current_fact_contract is not None:
            # High-risk execution still performs transactional version
            # revalidation after approval; this horizon only governs reads and
            # non-authoritative Memory projection.
            freshness_class = current_fact_contract.freshness_class
            ttl_seconds = int(current_fact_contract.lifetime.total_seconds())
        elif tool_name == "query_api_usage":
            observed_at = cls._parse_observed_at(payload.get("observed_at"), fallback)
            freshness_class = "near_real_time"
            freshness_status = str(payload.get("freshness_status", "unknown"))
            ttl_seconds = {"1m": 120, "5m": 300, "1h": 900, "24h": 3600}.get(
                str(payload.get("window")), 300
            )
        elif tool_name in {"query_request_trace", "query_incident_impact"}:
            freshness_class = "event_record"
            ttl_seconds = 86400
        elif tool_name == "check_service_status":
            freshness_class = "near_real_time"
            ttl_seconds = 120
        elif tool_name == "search_knowledge":
            freshness_class = "versioned_knowledge"
            ttl_seconds = 86400
        return freshness_class, freshness_status, ttl_seconds, observed_at

    @staticmethod
    def _parse_observed_at(value: Any, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return fallback
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return fallback

    def _read_tool_context(
        self,
        state: AgentState,
        tool_call_id: str,
        *,
        tool_name: str,
        reservation: tuple[JobLease, ReservedAttempt] | None,
        logical_invocation_id: str | None,
        transport_attempt: int,
        tool_round: int,
    ) -> ToolCallContext:
        if not state.get("job_id") or self.test_capability is not None:
            return ToolCallContext.fixture(
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                ticket_id=state["ticket_id"],
                run_id=state["run_id"],
                tool_call_id=tool_call_id,
                trace_id=state["trace_id"],
            )
        if logical_invocation_id is None:
            raise RuntimeError("read MCP logical invocation lineage is missing")
        if reservation is None or reservation[1].transport_attempt_id is None:
            raise RuntimeError("read MCP transport reservation is missing")
        lease, reserved = reservation
        call_deadline = min(
            lease.expires_at,
            datetime.now(UTC) + timedelta(seconds=10),
        )
        return ToolCallContext(
            tenant_id=state["tenant_id"],
            customer_id=state["customer_id"],
            ticket_id=state["ticket_id"],
            run_id=state["run_id"],
            job_id=state["job_id"],
            segment_id=state["segment_id"],
            delivery_generation=state["delivery_generation"],
            fencing_token=state["fencing_token"],
            tool_call_id=tool_call_id,
            trace_id=state["trace_id"],
            mcp_context=ReadMcpCallContext(
                logical_invocation_id=logical_invocation_id,
                tool_attempt_id=reserved.id,
                transport_attempt_id=reserved.transport_attempt_id,
                tool_name=tool_name,
                transport_attempt=transport_attempt,
                agent_tool_round=tool_round,
                call_deadline=call_deadline,
                worker_deadline=lease.expires_at,
                retrieval_intent=(
                    self._trusted_retrieval_intent(state)
                    if tool_name == "search_knowledge"
                    else None
                ),
            ),
        )

    @staticmethod
    def _durable_read_invocation_logical_id(
        *,
        state: AgentState,
        lease: JobLease,
        invocation: ToolInvocation | None,
        provider_tool_call_id: str,
        tool_name: str,
        arguments_hash: str,
    ) -> str:
        """Return the MCP identity only from the current durable Invocation.

        Checkpoints retain parallel Invocation-ID lists for recovery
        compatibility. The database row is the authority for a new physical
        transport, so a stale parallel logical ID cannot be paired with a
        newer Tool Round.
        """

        if (
            invocation is None
            or invocation.tenant_id != state["tenant_id"]
            or invocation.run_id != state["run_id"]
            or invocation.job_id != lease.job_id
            or invocation.turn_group_id != state.get("turn_group_id")
            or invocation.segment_id != state["segment_id"]
            or invocation.fencing_token != lease.fencing_token
            or invocation.provider_tool_call_id != provider_tool_call_id
            or invocation.tool_name != tool_name
            or invocation.arguments_hash != arguments_hash
        ):
            raise RuntimeConflict("tool_invocation_lineage_mismatch")
        return invocation.logical_invocation_id

    @classmethod
    def _trusted_retrieval_intent(cls, state: AgentState) -> RetrievalIntentEnvelope:
        """Derive temporal authority from the current customer turn only.

        A canonicalized anaphoric query may include one bounded prior customer
        message, but never an assistant answer. That customer-authored topic may
        improve recall, yet cannot supply a historical version or date. An
        unanchored historical follow-up to a prior customer topic requires both
        published evidence groups without inventing a historical anchor.
        """

        current_message = str(
            state.get("redacted_message") or state.get("user_message") or ""
        ).strip()
        direct = resolve_retrieval_intent(current_message)
        contextual_historical_follow_up = bool(
            direct.intent == "historical"
            and direct.historical_version is None
            and direct.as_of is None
            and cls._is_anaphoric_knowledge_follow_up(current_message)
            and cls._history_customer_messages(state)
        )
        if (
            (refers_to_prior_comparison_scope(current_message) or contextual_historical_follow_up)
            and direct.intent != "compare"
            and direct.historical_version is None
            and direct.as_of is None
        ):
            return RetrievalIntentEnvelope(
                intent="compare",
                reason_code=(
                    "contextual_historical_comparison_semantics"
                    if contextual_historical_follow_up
                    else "referential_comparison_semantics"
                ),
            )
        return direct

    def _tool_context(
        self,
        state: AgentState,
        *,
        approval: bool,
        observation_binding: list[dict[str, Any]] | None = None,
        capability: ReservedCapability | None = None,
        lease: JobLease | None = None,
    ) -> ToolCallContext:
        if not state.get("job_id") or self.test_capability is not None:
            return ToolCallContext.fixture(
                tenant_id=state["tenant_id"],
                customer_id=state["customer_id"],
                ticket_id=state["ticket_id"],
                run_id=state["run_id"],
                checkpoint_id=(
                    f"checkpoint:{state['run_id']}:awaiting_approval" if approval else None
                ),
                observation_binding=observation_binding,
                tool_call_id=f"tool_{uuid4().hex}",
                trace_id=state["trace_id"],
            )
        if capability is None or lease is None:
            raise ValueError("policy capability reservation is required")
        call_deadline = min(
            lease.expires_at,
            datetime.now(UTC) + timedelta(seconds=10),
        )
        return ToolCallContext(
            tenant_id=state["tenant_id"],
            customer_id=state["customer_id"],
            ticket_id=state["ticket_id"],
            run_id=state["run_id"],
            job_id=state["job_id"],
            segment_id=state["segment_id"],
            delivery_generation=state["delivery_generation"],
            fencing_token=state["fencing_token"],
            checkpoint_id=(f"checkpoint:{state['run_id']}:awaiting_approval" if approval else None),
            observation_binding=observation_binding or [],
            tool_call_id=f"tool_{uuid4().hex}",
            trace_id=state["trace_id"],
            mcp_context=PolicyCapabilityMcpCallContext(
                capability_invocation_id=capability.id,
                capability_attempt_id=capability.attempt_id,
                capability_name=capability.capability_name,
                effect_identity=capability.effect_identity,
                capability_attempt=capability.attempt_ordinal,
                capability_sequence=capability.sequence,
                causal_decision_hash=capability.causal_decision_hash,
                causal_decision=capability.causal_decision,
                observation_binding_hash=capability.observation_binding_hash,
                call_deadline=call_deadline,
                worker_deadline=lease.expires_at,
            ),
        )

    @staticmethod
    def _capability_observation_binding(state: AgentState) -> list[dict[str, Any]]:
        """Project immutable Observation identities; never hash mutable result bodies/scores."""

        return [
            {
                "tool_name": item.get("tool_name"),
                "tool_call_id": item.get("tool_call_id"),
                "invocation_id": item.get("invocation_id"),
                "observation_id": item.get("observation_id"),
                "observation_content_hash": item.get("observation_content_hash"),
                "turn_group_id": item.get("turn_group_id"),
                "status": "ok",
            }
            for item in state.get("tool_observations", [])
            if item.get("status") == "ok" and item.get("run_id") == state.get("run_id")
        ]

    def _trace(self, prompt_hash: str, state: AgentState) -> dict[str, str]:
        return {
            "trace_id": state["trace_id"],
            "run_id": state["run_id"],
            "ticket_id": state["ticket_id"],
            "prompt_hash": prompt_hash,
            "context_version": self.budget.version,
            "runtime_manifest_hash": self.runtime_manifest.content_hash,
            "runtime_prompt_version": self.runtime_manifest.prompt_version,
            "runtime_prompt_hash": self.runtime_manifest.prompt_hash,
            "runtime_schema_version": self.runtime_manifest.schema_version,
            "runtime_schema_hash": self.runtime_manifest.schema_hash,
            "embedding_fingerprint": self.runtime_manifest.embedding_fingerprint,
            "code_commit": self.runtime_manifest.code_commit,
        }

    async def _event(
        self,
        state: AgentState,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: str = "internal",
        status: str = "completed",
        tool_call_id: str | None = None,
        tool_round: int | None = None,
    ) -> None:
        if state.get("job_id"):
            self.segment_events.append(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "visibility": visibility,
                    "status": status,
                    "tool_call_id": tool_call_id,
                    "step_index": state.get("step_index", 0),
                    "tool_round": (
                        tool_round if tool_round is not None else state.get("tool_rounds", 0)
                    ),
                }
            )
            return None
        if self.session is None:
            return None
        run = await self.session.get(AgentRun, state["run_id"])
        if run is None:
            raise RuntimeError("Agent Run disappeared during execution")
        await AgentRunStore(self.session).append_event(
            run,
            event_type=event_type,
            payload=payload,
            visibility=cast(Any, visibility),
            status=status,
            tool_call_id=tool_call_id,
            step_index=state.get("step_index", run.step_index),
            tool_round=tool_round,
        )
        await self.session.commit()

    async def _transition(self, state: AgentState, **values: Any) -> None:
        if state.get("job_id"):
            return
        if self.session is None:
            return
        run = await self.session.get(AgentRun, state["run_id"])
        if run is None:
            raise RuntimeError("Agent Run disappeared during transition")
        await AgentRunStore(self.session).transition(run, **values)
        await self.session.commit()

    async def _reserve_external(
        self,
        state: AgentState,
        kind: str,
        *,
        logical_invocation_id: str | None = None,
        transport_ordinal: int | None = None,
        repair_of_attempt_id: str | None = None,
    ) -> tuple[JobLease, ReservedAttempt] | None:
        lease = await self._current_lease(state)
        if self.session is None or lease is None:
            return None
        reserved = await AttemptLedger(self.session).reserve(
            lease,
            kind=kind,
            logical_invocation_id=logical_invocation_id,
            transport_ordinal=transport_ordinal,
            actual_provider=(
                self.provider.model,
                self.provider.mode,
                self.provider.tool_call_mode,
            ),
            repair_of_attempt_id=repair_of_attempt_id,
        )
        await self.session.commit()
        return lease, reserved

    async def _reserve_tool_round(self, state: AgentState) -> None:
        lease = await self._current_lease(state)
        if self.session is None or lease is None:
            return
        await AttemptLedger(self.session).reserve_tool_round(lease)
        await self.session.commit()

    async def _current_lease(self, state: AgentState) -> JobLease | None:
        if self.session is None or not state.get("job_id"):
            return None
        is_worker_postgres = False
        if self.session.get_bind().dialect.name == "postgresql":
            is_worker_postgres = (
                await self.session.scalar(text("SELECT session_user")) == "supportguard_worker"
            )
        if not is_worker_postgres:
            job = await self.session.get(RuntimeJob, state["job_id"])
            if (
                job is None
                or job.lease_owner is None
                or job.lease_expires_at is None
                or job.fencing_token != state.get("fencing_token")
            ):
                raise RuntimeConflict("stale_fencing_token")
            return JobLease(
                job.id,
                job.run_id,
                job.tenant_id,
                job.lease_owner,
                job.fencing_token,
                job.lease_expires_at,
                job.kind,
                job.approval_id,
                job.attempt,
            )
        worker = worker_execution_context.get()
        if (
            worker.job_id != state["job_id"]
            or worker.run_id != state.get("run_id")
            or worker.tenant_id != state.get("tenant_id")
            or worker.fencing_token != state.get("fencing_token")
        ):
            raise RuntimeConflict("stale_fencing_token")
        lease = JobLease(
            worker.job_id,
            worker.run_id,
            worker.tenant_id,
            worker.executor_service_principal,
            worker.fencing_token,
            worker.deadline,
        )
        return await RuntimeJobRepository(self.session).refresh_lease(lease)

    async def _finish_external(
        self,
        reservation: tuple[JobLease, ReservedAttempt] | None,
        *,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: str | None = None,
        provider_transport_attempts: int | None = None,
        transport_lifecycle: dict[str, object] | None = None,
        structured_error_paths: list[str] | None = None,
    ) -> None:
        if self.session is None or reservation is None:
            return
        lease, reserved = reservation
        await AttemptLedger(self.session).finish(
            lease,
            reserved,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=error_code,
            provider_transport_attempts=provider_transport_attempts,
            transport_lifecycle=transport_lifecycle,
            structured_error_paths=structured_error_paths,
        )
        await self.session.commit()

    @staticmethod
    def _exception_transport_attempts(exc: Exception) -> int | None:
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            value = getattr(current, "transport_attempts", None)
            if isinstance(value, int) and value in {1, 2}:
                return value
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _provider_failure_error_code(exc: BaseException) -> str:
        return provider_error_code(exc)

    async def _finish_tool_terminal(
        self,
        reservation: tuple[JobLease, ReservedAttempt] | None,
        *,
        invocation_id: str | None,
        observation: ObservationEnvelope,
        attempt_status: str,
        invocation_outcome: str | None = None,
        trusted_retrieval_intent: dict[str, Any] | None = None,
    ) -> ToolObservation | None:
        """Commit output validation, transport outcome and one Observation together."""

        if self.session is None or reservation is None or invocation_id is None:
            return None
        lease, reserved = reservation
        await AttemptLedger(self.session).finish(
            lease,
            reserved,
            status=attempt_status,
            error_code=observation.error_code,
            transport_lifecycle=observation.transport_lifecycle,
        )
        persisted = await ToolLedger(self.session).terminalize(
            lease,
            invocation_id,
            outcome=invocation_outcome or self._observation_outcome(observation),
            observation=observation,
            trusted_retrieval_intent=trusted_retrieval_intent,
        )
        await self.session.commit()
        return persisted

    async def _terminalize_tool_without_attempt(
        self,
        lease: JobLease | None,
        *,
        invocation_id: str | None,
        observation: ObservationEnvelope,
        trusted_retrieval_intent: dict[str, Any] | None = None,
    ) -> ToolObservation | None:
        """Persist a terminal Observation when recovery fails before a resend."""

        if self.session is None or lease is None or invocation_id is None:
            return None
        persisted = await ToolLedger(self.session).terminalize(
            lease,
            invocation_id,
            outcome=self._observation_outcome(observation),
            observation=observation,
            trusted_retrieval_intent=trusted_retrieval_intent,
        )
        await self.session.commit()
        return persisted

    async def _reserve_capability(
        self,
        state: AgentState,
        capability_name: str,
        *,
        model_arguments: dict[str, Any],
        observation_binding: list[dict[str, Any]],
    ) -> tuple[JobLease, ReservedCapability] | None:
        lease = await self._current_lease(state)
        if self.session is None or lease is None:
            return None
        binding_hash = canonical_json_hash(observation_binding)
        spec = get_action_spec_by_policy_capability(capability_name)
        if spec is None or not observation_binding:
            raise RuntimeConflict("policy_capability_binding_missing")
        resource = observation_binding[0]
        idempotency_prefix = {
            "refund": "refund",
            "api_key_revocation": "key-revoke",
            "entitlement_change": "entitlement",
        }[spec.action_type]
        model_arguments["idempotency_key"] = (
            f"{idempotency_prefix}:{state['ticket_id']}:{resource['resource_id']}"
        )
        causal_decision = ProposalCausalDecisionV2(
            capability_name=spec.policy_capability,
            action_type=spec.action_type,
            resource_id=str(resource["resource_id"]),
            resource_version=int(resource["resource_version"]),
            model_arguments=model_arguments,
            observation_binding_hash=binding_hash,
            policy_version="supportguard-policy-gate.v1",
        )
        reserved = await PolicyCapabilityLedger(self.session).reserve(
            lease,
            segment_id=state["segment_id"],
            capability_name=capability_name,
            causal_decision=causal_decision,
            observation_binding=observation_binding,
        )
        await self.session.commit()
        return lease, reserved

    async def _finish_capability(
        self,
        reservation: tuple[JobLease, ReservedCapability] | None,
        *,
        status: str,
        error_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> PolicyCapabilityResult | None:
        if self.session is None or reservation is None:
            return None
        lease, reserved = reservation
        result = await PolicyCapabilityLedger(self.session).finish(
            lease,
            reserved,
            status=status,
            error_code=error_code,
            payload=payload,
        )
        await self.session.commit()
        return result

    async def _persist_context_ledger(
        self,
        state: AgentState,
        reservation: tuple[JobLease, ReservedAttempt] | None,
        *,
        component_manifest: dict[str, Any],
        transport: ProviderTransportRecord | None,
        require_capture: bool = True,
        ledger_id: str | None = None,
        binding_plans: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if self.session is None or reservation is None:
            return None
        if transport is None:
            if require_capture:
                raise RuntimeConflict("provider_transport_not_captured")
            return None
        estimated_tokens = max(1, (len(transport.request_bytes) + 2) // 3)
        max_input_tokens = int(getattr(self.provider, "max_input_tokens", 8000))
        if estimated_tokens > max_input_tokens:
            raise RuntimeConflict("provider_transport_budget_exceeded")
        _, reserved = reservation
        sensitivity = redact_pii(transport.request_bytes.decode(errors="replace"))
        existing = await self.session.scalar(
            select(ContextLedger).where(ContextLedger.provider_attempt_id == reserved.id)
        )
        if existing is not None:
            if existing.canonical_request_hash != transport.request_hash:
                raise RuntimeConflict("provider_transport_hash_changed")
            return existing.id
        ledger = ContextLedger(
            id=ledger_id or new_id("context"),
            tenant_id=state["tenant_id"],
            run_id=state["run_id"],
            job_id=state["job_id"],
            provider_attempt_id=reserved.id,
            serializer_version=transport.serializer_version,
            canonical_request_hash=transport.request_hash,
            canonical_request_bytes=None,
            request_storage_mode="hash_only",
            sensitivity_manifest={
                "schema": "provider-request-sensitivity.v1",
                "storage": "hash_only",
                "redaction_count": sensitivity.redaction_count,
                "rule_ids": list(sensitivity.applied_rule_ids),
                "secret_fingerprints": list(sensitivity.secret_fingerprints),
            },
            component_manifest={
                **component_manifest,
                "evidence_projection_version": EVIDENCE_PROJECTION_V2,
            },
            token_preflight={
                "schema": "transport-preflight.v1",
                "request_bytes": len(transport.request_bytes),
                "estimated_input_tokens": estimated_tokens,
                "max_input_tokens": max_input_tokens,
            },
            runtime_provenance=runtime_provenance(
                model=self.provider.model,
                provider_mode=self.provider.mode,
                tool_call_mode=self.provider.tool_call_mode,
                context_version=self.budget.version,
                code_version=self.settings.code_version,
            ),
        )
        self.session.add(ledger)
        await self.session.flush()
        pending_bindings: list[tuple[dict[str, Any], ContextMembership]] = []
        for plan in binding_plans or []:
            membership = ContextMembership(
                id=str(plan["membership_id"]),
                tenant_id=state["tenant_id"],
                run_id=state["run_id"],
                origin_job_id=str(plan["origin_job_id"]),
                origin_marker_id=str(plan["origin_marker_id"]),
                origin_fencing_token=int(plan["origin_fencing_token"]),
                origin_segment_ref=str(plan["origin_segment_ref"]),
                logical_invocation_id=str(plan["logical_invocation_id"]),
                executor_job_id=state["job_id"],
                executor_marker_id=state["segment_id"],
                executor_fencing_token=state["fencing_token"],
                provider_attempt_id=reserved.id,
                context_ledger_id=ledger.id,
                payload_ordinal=int(plan["payload_ordinal"]),
                payload_json_pointer=str(plan["payload_json_pointer"]),
                serialized_evidence_fragment_hash=str(plan["fragment_hash"]),
                ordered_membership_root_hash=str(plan["ordered_membership_root_hash"]),
                schema_version=str(plan.get("schema_version") or "context-membership.v1"),
            )
            self.session.add(membership)
            if plan.get("membership_kind", "knowledge") == "knowledge":
                pending_bindings.append((plan, membership))
        # The models intentionally do not expose a mutable ORM relationship. Flush
        # the append-only parent rows before their FK-bound citation children.
        await self.session.flush()
        for plan, membership in pending_bindings:
            binding_payload = {
                "schema_version": "citation-binding.v1",
                "citation_binding_id": str(plan["citation_binding_id"]),
                "membership_id": membership.id,
                "observation_id": str(plan["observation_id"]),
                "tool_invocation_id": str(plan["logical_invocation_id"]),
                "retrieval_trace_id": str(plan["retrieval_trace_id"]),
                "provider_attempt_id": reserved.id,
                "context_ledger_id": ledger.id,
                "selected_candidate_ordinal": int(plan["selected_candidate_ordinal"]),
                "locator_hash": str(plan["locator_hash"]),
                "temporal_selector": plan["temporal_selector"],
            }
            binding_hash = hashlib.sha256(
                json.dumps(
                    binding_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            self.session.add(
                CitationBinding(
                    id=str(plan["citation_binding_id"]),
                    tenant_id=state["tenant_id"],
                    run_id=state["run_id"],
                    origin_job_id=str(plan["origin_job_id"]),
                    membership_id=membership.id,
                    observation_id=str(plan["observation_id"]),
                    tool_invocation_id=str(plan["logical_invocation_id"]),
                    retrieval_trace_id=str(plan["retrieval_trace_id"]),
                    provider_attempt_id=reserved.id,
                    context_ledger_id=ledger.id,
                    selected_candidate_ordinal=int(plan["selected_candidate_ordinal"]),
                    locator_hash=str(plan["locator_hash"]),
                    temporal_selector=dict(plan["temporal_selector"]),
                    binding_hash=binding_hash,
                )
            )
        await self.session.flush()
        return ledger.id

    async def _persist_raw_provider_decision(
        self,
        state: AgentState,
        reservation: tuple[JobLease, ReservedAttempt] | None,
        raw: RawProviderDecision,
    ) -> RawProviderDecisionEnvelope | None:
        if self.session is None or reservation is None:
            return None
        lease, reserved = reservation
        existing = await self.session.scalar(
            select(RawProviderDecisionEnvelope).where(
                RawProviderDecisionEnvelope.provider_attempt_id == reserved.id
            )
        )
        manifest = [
            {
                "ordinal": item.ordinal,
                "provider_tool_call_id_hash": hashlib.sha256(
                    item.provider_tool_call_id.encode()
                ).hexdigest(),
                "tool_name": item.name[:100],
                "arguments_hash": hashlib.sha256(item.arguments_json.encode()).hexdigest(),
                "arguments_json_valid": self._json_object_valid(item.arguments_json),
            }
            for item in raw.tool_calls
        ]
        canonical = json.dumps(
            {
                "finish_reason": raw.finish_reason,
                "content_hash": (
                    hashlib.sha256(raw.content.encode()).hexdigest() if raw.content else None
                ),
                "calls": manifest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        response_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if existing is not None:
            if existing.response_hash != response_hash:
                raise RuntimeConflict("raw_provider_decision_changed")
            return existing
        row = RawProviderDecisionEnvelope(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=state["segment_id"],
            fencing_token=lease.fencing_token,
            provider_attempt_id=reserved.id,
            finish_reason=raw.finish_reason,
            response_hash=response_hash,
            content_hash=(
                hashlib.sha256(raw.content.encode()).hexdigest() if raw.content else None
            ),
            call_count=len(raw.tool_calls),
            call_manifest=manifest,
            intake_status="received",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    @staticmethod
    def _json_object_valid(value: str) -> bool:
        try:
            return isinstance(json.loads(value), dict)
        except (TypeError, ValueError):
            return False

    async def _persist_final(self, state: AgentState, final: FinalResponse) -> None:
        if state.get("job_id"):
            return
        if self.session is None:
            return
        ticket = await self.session.get(SupportTicket, state["ticket_id"], with_for_update=True)
        run = await self.session.get(AgentRun, state["run_id"])
        if ticket is None or run is None:
            raise RuntimeError("Ticket or Agent Run disappeared before finalization")
        ticket.status = final.terminal_state
        ticket.issue_type = str(state.get("classification", {}).get("issue_type", "unknown"))
        ticket.risk = str(state.get("classification", {}).get("risk", "low"))
        ticket.final_response = final.answer
        await AgentRunStore(self.session).transition(
            run,
            status="completed",
            checkpoint_stage="completed",
            agent_finish_reason=state.get("agent_finish_reason", "answered"),
            error_code=state.get("safe_stop_error_code") or None,
            tool_rounds=state.get("tool_rounds", 0),
            tool_attempts=state.get("tool_attempts", 0),
            llm_calls=state.get("llm_calls", 0),
        )
        if run.turn_id:
            turn = await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
            if turn is not None:
                turn.activity_state = "completed"
                turn.result_state = turn_result_for(
                    state.get("agent_finish_reason"),
                    terminal_state=final.terminal_state,
                    automation_mode=ticket.automation_mode,
                )
                turn.completed_at = run.completed_at
        existing_answer = await self.session.scalar(
            select(TicketMessage.id).where(
                TicketMessage.tenant_id == ticket.tenant_id,
                TicketMessage.publication_key == f"assistant:{run.id}",
            )
        )
        if existing_answer is None:
            ticket.next_message_sequence += 1
            advance_conversation_activity(ticket)
            self.session.add(
                TicketMessage(
                    tenant_id=ticket.tenant_id,
                    ticket_id=ticket.id,
                    turn_id=run.turn_id,
                    run_id=run.id,
                    conversation_sequence=ticket.next_message_sequence,
                    message_kind="assistant",
                    publication_key=f"assistant:{run.id}",
                    role="assistant",
                    content=redact_pii(final.answer).text,
                    source_refs=[],
                )
            )
        await AgentRunStore(self.session).append_event(
            run,
            event_type="final_outcome",
            payload={
                "terminal_state": final.terminal_state,
                "policy_route": final.policy_route,
                "agent_finish_reason": state.get("agent_finish_reason", "answered"),
            },
            visibility="customer",
        )
        await activate_next_turn(
            self.session,
            ticket=ticket,
            trace_id=f"turn-dispatch:{run.id}",
        )
        await self.session.commit()
