from __future__ import annotations

from typing import cast

import pytest

from supportguard.agent.conversation_semantics import is_knowledge_only_api_question
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.schemas import CandidateResponse
from supportguard.api.conversation_presentation import (
    apply_conversation_detail_presentation,
)
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.conversation_text import is_standalone_greeting
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


@pytest.mark.parametrize("message", ["你好", "您好！", "Hi", " hello there "])
def test_standalone_greeting_is_bounded(message: str) -> None:
    assert is_standalone_greeting(message) is True


@pytest.mark.parametrize(
    "message",
    ["你好，我的账号为什么返回 429？", "hi, please refund bill_123", "怎么退款"],
)
def test_support_request_is_not_reduced_to_a_greeting(message: str) -> None:
    assert is_standalone_greeting(message) is False


@pytest.mark.asyncio
async def test_greeting_uses_trusted_non_material_answer_without_tools_or_citations() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )

    output = await graph.run(
        AgentState(
            tenant_id="tenant_demo",
            ticket_id="ticket_greeting",
            customer_id="cust_demo",
            run_id="run_greeting",
            trace_id="trace_greeting",
            user_message="你好",
        )
    )

    assert output["classification"]["support_subject"] == "supportguard_greeting"
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output["citation_integrity"] is True
    assert output["final"]["terminal_state"] == "resolved"
    assert "我是 SupportGuard" in output["final"]["answer"]
    assert "具体问题" in output["final"]["answer"]
    assert "证据" not in output["final"]["answer"]


def test_generic_api_definition_exposes_only_knowledge_read() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    state = AgentState(
        classification={
            "issue_type": "api_diagnostics",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
        }
    )

    assert graph.runtime._allowlist(state) == {"search_knowledge"}
    assert is_knowledge_only_api_question(state["classification"]) is True


def test_greeting_title_is_replaced_by_first_substantive_customer_question() -> None:
    detail = apply_conversation_detail_presentation(
        {
            "title": "你好",
            "activity_label": "已回答",
            "turns": [
                {
                    "id": "turn_1",
                    "ordinal": 1,
                    "messages": [{"role": "customer", "content": "你好"}],
                },
                {
                    "id": "turn_2",
                    "ordinal": 2,
                    "messages": [{"role": "customer", "content": "429 之类的代码是什么意思？"}],
                },
            ],
            "pending_actions": [],
        }
    )
    assert detail["title"] == "429 之类的代码是什么意思？"


def test_latest_executed_action_has_a_completed_product_status() -> None:
    detail = apply_conversation_detail_presentation(
        {
            "title": "请处理重复扣费",
            "activity_label": "已回答",
            "turns": [{"id": "turn_refund", "ordinal": 1, "messages": []}],
            "pending_actions": [
                {"turn_id": "turn_refund", "status": "executed"},
            ],
        }
    )

    assert detail["activity_label"] == "操作已完成"


@pytest.mark.asyncio
async def test_irrelevant_stale_fact_does_not_downgrade_grounded_definition() -> None:
    evidence = {
        "chunk_id": "api:guide",
        "document_id": "api-guide",
        "version": "2.2",
        "content_hash": "a" * 64,
        "evidence_group": "current",
        "supporting_span_eligible": True,
        "supporting_span": "429 表示请求速率或并发达到限制。",
        "source_locator": {"locator_hash": "a" * 64},
    }
    candidate = CandidateResponse.model_validate(
        {
            "answer": "429 表示请求达到限制；当前并发为 40。",
            "action": "answer",
            "knowledge_chunk_ids": ["api:guide"],
            "knowledge_citations": [{"citation_binding_id": "citation-guide"}],
            "business_source_ids": ["usage:stale"],
            "material_claims": [
                {
                    "text": "429 表示请求速率或并发达到限制。",
                    "citation_binding_ids": ["citation-guide"],
                    "knowledge_locator_hashes": ["a" * 64],
                },
                {
                    "text": "当前并发为 40。",
                    "observation_source_ids": ["usage:stale"],
                },
            ],
        }
    )
    state = AgentState(
        run_id="run_generic_429",
        redacted_message="429 之类的数字代码是什么意思？",
        classification={
            "issue_type": "api_diagnostics",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
        },
        candidate=candidate.model_dump(mode="json"),
        agent_finish_reason="answered",
        llm_calls=4,
        tool_rounds=2,
        tool_attempts=2,
        tool_observations=[
            {
                "run_id": "run_generic_429",
                "tool_name": "search_knowledge",
                "status": "ok",
                "freshness_status": "fresh",
                "fresh_until": "2999-01-01T00:00:00Z",
                "data": {"evidence": [evidence]},
            },
            {
                "run_id": "run_generic_429",
                "tool_name": "query_api_usage",
                "status": "ok",
                "freshness_status": "stale",
                "fresh_until": "2026-01-01T00:00:00Z",
                "source_refs": [{"source_id": "usage:stale"}],
            },
        ],
        evidence=[evidence],
        evidence_conflict=False,
        citation_binding_map={
            "citation-guide": {
                "chunk_id": "api:guide",
                "document_id": "api-guide",
                "version": "2.2",
                "content_hash": "a" * 64,
                "locator_hash": "a" * 64,
            }
        },
        evidence_replan_count=1,
    )
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, ToolGateway(None)),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["agent_finish_reason"] == "answered"
    assert update["evidence_assessment"]["result"] == "accept"
    assert update["candidate"]["business_source_ids"] == []
    assert [claim["text"] for claim in update["candidate"]["material_claims"]] == [
        "429 表示请求速率或并发达到限制。"
    ]
    assert "当前并发为 40" not in update["validated_answer"]
    assert "实时数据已过期" not in update["validated_answer"]
