from __future__ import annotations

from typing import Literal

TurnResult = Literal[
    "answered",
    "answered_limited",
    "needs_clarification",
    "refused",
    "proposal_created",
    "human_queue",
    "failed",
    "stale",
    "rejected",
    "withdrawn",
]

SafeStopTerminalState = Literal["resolved", "failed"]

TURN_RESULTS: frozenset[str] = frozenset(
    {
        "answered",
        "answered_limited",
        "needs_clarification",
        "refused",
        "proposal_created",
        "human_queue",
        "failed",
        "stale",
        "rejected",
        "withdrawn",
    }
)

FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "provider_failed",
        "provider_decision_invalid",
        "provider_terminal_schema_invalid",
        "tool_failed",
        "policy_failed",
        "proposal_eligibility_failed",
        "proposal_not_durable",
        "citation_binding_incomplete",
        "insufficient_evidence",
        "no_progress",
        "budget_exhausted",
        "tool_round_budget_exhausted",
        "tool_attempt_budget_exhausted",
        "llm_call_budget_exhausted",
        "context_budget_exhausted",
        "time_budget_exhausted",
        "logical_degradation",
        "binding_stale",
    }
)

CUSTOMER_RESOLVED_STOP_REASONS: frozenset[str] = frozenset(
    {
        # A current business fact can prove that a requested action is no
        # longer admissible (for example, an already-refunded billing record).
        # The action is blocked, but the support question has been answered.
        "obligation_conflict",
        "terminal_business_outcome",
    }
)


def safe_stop_terminal_state(finish_reason: str) -> SafeStopTerminalState:
    """Separate resolved business contradictions from runtime failures."""
    if finish_reason in CUSTOMER_RESOLVED_STOP_REASONS:
        return "resolved"
    return "failed"


def turn_result_for(
    finish_reason: str | None,
    *,
    terminal_state: str | None = None,
    automation_mode: str = "agent",
) -> TurnResult:
    """Project an orthogonal customer result without inventing human work."""
    if finish_reason == "needs_clarification" or terminal_state == "needs_clarification":
        return "needs_clarification"
    if finish_reason in {
        "evidence_freshness_insufficient",
        "applicability_condition_unresolved",
        "mixed_account_applicability_incomplete",
        "explicit_current_fact_incomplete",
    }:
        return "answered_limited"
    if finish_reason == "rejected" and terminal_state == "rejected":
        return "rejected"
    if finish_reason == "rejected":
        return "refused"
    if finish_reason == "withdrawn" or terminal_state == "withdrawn":
        return "withdrawn"
    if finish_reason == "stale" or terminal_state == "stale":
        return "stale"
    if finish_reason in {"refused", "out_of_scope"}:
        return "refused"
    if finish_reason in {"proposed", "proposal_created"} or terminal_state == "awaiting_approval":
        return "proposal_created"
    if finish_reason == "manual_takeover" and automation_mode == "human_queue":
        return "human_queue"
    if (
        finish_reason == "manual_takeover"
        or finish_reason in FAILURE_REASONS
        or terminal_state in {"failed", "manual_takeover"}
    ):
        return "failed"
    return "answered"


def activity_label(
    *,
    lifecycle: str,
    automation_mode: str,
    latest_result: str | None,
    has_running: bool,
    has_queued: bool,
    has_pending_action: bool,
    has_executing_action: bool = False,
    has_completed_action: bool = False,
) -> str:
    if has_executing_action:
        return "正在执行已批准操作"
    if has_pending_action:
        return "等待审批"
    if has_completed_action:
        return "操作已完成"
    if has_running:
        return "正在处理"
    if has_queued:
        return "排队中"
    if automation_mode == "human_queue":
        return "自动处理已停止"
    labels = {
        "answered": "已回答",
        "answered_limited": "已给出有限结论",
        "needs_clarification": "需要补充信息",
        "refused": "请求未执行",
        "proposal_created": "等待审批",
        "human_queue": "自动处理已停止",
        "failed": "本轮未完成",
        "stale": "业务事实已变化",
        "rejected": "审批者已拒绝",
        "withdrawn": "申请已撤回",
    }
    if latest_result in labels:
        return labels[latest_result]
    if lifecycle == "archived":
        return "已归档"
    return "等待处理"
