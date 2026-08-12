from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from supportguard.agent.contracts import CONTEXT_VERSION
from supportguard.agent.current_facts import requested_current_fact_projection
from supportguard.agent.evidence import (
    comparison_transition_markers,
    observation_is_fresh,
    referential_applicability_contract,
)
from supportguard.agent.patterns import (
    SUBSCRIPTION_CURRENT_FACT_FIELD,
    SUBSCRIPTION_POLICY_OR_OPERATIONAL_REQUEST,
)
from supportguard.agent.state import AgentState
from supportguard.rag.intent import resolve_retrieval_intent


class ContextBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextBudget:
    max_input_tokens: int = 8000
    evidence_tokens: int = 3400
    tool_tokens: int = 1800
    history_tokens: int = 800
    output_reserve: int = 1200
    version: str = CONTEXT_VERSION


class ContextSectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    trust_class: Literal["policy", "trusted_state", "untrusted_data"]
    source_refs: list[str]
    content_hash: str
    canonical_lineage_hash: str | None = None
    token_count: int


class ContextPacketManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_index: int
    node: str
    context_assembly_version: str
    max_input_tokens: int
    input_budget: int
    output_reserve: int
    sections: list[ContextSectionManifest]
    visible_tool_schema_hash: str
    omitted: list[dict[str, str]]
    total_input_tokens: int
    prior_turn_tokens: int = 0


class AssembledContext(BaseModel):
    content: str
    manifest: ContextPacketManifest


