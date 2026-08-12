from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.schemas import CandidateResponse
from supportguard.contracts.action_preconditions import (
    resolve_missing_action_preconditions,
    validate_entitlement_target,
)
from supportguard.contracts.tools import EntitlementChangeProposalInput
from supportguard.policies.gate import PolicyRoute
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


class NeverCalledProvider(DeterministicFakeProvider):
    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic admission must not call the Provider")

    async def decide(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic admission must not call the Provider")


def _state(message: str, **updates: Any) -> AgentState:
    state: AgentState = {
        "tenant_id": "tenant_test",
        "ticket_id": "ticket_test",
        "customer_id": "customer_test",
        "run_id": "run_test",
        "trace_id": "trace_test",
        "user_message": message,
        "redacted_message": message,
        "classification_context": [],
        "relevant_history": [],
        "llm_calls": 0,
        "tool_rounds": 0,
        "tool_attempts": 0,
        "tool_observations": [],
        "latest_observations": [],
        "evidence": [],
        "provider_turns": [],
        "step_index": 0,
    }
    state.update(updates)
    return state


@pytest.mark.parametrize(
    ("message", "action_type", "missing_field", "customer_field"),
    [
        (
            "我好像被重复扣费了，帮我退款。",
            "refund",
            "billing_record_id",
            "账单 ID",
        ),
        (
            "请立即撤销这个 API Key。",
            "api_key_revocation",
            "api_key_ref",
            "Key Reference",
        ),
        (
            "帮我把并发额度提高一些。",
            "entitlement_change",
            "target.concurrency_limit",
            "并发上限目标值",
        ),
    ],
)
def test_missing_action_fields_are_derived_from_generic_contracts(
    message: str,
    action_type: str,
    missing_field: str,
    customer_field: str,
) -> None:
    admission = resolve_missing_action_preconditions(message, [])

    assert admission is not None
    assert admission.action_type == action_type
    assert admission.missing_fields == (missing_field,)
    assert customer_field in admission.clarification_question


@pytest.mark.parametrize(
    "message",
    [
        "退款政策是什么？",
        "提高并发上限需要满足哪些条件？",
        "API Key 被撤销后还能恢复吗？",
    ],
)
def test_informational_questions_are_not_intercepted_as_action_requests(
    message: str,
) -> None:
    assert resolve_missing_action_preconditions(message, []) is None


def test_complete_action_fields_do_not_trigger_missing_field_admission() -> None:
    assert (
        resolve_missing_action_preconditions(
            "请检查账单 bill_example_123 并帮我退款。",
            [],
        )
        is None
    )
    assert (
        resolve_missing_action_preconditions(
            "请把订阅的并发上限从 20 明确调整到 40。",
            [],
        )
        is None
    )


@pytest.mark.asyncio
async def test_refund_missing_billing_id_clarifies_before_any_provider_or_tool_call() -> None:
    graph = SupportGraph(
        provider=NeverCalledProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    initial = _state("我好像被重复扣费了，帮我退款。")

    classified = await graph.intake_nodes.classify(initial)
    decided = await graph.decision_nodes.agent_decide(AgentState(**{**initial, **classified}))

    assert classified["llm_calls"] == 0
    assert classified["classification"]["requested_action"] == "refund"
    assert classified["action_admission"]["missing_fields"] == ["billing_record_id"]
    assert decided["agent_finish_reason"] == "needs_clarification"
    assert "账单 ID" in decided["candidate"]["answer"]
    assert decided["agent_decision"]["tool_calls"] == []
    assert int(decided.get("llm_calls", 0)) == 0


@pytest.mark.asyncio
async def test_entitlement_missing_target_clarifies_before_proposal_path() -> None:
    graph = SupportGraph(
        provider=NeverCalledProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    initial = _state("帮我把并发额度提高一些。")

    classified = await graph.intake_nodes.classify(initial)
    decided = await graph.decision_nodes.agent_decide(AgentState(**{**initial, **classified}))

    assert classified["classification"]["requested_action"] == "entitlement_change"
    assert classified["classification"]["requested_concurrency_limit"] is None
    assert classified["action_admission"]["missing_fields"] == ["target.concurrency_limit"]
    assert decided["agent_finish_reason"] == "needs_clarification"
    assert "具体并发上限目标值" in decided["candidate"]["answer"]
    assert decided["agent_decision"]["tool_calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_field"),
    [
        ("我好像被重复扣费了，帮我退款。", "账单 ID"),
        ("帮我把并发额度提高一些。", "具体并发上限目标值"),
    ],
)
async def test_missing_action_precondition_finishes_full_graph_without_side_effect_path(
    message: str,
    expected_field: str,
) -> None:
    graph = SupportGraph(
        provider=NeverCalledProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )

    output = await graph.run(
        AgentState(
            tenant_id="tenant_test",
            ticket_id="ticket_full_admission",
            customer_id="customer_test",
            run_id="run_full_admission",
            trace_id="trace_full_admission",
            user_message=message,
        )
    )

    assert output["agent_finish_reason"] == "needs_clarification"
    assert output["policy_route"] == PolicyRoute.ANSWER.value
    assert expected_field in output["final"]["answer"]
    assert "不会创建审批" in output["final"]["answer"]
    assert "不会执行任何变更" in output["final"]["answer"]
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output.get("action_result", {}) == {}


@pytest.mark.asyncio
async def test_explicit_version_conflict_requires_knowledge_tool_before_provider() -> None:
    graph = SupportGraph(
        provider=NeverCalledProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    message = (
        "两个版本对这个功能的区域限制说法不同，但我没告诉你部署区域。现在能直接判断是否支持吗？"
    )
    state = _state(
        message,
        classification={
            "issue_type": "product_knowledge",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
            "rationale": "fixture",
        },
    )

    decided = await graph.decision_nodes.agent_decide(state)

    assert int(decided.get("llm_calls", 0)) == 0
    assert decided["agent_decision"]["decision_type"] == "tool_calls"
    assert decided["agent_decision"]["tool_calls"][0]["call"] == {
        "name": "search_knowledge",
        "arguments": {"query": message},
    }


@pytest.mark.asyncio
async def test_post_retrieval_conflict_clarification_exposes_evidence_and_missing_condition() -> (
    None
):
    graph = SupportGraph(
        provider=NeverCalledProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    message = "两个版本的区域限制不同，我还没有提供部署区域。"
    state = _state(
        message,
        classification={
            "issue_type": "product_knowledge",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
            "rationale": "fixture",
        },
        candidate=CandidateResponse(
            answer="根据当前产品文档，可以按默认限制使用。",
            action="answer",
            knowledge_chunk_ids=[],
            business_source_ids=[],
        ).model_dump(mode="json"),
        agent_finish_reason="answered",
        tool_rounds=2,
        tool_attempts=2,
        tool_observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "run_id": "run_test",
                "source_refs": [],
                "data": {"conflict": True, "evidence": [{"chunk_id": "old"}]},
            }
        ],
        evidence=[
            {
                "chunk_id": "old",
                "document_id": "regional-policy",
                "version": "2.2",
                "content_hash": "1" * 64,
                "evidence_group": "historical",
                "supporting_span_eligible": True,
                "source_locator": {"locator_hash": "a" * 64},
            },
            {
                "chunk_id": "current",
                "document_id": "regional-policy",
                "version": "3.1",
                "content_hash": "2" * 64,
                "evidence_group": "current",
                "supporting_span_eligible": True,
                "source_locator": {"locator_hash": "b" * 64},
            },
        ],
        evidence_conflict=True,
        citation_binding_map={
            "citation_old": {
                "chunk_id": "old",
                "document_id": "regional-policy",
                "version": "2.2",
                "content_hash": "1" * 64,
                "locator_hash": "a" * 64,
            },
            "citation_current": {
                "chunk_id": "current",
                "document_id": "regional-policy",
                "version": "3.1",
                "content_hash": "2" * 64,
                "locator_hash": "b" * 64,
            },
        },
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["policy_route"] == PolicyRoute.ANSWER.value
    assert update["citation_integrity"] is True
    assert "当前与历史发布证据对相关限制存在差异" in update["validated_answer"]
    assert "请补充部署区域" in update["validated_answer"]
    assert "不能直接判断是否支持" in update["validated_answer"]
    assert "可以按默认限制使用" not in update["validated_answer"]
    assert update["candidate"]["knowledge_chunk_ids"] == ["old", "current"]
    assert len(update["candidate"]["knowledge_citations"]) == 2


def test_v158_conflict_clarification_requires_both_published_groups() -> None:
    candidate = CandidateResponse(
        answer="请补充部署区域。",
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=[],
    )
    state = _state(
        "两个版本的区域限制不同，我还没有提供部署区域。",
        classification={
            "issue_type": "product_knowledge",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
            "rationale": "fixture",
        },
        agent_finish_reason="needs_clarification",
        evidence_conflict=True,
        tool_observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "run_id": "run_test",
                "source_refs": [],
                "data": {"conflict": False, "refusal_reason": "compare_evidence_group_missing"},
            }
        ],
        evidence=[
            {
                "chunk_id": "current",
                "document_id": "regional-policy",
                "version": "3.1",
                "content_hash": "2" * 64,
                "evidence_group": "current",
                "supporting_span_eligible": True,
                "source_locator": {"locator_hash": "b" * 64},
            }
        ],
        citation_binding_map={
            "citation_current": {
                "chunk_id": "current",
                "document_id": "regional-policy",
                "version": "3.1",
                "content_hash": "2" * 64,
                "locator_hash": "b" * 64,
            }
        },
    )

    canonical = AgentRuntimeServices._canonicalize_grounded_conflict_clarification(state, candidate)

    assert canonical == candidate


@pytest.mark.parametrize(
    "target",
    [
        {"concurrency_limit": None},
        {"concurrency_limit": True},
        {"concurrency_limit": 40, "rpm_limit": 1000},
        {},
    ],
)
def test_entitlement_target_contract_rejects_null_ambiguous_or_untyped_values(
    target: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        CandidateResponse.model_validate(
            {
                "answer": "candidate",
                "action": "entitlement_change_proposal",
                "knowledge_chunk_ids": [],
                "business_source_ids": [],
                "proposed_arguments": {
                    "subscription_id": "sub_example",
                    "change_type": "quota_change",
                    "target": target,
                    "reason": "Customer requested a verified quota change.",
                },
            }
        )
    with pytest.raises(ValidationError):
        EntitlementChangeProposalInput.model_validate(
            {
                "subscription_id": "sub_example",
                "change_type": "quota_change",
                "target": target,
                "reason": "Customer requested a verified quota change.",
                "idempotency_key": "idem_example_123",
            }
        )


def test_entitlement_target_contract_canonicalizes_one_typed_value() -> None:
    assert validate_entitlement_target(
        "quota_change",
        {"concurrency_limit": 40},
    ) == {"concurrency_limit": 40}
