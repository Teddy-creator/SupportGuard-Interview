from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast, get_type_hints

import pytest

from supportguard.agent.decision import ProviderDecisionPreparation
from supportguard.agent.evidence_contracts import EvidenceDecision
from supportguard.agent.graph import SupportGraph
from supportguard.agent.state import AgentState
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


def _knowledge_context() -> tuple[dict[str, Any], dict[str, Any]]:
    now = datetime.now(UTC)
    tenant_id = "tenant_evidence_wiring"
    customer_id = "customer_evidence_wiring"
    scope_hash = hashlib.sha256(
        json.dumps(
            {"customer_id": customer_id, "tenant_id": tenant_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence = {
        "evidence_id": "policy:evidence-wiring",
        "document_id": "plans-limits",
        "chunk_id": "plans-limits:c001",
        "title": "Plan limits",
        "section_path": "Current limits",
        "version": "1",
        "effective_at": now.isoformat(),
        "index_version": "index-v1",
        "content_hash": "a" * 64,
        "supporting_span": "Pro 为 60 RPM、40 并发。",
        "supporting_span_eligible": True,
        "supporting_span_reason": "selected",
        "retrieval_score": 1.0,
        "evidence_group": "current",
        "source_locator": {"locator_hash": "b" * 64},
    }
    observation = {
        "schema_version": "observation.v1",
        "observation_id": "observation_evidence_wiring",
        "tool_call_id": "call_evidence_wiring",
        "tool_name": "search_knowledge",
        "ticket_id": "ticket_evidence_wiring",
        "run_id": "run_evidence_wiring",
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "scope_hash": scope_hash,
        "attempt_index": 1,
        "status": "ok",
        "retryable": False,
        "error_code": None,
        "safe_error_summary": None,
        "observed_at": now.isoformat(),
        "freshness_class": "versioned_knowledge",
        "freshness_status": "fresh",
        "fresh_until": (now + timedelta(minutes=5)).isoformat(),
        "duration_ms": 1,
        "source_refs": [
            {
                "source_type": "knowledge_chunk",
                "source_id": "knowledge:plans-limits:c001",
                "observed_at": now.isoformat(),
            }
        ],
        "resource_version": "1",
        "data": {
            "evidence": [evidence],
            "conflict": False,
            "refusal_reason": None,
        },
        "trusted_scope": {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "scope_hash": scope_hash,
        },
    }
    return evidence, observation


def _decision_state() -> AgentState:
    evidence, observation = _knowledge_context()
    return AgentState(
        tenant_id="tenant_evidence_wiring",
        customer_id="customer_evidence_wiring",
        ticket_id="ticket_evidence_wiring",
        run_id="run_evidence_wiring",
        trace_id="trace_evidence_wiring",
        redacted_message="当前套餐限制是什么？",
        classification={
            "issue_type": "product_knowledge",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "needs_realtime_facts": False,
            "support_subject": "customer_problem",
        },
        action_admission={},
        action_obligation_ledger={},
        tool_observations=[observation],
        evidence=[evidence],
        relevant_history=[],
        provider_turns=[],
        llm_calls=1,
        tool_rounds=1,
        tool_attempts=1,
        step_index=1,
        evidence_replan_count=0,
        evidence_conflict=False,
    )


@pytest.mark.asyncio
async def test_public_decision_stage_freezes_evidence_before_candidate() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    state = _decision_state()
    update = await graph.decision_nodes.agent_decide(state)

    assert update["agent_finish_reason"] == "answered"
    assert update["evidence_decision"]["result"] == "accept"
    assert update["evidence_decision"]["sufficient"] is True
    assert update["evidence_decision"]["requirements"]["required_groups"] == ["knowledge"]
    assert (
        update["evidence_decision"]["provider_attempt_id"] == update["latest_provider_attempt_id"]
    )
    assert update["candidate"]

    policy_update = await graph.action_flow_nodes.policy(cast(AgentState, {**state, **update}))
    assert policy_update["evidence_assessment"]["sufficient"] is True
    assert policy_update["evidence_assessment"]["satisfied_groups"] == ["knowledge"]


@pytest.mark.asyncio
async def test_policy_replans_from_the_same_insufficient_evidence_snapshot() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )
    state = _decision_state()
    state["tool_observations"][0]["freshness_status"] = "stale"

    update = await graph.decision_nodes.agent_decide(state)

    assert update["evidence_decision"]["result"] == "replan"
    assert update["evidence_decision"]["stale_groups"] == ["knowledge"]
    policy_update = await graph.action_flow_nodes.policy(cast(AgentState, {**state, **update}))
    assert policy_update["policy_route"] == "replan"
    assert policy_update["candidate"] == {}
    assert policy_update["evidence_assessment"]["error_code"] == ("evidence_freshness_insufficient")


@pytest.mark.asyncio
async def test_policy_rejects_a_tampered_cross_scope_evidence_snapshot() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )
    state = _decision_state()
    update = await graph.decision_nodes.agent_decide(state)
    update["evidence_decision"]["tenant_id"] = "tenant_foreign"

    policy_update = await graph.action_flow_nodes.policy(cast(AgentState, {**state, **update}))

    assert policy_update["evidence_assessment"]["sufficient"] is False
    assert policy_update["evidence_assessment"]["error_code"] == "evidence_decision_invalid"
    assert policy_update["evidence_replan_required"] is False


def test_provider_preparation_requires_one_typed_evidence_decision() -> None:
    assert get_type_hints(ProviderDecisionPreparation)["evidence_decision"] is EvidenceDecision