def count_tokens(value: Any) -> int:
    """Conservative deterministic counter calibrated for mixed Chinese/JSON context."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, (len(encoded.encode("utf-8")) + 2) // 3)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def latest_assistant_history_message(state: AgentState) -> str | None:
    """Return the latest redacted assistant text as an anaphora hint only."""

    for item in reversed(state.get("relevant_history", [])):
        if (
            isinstance(item, dict)
            and item.get("history_kind") == "message"
            and item.get("role") == "assistant"
        ):
            content = str(item.get("content", "")).strip()
            if content:
                return content
    return None


def authoritative_read_only_fact_observation(
    state: AgentState,
) -> dict[str, Any] | None:
    """Return the fresh subscription read that can answer a read-only fact."""

    classification = state.get("classification", {})
    if (
        classification.get("policy_boundary", "allowed") != "allowed"
        or classification.get("issue_type") != "entitlement_change"
        or classification.get("requested_action", "none") != "none"
        or classification.get("needs_realtime_facts") is not True
    ):
        return None
    run_id = state.get("run_id")
    if not run_id:
        return None
    return next(
        (
            item
            for item in reversed(state.get("tool_observations", []))
            if item.get("run_id") == run_id
            and item.get("tool_name") == "query_subscription"
            and item.get("status") == "ok"
            and item.get("freshness_status") == "fresh"
            and item.get("source_refs")
            and isinstance(item.get("data"), dict)
            and item["data"].get("subscription_id")
            and item["data"].get("version") is not None
        ),
        None,
    )


def authoritative_current_account_observation(
    state: AgentState,
) -> dict[str, Any] | None:
    """Return the fresh customer-scoped account read from this Agent Run."""

    run_id = state.get("run_id")
    if not run_id:
        return None
    return next(
        (
            item
            for item in reversed(state.get("tool_observations", []))
            if item.get("run_id") == run_id
            and item.get("tool_name") == "query_account"
            and item.get("status") == "ok"
            and observation_is_fresh(item)
            and item.get("source_refs")
            and isinstance(item.get("data"), dict)
            and item["data"].get("account_status") is not None
            and item["data"].get("region") is not None
        ),
        None,
    )


def authoritative_fact_completes_current_request(state: AgentState) -> bool:
    """Close reads only for a literal current subscription fact question."""

    if authoritative_read_only_fact_observation(state) is None:
        return False
    message = str(state.get("redacted_message", "")).strip()
    return bool(
        message
        and SUBSCRIPTION_CURRENT_FACT_FIELD.search(message)
        and SUBSCRIPTION_POLICY_OR_OPERATIONAL_REQUEST.search(message) is None
    )


def usable_current_knowledge_observation(
    state: AgentState,
) -> dict[str, Any] | None:
    """Return clean current-run RAG evidence for a literal current question."""

    classification = state.get("classification", {})
    if (
        classification.get("policy_boundary", "allowed") != "allowed"
        or classification.get("requested_action", "none") != "none"
        or resolve_retrieval_intent(str(state.get("redacted_message", ""))).intent != "current"
    ):
        return None
    run_id = state.get("run_id")
    if not run_id:
        return None
    return next(
        (
            item
            for item in reversed(state.get("tool_observations", []))
            if item.get("run_id") == run_id
            and item.get("tool_name") == "search_knowledge"
            and item.get("status") == "ok"
            and item.get("freshness_status") == "fresh"
            and item.get("data", {}).get("conflict") is not True
            and item.get("data", {}).get("refusal_reason") is None
            and any(
                evidence.get("supporting_span_eligible") is True
                and len(str(evidence.get("source_locator", {}).get("locator_hash") or "")) == 64
                for evidence in item.get("data", {}).get("evidence", [])
                if isinstance(evidence, dict)
            )
        ),
        None,
    )


def _base_trusted_task_state(state: AgentState) -> dict[str, Any]:
    classification = state.get("classification", {})
    return {
        "ticket_id": state["ticket_id"],
        "customer_id": state["customer_id"],
        "issue_type": classification.get("issue_type", "unknown"),
        "risk": classification.get("risk", "low"),
        "policy_boundary": classification.get("policy_boundary", "allowed"),
        "support_subject": classification.get("support_subject", "customer_problem"),
        "requested_action": classification.get("requested_action", "none"),
        "requested_concurrency_limit": classification.get("requested_concurrency_limit"),
        "action_admission": state.get("action_admission", {}),
        "action_obligations": state.get("action_obligation_ledger", {}),
        "current_actions": state.get("current_actions", []),
        "current_actions_grant_action_authority": False,
        "missing_evidence_groups": state.get("evidence_assessment", {}).get("missing_groups", []),
    }


def _add_completed_evidence_guidance(
    state: AgentState,
    trusted: dict[str, Any],
) -> None:
    if state.get("knowledge_comparison_complete", False):
        trusted["versioned_knowledge_evidence"] = {
            "status": "complete",
            "required_evidence_groups": ["current", "historical"],
            "required_transition_markers": comparison_transition_markers(state.get("evidence", [])),
            "additional_read_authorized": False,
            "instruction": (
                "Both published evidence groups are already present for this decision. "
                "Produce a grounded final candidate whose material claims cite eligible "
                "bindings from both evidence groups and directly explain every "
                "evidence-derived required_transition_marker. Keep the immediately "
                "preceding comparison as the primary focus when the current message "
                "refers to a previously mentioned limit or difference. If the "
                "observations do not support the customer's question, return a safe "
                "clarification. Do not request another Read Tool."
            ),
        }
    if usable_current_knowledge_observation(state) is not None:
        trusted["current_knowledge_evidence"] = {
            "status": "complete",
            "additional_knowledge_read_authorized": False,
            "instruction": (
                "A successful current-run knowledge observation already contains "
                "eligible evidence for the customer's current question. Produce a "
                "grounded final candidate from the existing citation bindings, or a "
                "safe clarification if those spans do not answer the question. Do not "
                "request another knowledge search."
            ),
        }
    current_fact = authoritative_read_only_fact_observation(state)
    if current_fact is not None:
        read_complete = authoritative_fact_completes_current_request(state)
        trusted["authoritative_current_fact"] = {
            "status": "complete",
            "tool_name": current_fact["tool_name"],
            "freshness_status": current_fact.get("freshness_status"),
            "additional_same_tool_read_authorized": False,
            "read_phase_complete": read_complete,
            "instruction": (
                "A fresh authoritative business observation from this Agent Run fully "
                "answers the customer's current-state question. Produce a grounded final "
                "candidate from that observation and do not request another Read Tool."
                if read_complete
                else (
                    "A fresh authoritative business observation from this Agent Run "
                    "already answers the current-state part of the customer's request. "
                    "Use that observation for a grounded final candidate. Do not call "
                    "the same Read Tool again. Use another visible Read Tool only when "
                    "the customer asks for a distinct policy or operational fact that "
                    "is not present in the observation."
                )
            ),
        }
    current_account = authoritative_current_account_observation(state)
    if current_account is not None:
        trusted["authoritative_current_account"] = {
            "status": "complete",
            "tool_name": "query_account",
            "freshness_status": current_account.get("freshness_status"),
            "additional_same_tool_read_authorized": False,
            "instruction": (
                "A fresh customer-scoped account observation is already available for "
                "the current turn. Use it together with any eligible knowledge evidence "
                "to answer account applicability. Do not call query_account again."
            ),
        }
    current_fact_projection = requested_current_fact_projection(state)
    if current_fact_projection is not None:
        trusted["requested_current_facts"] = current_fact_projection


def _previous_rejection_guidance(state: AgentState) -> dict[str, Any] | None:
    assessment = state.get("evidence_assessment", {})
    error_code = assessment.get("error_code")
    if error_code == "comparison_citation_incomplete":
        return {
            "reason_code": error_code,
            "required_evidence_groups": ["current", "historical"],
            "required_transition_markers": comparison_transition_markers(state.get("evidence", [])),
            "correction": (
                "Use the already observed current and historical published evidence. "
                "Produce a final comparison answer whose material claims cite at least "
                "one eligible citation_binding_id from each evidence_group. Directly "
                "explain the material quantified transition represented by "
                "required_transition_markers, including when the current message refers "
                "to a limit or difference mentioned earlier. These markers are "
                "evidence-derived hints, not independent facts. Do not ask for a version "
                "already present in the observations."
            ),
        }
    if error_code == "comparison_evidence_incomplete":
        return {
            "reason_code": error_code,
            "required_evidence_groups": ["current", "historical"],
            "missing_evidence_groups": list(assessment.get("missing_groups", [])),
            "correction": (
                "The executed comparison read did not return both published evidence "
                "groups. If one bounded Read Tool round remains, search again using only "
                "the product, capability, and version topic from the untrusted "
                "conversation history. Do not answer a two-version question from a "
                "current-only observation."
            ),
        }
    if error_code == "comparison_transition_incomplete":
        return {
            "reason_code": error_code,
            "required_evidence_groups": ["current", "historical"],
            "required_transition_markers": comparison_transition_markers(state.get("evidence", [])),
            "correction": (
                "Use the already observed current and historical published evidence. "
                "Treat the immediately preceding customer question as the primary focus "
                "of the current anaphoric follow-up. The final answer must directly "
                "explain the material quantified transition represented by "
                "required_transition_markers, not a different secondary limitation. "
                "Cite eligible bindings from both evidence groups. These markers are "
                "evidence-derived hints, not independent facts."
            ),
        }
    if error_code == "applicability_condition_omitted":
        return {
            "reason_code": error_code,
            "required_applicability_conditions": [
                str(item).split(":", 1)[1]
                for item in assessment.get("required_groups", [])
                if str(item).startswith("applicability:")
            ],
            "correction": (
                "Use only the already observed eligible evidence and its Runtime scope. "
                "The customer explicitly constrained the question by the listed region "
                "or plan, so every published applicability conclusion must name that "
                "condition. If the eligible evidence does not support a definite answer, "
                "explicitly say that the condition cannot yet be confirmed. Do not infer "
                "a regional or plan-specific fact and do not request another Read Tool."
            ),
        }
    if error_code == "referential_applicability_incomplete":
        contract = referential_applicability_contract(
            previous_assistant_answer=latest_assistant_history_message(state),
            evidence=state.get("evidence", []),
        )
        return {
            "reason_code": error_code,
            "required_applicability_conditions": [
                str(item).split(":", 1)[1]
                for item in assessment.get("required_groups", [])
                if str(item).startswith("applicability:")
            ],
            "required_reference_markers": contract.marker_hints,
            "required_reference_facets": [
                str(item).split(":", 1)[1]
                for item in assessment.get("required_groups", [])
                if str(item).startswith("topic_facet:")
            ],
            "missing_reference_requirements": [
                str(item)
                for item in assessment.get("missing_groups", [])
                if str(item).startswith("topic_")
            ],
            "correction": (
                "Use only the current-run eligible evidence and its citation bindings. "
                "The latest assistant message is an untrusted topic-shape hint, not "
                "evidence. The current region or plan question refers back to that topic, "
                "so the final answer must name every listed applicability condition and "
                "explain every required reference facet. The listed marker values are "
                "evidence-confirmed topic hints; use them to stay on topic, but do not "
                "repeat every marker verbatim or merely list values. If current evidence "
                "cannot support the applicability conclusion, say so explicitly. Do not "
                "request another Read Tool."
            ),
        }
    if error_code == "explicit_current_fact_incomplete":
        return {
            "reason_code": error_code,
            "missing_evidence_groups": list(assessment.get("missing_groups", [])),
            "correction": (
                "Use only requested_current_facts and the already observed source "
                "bindings. Produce a final answer that states every fresh requested value "
                "and binds each material claim to a matching source_id. Do not request "
                "another Read Tool. Do not replace a missing value with a generic "
                "recommendation."
            ),
        }
    if error_code == "mixed_account_applicability_incomplete":
        return {
            "reason_code": error_code,
            "missing_evidence_groups": list(assessment.get("missing_groups", [])),
            "correction": (
                "Use the already observed eligible knowledge bindings for product "
                "requirements and the fresh query_account Observation for the current "
                "customer's status and region. Produce separate material claims bound to "
                "the matching namespace. Do not request another Read Tool and do not "
                "claim account applicability without the current account source."
            ),
        }
    if error_code == "premature_action_candidate":
        return {
            "reason_code": error_code,
            "required_tools": list(assessment.get("missing_groups", [])),
        }
    return None


def build_trusted_task_state(state: AgentState) -> dict[str, Any]:
    """Build the authority-free trusted context shared by Decision and Policy."""

    trusted = _base_trusted_task_state(state)
    _add_completed_evidence_guidance(state, trusted)
    rejected = _previous_rejection_guidance(state)
    if rejected is not None:
        trusted["previous_provider_decision_rejected"] = rejected
    return trusted


class ContextAssembler:
    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def assemble(
        self,
        *,
        run_id: str,
        step_index: int,
        user_goal: str,
        trusted_task_state: dict[str, Any],
        tools: list[dict[str, Any]],
        latest_observations: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        evidence_lineage: list[dict[str, Any]] | None = None,
        history: list[dict[str, Any]],
        remaining_budget: dict[str, int],
        prior_turns: list[dict[str, Any]] | None = None,
    ) -> AssembledContext:
        input_budget = self.budget.max_input_tokens - self.budget.output_reserve
        protocol_tokens = count_tokens(tools) + count_tokens(prior_turns or [])
        packet_budget = input_budget - protocol_tokens
        mandatory = {
            "user_goal": user_goal,
            "trusted_task_state": trusted_task_state,
            "remaining_budget": remaining_budget,
            "stop_conditions": {
                "max_tool_rounds": 2,
                "max_tool_attempts": 6,
                "duplicate_non_retry_call": "stop_no_progress",
                "write_tools": "never_model_visible",
            },
        }
        if packet_budget <= 0 or count_tokens(mandatory) > packet_budget:
            raise ContextBudgetExceeded("mandatory context exceeds model input budget")

        omitted: list[dict[str, str]] = []
        observations = self._fit_list(
            latest_observations,
            self.budget.tool_tokens,
            "latest_observations",
            omitted,
            protected=True,
        )
        selected_evidence = self._fit_list(
            evidence,
            self.budget.evidence_tokens,
            "retrieved_evidence",
            omitted,
            protected=True,
        )
        selected_history = self._fit_history(
            history,
            self.budget.history_tokens,
            omitted,
        )
        packet: dict[str, Any] = {
            **mandatory,
            "latest_observations": observations,
            "retrieved_evidence": selected_evidence,
            "relevant_history": selected_history,
        }
        while count_tokens(packet) > packet_budget:
            removable_index = next(
                (
                    index
                    for index in range(len(selected_history) - 1, -1, -1)
                    if selected_history[index].get("retention") != "latest_pair"
                ),
                None,
            )
            if removable_index is None:
                raise ContextBudgetExceeded(
                    "protected current facts, policy evidence, or claim support cannot fit"
                )
            removed = selected_history.pop(removable_index)
            omitted.append(
                self._history_omission(
                    removed,
                    reason="global_input_budget_message"
                    if removed.get("history_kind") == "message"
                    else "global_input_budget_summary",
                )
            )

        section_specs = [
            ("user_goal", "untrusted_data", ["ticket_message"]),
            ("trusted_task_state", "trusted_state", ["agent_run"]),
            ("remaining_budget", "trusted_state", ["agent_run"]),
            ("stop_conditions", "policy", ["runtime_policy"]),
            ("latest_observations", "untrusted_data", ["mcp_observation"]),
            ("retrieved_evidence", "untrusted_data", ["knowledge_chunk"]),
            ("relevant_history", "untrusted_data", ["ticket_summary"]),
        ]
        manifests = [
            ContextSectionManifest(
                name=name,
                trust_class=cast(Literal["policy", "trusted_state", "untrusted_data"], trust),
                source_refs=refs,
                content_hash=_hash(packet[name]),
                canonical_lineage_hash=(
                    _hash(evidence_lineage)
                    if name == "retrieved_evidence" and evidence_lineage is not None
                    else None
                ),
                token_count=count_tokens(packet[name]),
            )
            for name, trust, refs in section_specs
        ]
        manifest = ContextPacketManifest(
            run_id=run_id,
            step_index=step_index,
            node="agent_decide",
            context_assembly_version=self.budget.version,
            max_input_tokens=self.budget.max_input_tokens,
            input_budget=input_budget,
            output_reserve=self.budget.output_reserve,
            sections=manifests,
            visible_tool_schema_hash=_hash(tools),
            omitted=omitted,
            total_input_tokens=(
                count_tokens(packet) + count_tokens(tools) + count_tokens(prior_turns or [])
            ),
            prior_turn_tokens=count_tokens(prior_turns or []),
        )
        if manifest.total_input_tokens > self.budget.max_input_tokens:
            raise ContextBudgetExceeded("tool schemas and context exceed model input budget")
        return AssembledContext(
            content=json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            manifest=manifest,
        )

    @classmethod
    def _fit_history(
        cls,
        values: list[dict[str, Any]],
        token_budget: int,
        omitted: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Fit conversation history one message at a time.

        Historical checkpoints may contain the legacy
        ``current_conversation_recent_messages`` wrapper. Expanding that
        wrapper before accounting prevents six messages from being charged and
        discarded as one opaque object. The most recent customer/response pair
        is protected; summaries and older messages remain individually
        omittable historical data.
        """

        normalized = cls._normalize_history(values, omitted)
        if not normalized:
            return []

        # History is untrusted. A persisted legacy payload cannot promote
        # arbitrary messages into the protected set by supplying a retention
        # marker; protection is derived again from canonical ordering/roles.
        for value in normalized:
            if value.get("history_kind") == "message":
                value.pop("retention", None)
        pair_indices = cls._latest_pair_indices(normalized)
        for index in pair_indices:
            normalized[index]["retention"] = "latest_pair"

        protected_cost = sum(count_tokens(normalized[index]) for index in pair_indices)
        if protected_cost > token_budget:
            raise ContextBudgetExceeded("protected recent history pair exceeds budget")

        selected_indices = set(pair_indices)
        used = protected_cost

        current_summaries = [
            index
            for index, value in enumerate(normalized)
            if value.get("history_kind") == "ticket_summary"
            and value.get("current_ticket") is True
            and index not in selected_indices
        ]
        message_candidates = [
            index
            for index in range(len(normalized) - 1, -1, -1)
            if normalized[index].get("history_kind") == "message" and index not in selected_indices
        ]
        summary_candidates = [
            index
            for index in range(len(normalized) - 1, -1, -1)
            if normalized[index].get("history_kind") != "message"
            and index not in selected_indices
            and index not in current_summaries
        ]
        for index in [*current_summaries, *message_candidates, *summary_candidates]:
            value = normalized[index]
            cost = count_tokens(value)
            if used + cost > token_budget:
                omitted.append(
                    cls._history_omission(
                        value,
                        reason=(
                            "history_section_budget_older_message"
                            if value.get("history_kind") == "message"
                            else "history_section_budget_summary"
                        ),
                    )
                )
                continue
            selected_indices.add(index)
            used += cost

        return [value for index, value in enumerate(normalized) if index in selected_indices]

    @classmethod
    def _normalize_history(
        cls,
        values: list[dict[str, Any]],
        omitted: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for outer_index, value in enumerate(values):
            nested = value.get("current_conversation_recent_messages")
            if isinstance(nested, list):
                for nested_index, item in enumerate(nested):
                    if not isinstance(item, dict):
                        continue
                    message = dict(item)
                    message.setdefault("history_kind", "message")
                    message.setdefault("legacy_checkpoint", True)
                    if not message.get("message_id"):
                        message["message_id"] = (
                            f"legacy-checkpoint-history-{outer_index}-{nested_index}"
                        )
                        message["synthetic_message_id"] = True
                    message.setdefault("historical", True)
                    message.setdefault("trusted", False)
                    flattened.append(message)
                remainder = {
                    key: item
                    for key, item in value.items()
                    if key != "current_conversation_recent_messages"
                }
                if remainder:
                    remainder.setdefault("history_kind", "legacy_summary")
                    remainder.setdefault(
                        "history_item_id",
                        f"legacy-summary-{outer_index}",
                    )
                    flattened.append(remainder)
                continue

            item = dict(value)
            if item.get("history_kind") == "message" or (
                item.get("role") in {"customer", "user", "assistant", "action"}
                and "content" in item
            ):
                item["history_kind"] = "message"
                if not item.get("message_id"):
                    if item.get("legacy_checkpoint") is not True:
                        raise ContextBudgetExceeded(
                            "history message is missing canonical message_id"
                        )
                    item["message_id"] = f"legacy-checkpoint-history-{outer_index}"
                    item["synthetic_message_id"] = True
                item.setdefault("historical", True)
                item.setdefault("trusted", False)
            else:
                item.setdefault("history_kind", "ticket_summary")
                item.setdefault(
                    "history_item_id",
                    str(
                        item.get("summary_id")
                        or item.get("ticket_id")
                        or f"history-summary-{outer_index}"
                    ),
                )
            flattened.append(item)

        normalized: list[dict[str, Any]] = []
        seen_message_ids: dict[str, str] = {}
        seen_other_hashes: set[str] = set()
        for value in flattened:
            if value.get("history_kind") == "message":
                message_id = str(value["message_id"])
                content = str(value.get("content", ""))
                identity_hash = _hash(
                    {
                        "role": value.get("role"),
                        "content": content,
                    }
                )
                existing_hash = seen_message_ids.get(message_id)
                if existing_hash is not None:
                    if existing_hash != identity_hash:
                        raise ContextBudgetExceeded(
                            f"history message identity conflict: {message_id}"
                        )
                    omitted.append(
                        cls._history_omission(
                            value,
                            reason="duplicate_history_message_id",
                        )
                    )
                    continue
                seen_message_ids[message_id] = identity_hash
                normalized.append(value)
                continue

            content_hash = _hash(value)
            if content_hash in seen_other_hashes:
                omitted.append(
                    cls._history_omission(
                        value,
                        reason="duplicate_history_summary",
                    )
                )
                continue
            seen_other_hashes.add(content_hash)
            normalized.append(value)
        return normalized

    @staticmethod
    def _latest_pair_indices(values: list[dict[str, Any]]) -> list[int]:
        response_index = next(
            (
                index
                for index in range(len(values) - 1, -1, -1)
                if values[index].get("history_kind") == "message"
                and values[index].get("role") in {"assistant", "action"}
            ),
            None,
        )
        if response_index is None:
            customer_index = next(
                (
                    index
                    for index in range(len(values) - 1, -1, -1)
                    if values[index].get("history_kind") == "message"
                    and values[index].get("role") in {"customer", "user"}
                ),
                None,
            )
            return [] if customer_index is None else [customer_index]
        customer_index = next(
            (
                index
                for index in range(response_index - 1, -1, -1)
                if values[index].get("history_kind") == "message"
                and values[index].get("role") in {"customer", "user"}
            ),
            None,
        )
        return [response_index] if customer_index is None else [customer_index, response_index]

    @staticmethod
    def _history_omission(
        value: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, str]:
        history_kind = str(value.get("history_kind") or "history_item")
        omission = {
            "section": "relevant_history",
            "history_kind": history_kind,
            "reason": reason,
        }
        if history_kind == "message":
            omission["message_id"] = str(
                value.get("message_id") or value.get("history_item_id") or "message-unknown"
            )
        elif history_kind == "ticket_summary":
            omission["summary_id"] = str(
                value.get("summary_id")
                or value.get("history_item_id")
                or value.get("ticket_id")
                or "summary-unknown"
            )
        else:
            omission["history_item_id"] = str(
                value.get("history_item_id")
                or value.get("message_id")
                or value.get("summary_id")
                or value.get("ticket_id")
                or "history-item-unknown"
            )
        return omission

    @staticmethod
    def _fit_list(
        values: list[dict[str, Any]],
        token_budget: int,
        section: str,
        omitted: list[dict[str, str]],
        *,
        protected: bool = False,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        used = 0
        seen: set[str] = set()
        for value in values:
            content_hash = _hash(value)
            if content_hash in seen:
                omitted.append({"section": section, "reason": "duplicate"})
                continue
            cost = count_tokens(value)
            if used + cost > token_budget:
                if protected:
                    raise ContextBudgetExceeded(
                        f"protected context section exceeds budget: {section}"
                    )
                omitted.append({"section": section, "reason": "section_budget"})
                continue
            selected.append(value)
            seen.add(content_hash)
            used += cost
        return selected
