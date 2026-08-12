from __future__ import annotations

from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.schemas import CandidateResponse
from supportguard.policies.gate import PolicyRoute


def _diagnostic_candidate() -> CandidateResponse:
    return CandidateResponse.model_validate(
        {
            "answer": "candidate",
            "action": "answer",
            "knowledge_chunk_ids": ["knowledge"],
            "knowledge_citations": [{"citation_binding_id": "citation"}],
            "business_source_ids": ["usage"],
            "material_claims": [
                {
                    "text": "余额充足与并发限制是两个独立事实。",
                    "citation_binding_ids": ["citation"],
                    "knowledge_locator_hashes": ["a" * 64],
                    "observation_source_ids": ["usage"],
                },
                {
                    "text": "收到限流响应后应确认错误子码，查看当前并发值，并实施退避重试。",
                    "citation_binding_ids": ["citation"],
                    "knowledge_locator_hashes": ["a" * 64],
                    "observation_source_ids": ["usage"],
                },
                {
                    "text": "应等待服务端建议的重试窗口后再重试。",
                    "citation_binding_ids": ["citation"],
                    "knowledge_locator_hashes": ["a" * 64],
                    "observation_source_ids": ["usage"],
                },
            ],
            "proposed_arguments": {},
        }
    )


def test_v158_explicit_first_step_prioritizes_one_verified_action() -> None:
    rendered = AgentRuntimeServices._render_validated_answer(
        _diagnostic_candidate(),
        route=PolicyRoute.ANSWER,
        finish_reason="answered",
        integrity=True,
        issue_type="api_diagnostics",
        explicit_first_step=True,
    )

    first_line = rendered.splitlines()[0]
    assert first_line.startswith("第一步：")
    assert "等待服务端建议的重试窗口" in first_line
    assert AgentRuntimeServices._requests_explicit_first_step("那我现在最先应该做哪一步？")
    assert AgentRuntimeServices._requests_explicit_first_step("What should I do first?")


def test_v158_non_priority_question_preserves_verified_claim_order() -> None:
    rendered = AgentRuntimeServices._render_validated_answer(
        _diagnostic_candidate(),
        route=PolicyRoute.ANSWER,
        finish_reason="answered",
        integrity=True,
        issue_type="api_diagnostics",
        explicit_first_step=False,
    )

    assert rendered.splitlines()[0] == "余额充足与并发限制是两个独立事实。"
    assert "第一步：" not in rendered
    assert not AgentRuntimeServices._requests_explicit_first_step("为什么会触发限流？")
