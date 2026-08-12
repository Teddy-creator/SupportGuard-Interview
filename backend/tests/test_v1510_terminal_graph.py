from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.obligations import TerminalBusinessOutcome
from supportguard.agent.schemas import CandidateResponse
from supportguard.contracts.action_preconditions import (
    resolve_action_admission_v2,
)
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


@pytest.mark.asyncio
async def test_graph_projects_terminal_fact_as_resolved_answer_without_provider_call() -> None:
    observed_at = datetime.now(UTC)
    admission = resolve_action_admission_v2(
        "请给账单 bill_terminal 退款。",
        [],
        requested_action="refund",
        issue_type="billing_refund",
        tenant_id="tenant-v1510",
        customer_id="customer-v1510",
        current_message_id="message-v1510",
        turn_group_id="turn-v1510",
    )
    observation = {
        "tool_name": "query_billing_record",
        "tool_call_id": "call-query-billing-record",
        "invocation_id": "invocation-query-billing-record",
        "observation_id": "observation-query-billing-record",
        "run_id": "run-v1510",
        "attempt_index": 1,
        "status": "ok",
        "retryable": False,
        "observed_at": observed_at.isoformat(),
        "freshness_status": "fresh",
        "fresh_until": (observed_at + timedelta(minutes=5)).isoformat(),
        "trusted_scope": {
            "tenant_id": "tenant-v1510",
            "customer_id": "customer-v1510",
            "scope_hash": admission.scope_hash,
        },
        "data": {
            "billing_record_id": "bill_terminal",
            "amount": "49.00",
            "currency": "USD",
            "status": "refunded",
            "duplicate_of": None,
            "version": 3,
        },
        "source_refs": [
            {
                "source_type": "business_record",
                "source_id": "source-query-billing-record",
                "observed_at": observed_at.isoformat(),
            }
        ],
        "request_binding": {
            "arguments_hash": "a" * 64,
            "resource_ref": "bill_terminal",
        },
    }
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    state = AgentState(
        tenant_id="tenant-v1510",
        customer_id="customer-v1510",
        ticket_id="ticket-v1510",
        run_id="run-v1510",
        job_id="job-v1510",
        trace_id="trace-v1510",
        redacted_message="请给账单 bill_terminal 退款。",
        redaction_rule_ids=[],
        classification={
            "issue_type": "billing_refund",
            "risk": "high",
            "policy_boundary": "allowed",
            "requested_action": "refund",
            "requested_concurrency_limit": None,
            "support_subject": "customer_problem",
        },
        action_admission=admission.model_dump(mode="json"),
        tool_observations=[observation],
        context_citation_bindings=[],
        evidence=[],
        citation_binding_map={},
        llm_calls=2,
        tool_rounds=1,
        tool_attempts=2,
        evidence_replan_count=0,
        step_index=1,
    )

    evaluated = await graph.action_flow_nodes.evaluate_obligations(state)
    projected = await graph.action_flow_nodes.explain_terminal_business_outcome(
        AgentState(**{**state, **evaluated})
    )
    policy = await graph.action_flow_nodes.policy(
        AgentState(**{**state, **evaluated, **projected})
    )
    final = await graph.finalization_nodes.finalize(
        AgentState(**{**state, **evaluated, **projected, **policy})
    )

    outcome = TerminalBusinessOutcome.model_validate(
        projected["terminal_business_outcome"]
    )
    assert outcome.outcome_code == "refund_status_not_actionable"
    assert projected["agent_finish_reason"] == "terminal_business_outcome"
    assert policy["policy_route"] == "answer"
    assert policy["citation_integrity"] is True
    assert final["final"]["terminal_state"] == "resolved"
    assert final["final"]["business_source_ids"] == [
        "source-query-billing-record"
    ]
    assert final["final"]["material_claims"] == []
    assert "已经退款" in final["final"]["answer"]
    assert "没有创建审批" in final["final"]["answer"]
    assert [
        event["event_type"]
        for event in graph.segment_events
        if event["event_type"].startswith("terminal_business_outcome_")
    ] == [
        "terminal_business_outcome_derived",
        "terminal_business_outcome_projected",
    ]


def test_policy_rejects_a_forged_terminal_projection() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    candidate = {
        "answer": "账单已经退款。",
        "action": "answer",
        "knowledge_chunk_ids": [],
        "business_source_ids": ["forged-source"],
        "material_claims": [],
        "proposed_arguments": {},
    }

    assert graph.action_flow_nodes._terminal_business_contract_valid(  # noqa: SLF001
        AgentState(
            tenant_id="tenant-v1510",
            customer_id="customer-v1510",
            run_id="run-v1510",
            terminal_business_outcome={
                "schema_version": "terminal-business-outcome.v1",
                "action_type": "refund",
                "terminal_class": "action_ineligible",
                "outcome_code": "refund_status_not_actionable",
                "obligation_id": "billing_record_current",
                "resource_ref": "bill_terminal",
                "observed_facts": {
                    "billing_record_id": "bill_terminal",
                    "status": "refunded",
                },
                "binding": {
                    "tool_name": "query_billing_record",
                    "invocation_id": "forged",
                    "observation_id": "forged",
                    "observation_content_hash": "a" * 64,
                    "tenant_id": "tenant-v1510",
                    "customer_id": "customer-v1510",
                    "scope_hash": "b" * 64,
                    "freshness_status": "fresh",
                    "source_ids": ["forged-source"],
                },
                "customer_message_key": "refund_status_not_actionable",
                "recommended_next_step": "review_existing_refund",
                "proposal_allowed": False,
                "approval_allowed": False,
                "execution_allowed": False,
                "outcome_hash": "c" * 64,
            },
            candidate=candidate,
            action_obligation_ledger={},
            action_admission={},
            tool_observations=[],
        ),
        candidate=CandidateResponse.model_validate(candidate),
    ) is False
