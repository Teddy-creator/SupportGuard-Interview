from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from supportguard.agent.policy import PolicyRoute
from supportguard.agent.responses import safe_failure_answer
from supportguard.agent.schemas import AgentDecision, CandidateResponse, FinalResponse
from supportguard.agent.state import AgentState
from supportguard.services.turn_results import safe_stop_terminal_state


class SafeStopHost(Protocol):
    async def _event(
        self,
        state: AgentState,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: str = "technical",
        status: str = "completed",
    ) -> None: ...


class FinalizationHost(SafeStopHost, Protocol):
    def _render_validated_answer(
        self,
        candidate: CandidateResponse,
        *,
        route: PolicyRoute,
        finish_reason: str | None,
        integrity: bool,
        issue_type: str = "unknown",
        requested_action: str = "none",
        explicit_first_step: bool = False,
    ) -> str: ...

    def _requests_explicit_first_step(self, message: str) -> bool: ...

    async def _persist_final(self, state: AgentState, final: FinalResponse) -> None: ...


def failed_current_tool_observation(state: AgentState, tool_name: str) -> bool:
    run_id = str(state.get("run_id", ""))
    return any(
        isinstance(item, dict)
        and item.get("run_id") == run_id
        and item.get("tool_name") == tool_name
        and item.get("status") != "ok"
        for item in state.get("tool_observations", [])
    )


async def safe_stop(
    host: SafeStopHost,
    state: AgentState,
    reason: str,
    *,
    error_code: str | None = None,
) -> AgentState:
    safe_error_code = error_code or (reason if reason.startswith("provider_") else None)
    issue_type = str((state.get("classification") or {}).get("issue_type", "unknown"))
    knowledge_read_failed = failed_current_tool_observation(state, "search_knowledge")
    candidate = CandidateResponse(
        answer=safe_failure_answer(
            reason,
            missing_groups=list((state.get("evidence_assessment") or {}).get("missing_groups", [])),
            failure_domain=(
                "knowledge" if issue_type == "product_knowledge" and knowledge_read_failed else None
            ),
        ),
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=[],
        proposed_arguments={},
    )
    terminal_state = safe_stop_terminal_state(reason)
    await host._event(
        state,
        "agent_stopped",
        {"agent_finish_reason": reason},
        visibility="customer",
        status="completed" if terminal_state == "resolved" else "failed",
    )
    return {
        "agent_decision": AgentDecision(
            decision_type="final_candidate",
            decision_summary=f"Runtime stopped safely: {reason}.",
            candidate=candidate,
        ).model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "agent_finish_reason": reason,
        "safe_stop_reason": reason,
        "safe_stop_error_code": safe_error_code or "",
        "policy_route": PolicyRoute.ANSWER.value,
        "citation_integrity": False,
        "validated_answer": candidate.answer,
    }


@dataclass(frozen=True, slots=True)
class FinalizationNodes:
    """Own final response projection; this module also owns fail-closed stops."""

    host: FinalizationHost

    async def finalize(self, state: AgentState) -> AgentState:
        candidate = CandidateResponse.model_validate(state["candidate"])
        raw_route = str(state.get("policy_route", "manual_takeover"))
        unsupported_legacy_route = raw_route in {"safe_action", "manual_takeover"}
        route = PolicyRoute.ANSWER if unsupported_legacy_route else PolicyRoute(raw_route)
        human_action = state.get("human_decision", {}).get("action")
        execution_status = state.get("execution_result", {}).get("status")
        terminal: str
        if state.get("safe_stop_reason"):
            terminal = safe_stop_terminal_state(str(state["safe_stop_reason"]))
        elif execution_status == "succeeded":
            terminal = "resolved"
        elif execution_status == "stale":
            terminal = "failed"
        elif execution_status == "execution_pending" or (
            state.get("execution_result", {}).get("execution_state") == "verification_pending"
        ):
            terminal = "verification_pending"
        elif human_action == "reject" or route == PolicyRoute.REJECT:
            terminal = "rejected"
        elif human_action == "manual_takeover":
            terminal = "manual_takeover"
        elif unsupported_legacy_route:
            terminal = "failed"
        elif state.get("agent_finish_reason") == "needs_clarification":
            terminal = "needs_clarification"
        else:
            terminal = "resolved"
        publishes_claims = self.can_publish_claims(state, terminal)
        if human_action == "manual_takeover":
            final_answer = (
                "这是一条历史人工接管决定，自动处理已停止，但当前版本没有"
                "人工坐席收件、回复或完成闭环，因此不会声称有人正在处理。"
            )
        elif human_action == "reject":
            final_answer = (
                "审批者已拒绝这项申请，因此没有执行任何业务操作。你仍可以在当前对话继续咨询。"
            )
        elif unsupported_legacy_route:
            final_answer = safe_failure_answer("human_handoff_unavailable")
        elif execution_status == "stale":
            final_answer = safe_failure_answer("binding_stale")
        elif execution_status == "execution_pending":
            final_answer = (
                "审批已经通过，系统正在提交这项高风险操作。"
                "最终执行结果确认前不会把它显示为已完成，也不会重复执行。"
            )
        elif state.get("execution_result", {}).get("execution_state") == "verification_pending":
            final_answer = (
                "审批已经通过，但当前还不能确认最终执行结果。"
                "系统将保持该申请并阻止重复操作，待核验完成后更新最终状态。"
            )
        else:
            final_answer = state.get("validated_answer") or self.host._render_validated_answer(
                candidate,
                route=route,
                finish_reason=state.get("agent_finish_reason"),
                integrity=state.get("citation_integrity", False),
                issue_type=str(state.get("classification", {}).get("issue_type", "unknown")),
                requested_action=str(
                    state.get("classification", {}).get("requested_action", "none")
                ),
                explicit_first_step=self.host._requests_explicit_first_step(
                    str(state.get("redacted_message", ""))
                ),
            )
        final = FinalResponse(
            answer=final_answer,
            terminal_state=terminal,
            knowledge_chunk_ids=(candidate.knowledge_chunk_ids if publishes_claims else []),
            business_source_ids=(candidate.business_source_ids if publishes_claims else []),
            material_claims=(candidate.material_claims if publishes_claims else []),
            policy_route=route.value,
        )
        await self.host._persist_final(state, final)
        return {"final": final.model_dump(mode="json")}

    @staticmethod
    def can_publish_claims(state: AgentState, terminal: str) -> bool:
        return (
            terminal == "resolved"
            or state.get("execution_result", {}).get("status") == "execution_pending"
        ) and state.get("agent_finish_reason") not in {
            "credential_redaction_guidance",
            "proposal_eligibility_failed",
        }

    @staticmethod
    async def persist_memory(state: AgentState) -> AgentState:
        del state
        return {}
