from __future__ import annotations

from typing import Any, TypedDict


class TopicContinuityResolution(TypedDict):
    query: str
    topic_anchor_applied: bool
    anchor_source: str | None
    anaphoric_chain_length: int
    query_sha256: str
    query_length: int


class AgentState(TypedDict, total=False):
    tenant_id: str
    ticket_id: str
    customer_id: str
    run_id: str
    job_id: str
    segment_id: str
    delivery_generation: int
    fencing_token: int
    trace_id: str
    customer_message_id: str
    conversation_turn_id: str
    user_message: str
    redacted_message: str
    redaction_count: int
    ingress_redaction_count: int
    graph_additional_redaction_count: int
    redaction_rule_ids: list[str]
    current_actions: list[dict[str, Any]]
    action_state_query: dict[str, Any]
    classification_context: list[dict[str, Any]]
    classification_context_omissions: list[dict[str, str]]
    classification: dict[str, Any]
    action_admission: dict[str, Any]
    action_obligation_ledger: dict[str, Any]
    terminal_business_outcome: dict[str, Any]
    context_citation_bindings: list[dict[str, Any]]
    obligation_synthesis_mode: bool
    relevant_history: list[dict[str, Any]]
    agent_decision: dict[str, Any]
    provider_turns: list[dict[str, Any]]
    tool_observations: list[dict[str, Any]]
    latest_observations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    evidence_conflict: bool
    knowledge_comparison_requested: bool
    knowledge_comparison_complete: bool
    executed_fingerprints: list[str]
    candidate: dict[str, Any]
    policy_route: str
    citation_integrity: bool
    proposal_eligibility: dict[str, Any]
    action_candidate: dict[str, Any]
    structure_repair_used: bool
    action_result: dict[str, Any]
    human_decision: dict[str, Any]
    approval_decision: dict[str, Any]
    runtime_effect_result: dict[str, Any]
    execution_result: dict[str, Any]
    final: dict[str, Any]
    llm_calls: int
    tool_rounds: int
    tool_attempts: int
    agent_finish_reason: str
    step_index: int
    segment_events: list[dict[str, Any]]
    turn_group_id: str
    tool_invocation_ids: list[str]
    tool_logical_invocation_ids: list[str]
    latest_provider_attempt_id: str
    latest_context_ledger_id: str
    citation_binding_map: dict[str, dict[str, Any]]
    validated_answer: str
    tool_round_rejected: bool
    evidence_assessment: dict[str, Any]
    evidence_decision: dict[str, Any]
    evidence_replan_required: bool
    evidence_replan_count: int
    safe_stop_reason: str
    safe_stop_error_code: str
