from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest

from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.action_flow import ActionFlowHost, ActionFlowNodes
from supportguard.agent.policy import (
    PolicyInput,
    PolicyRoute,
    PublicationDecision,
    evaluate_policy,
)
from supportguard.agent.schemas import CandidateResponse
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.policies import gate as legacy_gate
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


def _candidate(action: str = "answer") -> CandidateResponse:
    proposed_arguments = (
        {
            "billing_record_id": "bill_contract",
            "refund_reason": "verified duplicate charge",
        }
        if action == "refund_proposal"
        else {"reason": "unsupported handoff request"}
        if action == "escalate"
        else {}
    )
    return CandidateResponse(
        answer="可发布候选回答。",
        action=action,
        knowledge_chunk_ids=[],
        knowledge_citations=[],
        business_source_ids=[],
        material_claims=[],
        proposed_arguments=proposed_arguments,
    )


def _policy_input(**overrides: Any) -> PolicyInput:
    payload: dict[str, Any] = {
        "candidate": _candidate(),
        "evidence_conflict": False,
        "citation_integrity": True,
        "proposal_eligible": None,
        "finish_reason": "answered",
        "safe_stop_reason": None,
        "requested_action_unresolved": False,
        "evidence_assessment_result": "accept",
        "evidence_assessment_error_code": None,
        "has_secret_redaction": False,
        "policy_boundary": "allowed",
        "knowledge_comparison_requested": False,
        "knowledge_comparison_complete": False,
        "explainable_comparison": False,
        "comparison_citations_complete": False,
        "missing_transition_markers": (),
        "grounded_conflict_clarification": False,
        "requested_current_fact_missing": False,
        "mixed_account_applicability_missing": False,
    }
    payload.update(overrides)
    return PolicyInput(**payload)


def test_current_policy_has_no_live_legacy_handoff_route() -> None:
    assert {item.value for item in PolicyRoute} == {
        "answer",
        "await_human_approval",
        "reject",
    }
    assert legacy_gate.PolicyRoute.MANUAL_TAKEOVER.value == "manual_takeover"
    assert legacy_gate.PolicyRoute.SAFE_ACTION.value == "safe_action"
    assert legacy_gate.PolicyDecision is PublicationDecision


@pytest.mark.parametrize("legacy_route", ["safe_action", "manual_takeover"])
def test_legacy_checkpoint_route_can_only_converge_to_finalize(legacy_route: str) -> None:
    nodes = ActionFlowNodes(host=cast(ActionFlowHost, object()))

    assert nodes.route_policy({"policy_route": legacy_route}) == "finalize"


@pytest.mark.parametrize("action", ["escalate", "manual_takeover"])
def test_unsupported_handoff_is_bound_to_a_safe_publication(action: str) -> None:
    decision = evaluate_policy(_policy_input(candidate=_candidate(action)))

    assert decision.route == PolicyRoute.ANSWER
    assert decision.finish_reason == "human_handoff_unavailable"
    assert decision.unsafe_terminal_reason == "human_handoff_unavailable"
    assert decision.candidate.action == "answer"
    assert decision.candidate.knowledge_chunk_ids == []
    assert decision.candidate.business_source_ids == []
    assert decision.candidate.material_claims == []
    assert decision.grants_mutation is False
    assert (
        legacy_gate.decide_policy(_candidate(action), evidence_conflict=False)
        == legacy_gate.PolicyRoute.ANSWER
    )


@pytest.mark.parametrize(
    ("overrides", "expected_route", "expected_reason"),
    [
        ({}, PolicyRoute.ANSWER, "answered"),
        (
            {
                "candidate": _candidate("refund_proposal"),
                "proposal_eligible": True,
            },
            PolicyRoute.AWAIT_APPROVAL,
            "proposal_policy_approved",
        ),
        (
            {
                "candidate": _candidate("refund_proposal"),
                "proposal_eligible": False,
            },
            PolicyRoute.ANSWER,
            "proposal_eligibility_failed",
        ),
        (
            {"policy_boundary": "out_of_scope"},
            PolicyRoute.REJECT,
            "rejected",
        ),
        (
            {"has_secret_redaction": True},
            PolicyRoute.ANSWER,
            "credential_redaction_guidance",
        ),
        (
            {"requested_current_fact_missing": True},
            PolicyRoute.ANSWER,
            "explicit_current_fact_incomplete",
        ),
    ],
)
def test_publication_policy_matrix(
    overrides: dict[str, Any],
    expected_route: PolicyRoute,
    expected_reason: str,
) -> None:
    decision = evaluate_policy(_policy_input(**overrides))

    assert decision.route == expected_route
    assert decision.finish_reason == expected_reason
    assert decision.grants_mutation is False


