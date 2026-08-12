import pytest

from supportguard.agent.contracts import PROMPT_VERSION
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.schemas import CandidateResponse
from supportguard.policies.gate import PolicyRoute
from supportguard.prompts.registry import load_prompt
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


def test_field_specific_clarification_survives_validated_rendering() -> None:
    candidate = CandidateResponse(
        answer="请提供需要核验的账单 ID。",
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=[],
    )

    rendered = AgentRuntimeServices._render_validated_answer(
        candidate,
        route=PolicyRoute.ANSWER,
        finish_reason="needs_clarification",
        integrity=True,
        issue_type="billing_refund",
    )

    assert "账单 ID" in rendered
    assert "Request ID、发生区域或相关资源引用" not in rendered
    assert "不会执行" in rendered


def test_refund_clarification_is_derived_from_requested_action_not_provider_wording() -> None:
    candidate = CandidateResponse(
        answer="Please provide the relevant resource reference.",
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=[],
    )

    rendered = AgentRuntimeServices._render_validated_answer(
        candidate,
        route=PolicyRoute.ANSWER,
        finish_reason="needs_clarification",
        integrity=True,
        issue_type="billing_refund",
        requested_action="refund",
    )

    assert "账单 ID" in rendered
    assert "Billing ID" in rendered
    assert "账单编号" in rendered
    assert "不会创建审批" in rendered
    assert "不会执行" in rendered
    assert "resource reference" not in rendered


def test_unresolved_action_preserves_grounded_ineligibility_fact() -> None:
    candidate = CandidateResponse(
        answer="free provider text is not trusted",
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=["billing:bill_example"],
        material_claims=[
            {
                "text": "账单 bill_example 已经退款，因此无需再次发起退款。",
                "observation_source_ids": ["billing:bill_example"],
            }
        ],
    )

    rendered = AgentRuntimeServices._render_validated_answer(
        candidate,
        route=PolicyRoute.ANSWER,
        finish_reason="requested_action_unresolved",
        integrity=True,
        issue_type="billing_refund",
    )

    assert "已经退款" in rendered
    assert "无需再次发起退款" in rendered
    assert "没有创建审批" in rendered
    assert "没有执行" in rendered


def test_current_prompt_requires_conflict_evidence_before_clarification() -> None:
    assert PROMPT_VERSION == "agent_decide.v5+bound_evidence_synthesis.v1"
    prompt = load_prompt("agent_decide", version="v5").content

    assert "policy, document, or version conflict" in prompt
    assert "call `search_knowledge` before returning `needs_clarification`" in prompt
    assert "missing applicability condition" in prompt
    assert "use a grounded `final_candidate`" in prompt
    assert "every conflicting" in prompt


@pytest.mark.asyncio
async def test_explicit_version_conflict_replans_before_clarification() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    candidate = CandidateResponse(
        answer="请补充你的部署区域。",
        action="answer",
        knowledge_chunk_ids=[],
        business_source_ids=[],
    )

    update = await graph.action_flow_nodes.policy(
        AgentState(
            run_id="run_conflicting_versions",
            candidate=candidate.model_dump(mode="json"),
            classification={
                "issue_type": "product_knowledge",
                "policy_boundary": "allowed",
                "requested_action": "none",
                "support_subject": "customer_problem",
            },
            redacted_message=(
                "两个版本对这个功能的区域限制说法不同，但我没告诉你部署区域。"
                "现在能直接判断是否支持吗？"
            ),
            agent_finish_reason="needs_clarification",
            llm_calls=1,
            tool_rounds=0,
            tool_attempts=0,
            tool_observations=[],
            evidence=[],
            citation_binding_map={},
            evidence_replan_count=0,
        )
    )

    assert update["policy_route"] == "replan"
    assert update["evidence_assessment"]["error_code"] == "conflict_evidence_required"
    assert update["evidence_assessment"]["missing_groups"] == ["knowledge"]


@pytest.mark.asyncio
async def test_grounded_conflict_converges_without_claims_or_human_handoff() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    candidate = CandidateResponse.model_validate(
        {
            "answer": "两个版本对区域限制的说明冲突；请补充部署区域。",
            "action": "answer",
            "knowledge_chunk_ids": ["chunk_old", "chunk_current"],
            "knowledge_citations": [
                {"citation_binding_id": "citation_old"},
                {"citation_binding_id": "citation_current"},
            ],
            "business_source_ids": [],
            "material_claims": [
                {
                    "text": (
                        "历史版本与当前版本对区域限制的说明不同；"
                        "现有证据不能支持直接判断，请补充部署区域。"
                    ),
                    "citation_binding_ids": ["citation_old", "citation_current"],
                    "knowledge_locator_hashes": ["a" * 64, "b" * 64],
                    "observation_source_ids": [],
                }
            ],
        }
    )
    evidence = [
        {
            "chunk_id": "chunk_old",
            "document_id": "regional-support",
            "version": "2.2",
            "content_hash": "old",
            "supporting_span_eligible": True,
            "source_locator": {"locator_hash": "a" * 64},
        },
        {
            "chunk_id": "chunk_current",
            "document_id": "regional-support",
            "version": "3.1",
            "content_hash": "current",
            "supporting_span_eligible": True,
            "source_locator": {"locator_hash": "b" * 64},
        },
    ]

    update = await graph.action_flow_nodes.policy(
        AgentState(
            run_id="run_grounded_conflict",
            candidate=candidate.model_dump(mode="json"),
            classification={
                "issue_type": "product_knowledge",
                "policy_boundary": "allowed",
                "requested_action": "none",
                "support_subject": "customer_problem",
            },
            redacted_message="两个版本对区域限制的说法不同，我的部署区域尚未提供。",
            agent_finish_reason="answered",
            llm_calls=3,
            tool_rounds=1,
            tool_attempts=1,
            tool_observations=[
                {
                    "tool_name": "search_knowledge",
                    "status": "ok",
                    "run_id": "run_grounded_conflict",
                    "data": {"evidence": [{"chunk_id": "chunk_old"}]},
                }
            ],
            evidence=evidence,
            evidence_conflict=True,
            citation_binding_map={
                "citation_old": {
                    "chunk_id": "chunk_old",
                    "document_id": "regional-support",
                    "version": "2.2",
                    "content_hash": "old",
                    "locator_hash": "a" * 64,
                },
                "citation_current": {
                    "chunk_id": "chunk_current",
                    "document_id": "regional-support",
                    "version": "3.1",
                    "content_hash": "current",
                    "locator_hash": "b" * 64,
                },
            },
            evidence_replan_count=1,
        )
    )

    assert update["policy_route"] == "answer"
    assert update["agent_finish_reason"] == "evidence_conflict"
    assert "已发布证据存在冲突" in update["validated_answer"]
    assert "产品版本和发生时间" in update["validated_answer"]
    assert "人工队列" in update["validated_answer"]
    assert "已转交人工" not in update["validated_answer"]
    safe_candidate = CandidateResponse.model_validate(update["candidate"])
    assert safe_candidate.action == "answer"
    assert safe_candidate.knowledge_chunk_ids == []
    assert safe_candidate.knowledge_citations == []
    assert safe_candidate.material_claims == []