def test_publication_decision_is_frozen_and_mutation_grant_is_not_constructible() -> None:
    decision = evaluate_policy(_policy_input())

    assert len(decision.candidate_sha256) == 64
    projected_candidate = decision.candidate
    projected_candidate.answer = "attempted mutation"
    assert decision.candidate.answer == "可发布候选回答。"
    with pytest.raises(FrozenInstanceError):
        decision.route = PolicyRoute.REJECT  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(decision, grants_mutation=True)


def test_policy_owner_is_pure_and_current_runtime_bypasses_legacy_facade() -> None:
    source_root = Path(__file__).parents[1] / "src" / "supportguard"
    policy_path = source_root / "agent" / "policy.py"
    tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("provider", "sqlalchemy", ".mcp", "session", "mutation")
    assert not any(
        marker in module.casefold() for module in imported_modules for marker in forbidden
    )

    current_importers = []
    for path in source_root.rglob("*.py"):
        if path == source_root / "policies" / "gate.py":
            continue
        if "supportguard.policies.gate" in path.read_text(encoding="utf-8"):
            current_importers.append(path.relative_to(source_root).as_posix())
    assert current_importers == []


def test_policy_core_functions_respect_phase4_complexity_budget() -> None:
    path = Path(__file__).parents[1] / "src" / "supportguard" / "agent" / "nodes" / "action_flow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sizes = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None
    }

    assert sizes["policy"] < 150
    assert sizes["_call_bound_evidence_synthesis"] < 200
    assert all(size < 200 for name, size in sizes.items() if "policy" in name)


@pytest.mark.asyncio
async def test_terminal_stale_pruning_rebinds_publication_hash_to_published_candidate() -> None:
    evidence = {
        "chunk_id": "api:guide",
        "document_id": "api-guide",
        "version": "2.2",
        "content_hash": "a" * 64,
        "evidence_group": "current",
        "supporting_span_eligible": True,
        "supporting_span": "并发限制与余额不足是不同机制。",
        "source_locator": {"locator_hash": "a" * 64},
    }
    state = AgentState(
        run_id="run_policy_hash",
        job_id="job_policy_hash",
        redacted_message="我现在是不是已经达到并发上限？",
        classification={
            "issue_type": "api_diagnostics",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "support_subject": "customer_problem",
        },
        candidate=CandidateResponse.model_validate(
            {
                "answer": "当前并发为 40；并发限制与余额不足无关。",
                "action": "answer",
                "knowledge_chunk_ids": ["api:guide"],
                "knowledge_citations": [{"citation_binding_id": "citation-guide"}],
                "business_source_ids": ["usage:stale"],
                "material_claims": [
                    {
                        "text": "当前并发为 40。",
                        "observation_source_ids": ["usage:stale"],
                    },
                    {
                        "text": "并发限制与余额不足无关。",
                        "citation_binding_ids": ["citation-guide"],
                        "knowledge_locator_hashes": ["a" * 64],
                    },
                ],
            }
        ).model_dump(mode="json"),
        agent_finish_reason="answered",
        llm_calls=4,
        tool_rounds=2,
        tool_attempts=4,
        tool_observations=[
            {
                "run_id": "run_policy_hash",
                "tool_name": "search_knowledge",
                "status": "ok",
                "freshness_status": "fresh",
                "fresh_until": "2999-01-01T00:00:00Z",
                "data": {"evidence": [evidence]},
            },
            {
                "run_id": "run_policy_hash",
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
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["agent_finish_reason"] == "evidence_freshness_insufficient"
    assert update["candidate"]["business_source_ids"] == []
    policy_event = next(
        event for event in graph.segment_events if event["event_type"] == "policy_decision"
    )
    canonical_candidate = json.dumps(
        update["candidate"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert (
        policy_event["payload"]["candidate_sha256"]
        == hashlib.sha256(canonical_candidate.encode()).hexdigest()
    )
