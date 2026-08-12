from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest

from supportguard.agent.current_facts import resolve_referential_billing_reference
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


def _graph() -> SupportGraph:
    return SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )


def _state(
    *,
    message: str = "根据退款政策，这条重复扣费的标准处理流程是什么？只说明流程。",
    history: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
) -> AgentState:
    return AgentState(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        ticket_id="ticket_referential_billing",
        run_id="run_referential_billing",
        redacted_message=message,
        classification={
            "issue_type": "billing_refund",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": True,
            "support_subject": "customer_problem",
            "rationale": "Read-only billing policy follow-up.",
        },
        relevant_history=history or [],
        tool_observations=observations or [],
        tool_rounds=0,
        tool_attempts=0,
    )


def _customer(content: str) -> dict[str, object]:
    return {"history_kind": "message", "role": "customer", "content": content}


def _assistant(content: str) -> dict[str, object]:
    return {"history_kind": "message", "role": "assistant", "content": content}


def _knowledge_observation(*, run_id: str = "run_referential_billing") -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "run_id": run_id,
        "tool_name": "search_knowledge",
        "status": "ok",
        "freshness_status": "fresh",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(minutes=5)).isoformat(),
        "source_refs": [{"source_id": "knowledge:refund-policy"}],
        "data": {"evidence": [{"chunk_id": "billing-refunds-v3:c001"}]},
    }


def _billing_observation(
    billing_record_id: str,
    *,
    run_id: str = "run_referential_billing",
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "run_id": run_id,
        "tool_name": "query_billing_record",
        "status": "ok",
        "freshness_status": "fresh",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(minutes=5)).isoformat(),
        "source_refs": [{"source_id": f"billing:{billing_record_id}"}],
        "data": {
            "billing_record_id": billing_record_id,
            "amount": "49.00",
            "currency": "USD",
            "status": "charged",
            "duplicate_of": "bill_original",
            "version": 2,
        },
    }


@pytest.mark.parametrize(
    ("message", "history", "expected_id", "expected_reason"),
    [
        (
            "根据退款政策，这条重复扣费的标准处理流程是什么？只说明流程。",
            [_customer("请查询 bill_demo_duplicate 的金额和状态。")],
            "bill_demo_duplicate",
            "history_unique_reference",
        ),
        (
            "What is the refund policy process for this duplicate charge? Do not create a refund.",
            [_customer("Check billing record bill_english_duplicate only.")],
            "bill_english_duplicate",
            "history_unique_reference",
        ),
        (
            "请按退款政策说明 bill_current_new 应如何处理，不要退款。",
            [_customer("之前查询的是 bill_history_old。")],
            "bill_current_new",
            "current_message_reference",
        ),
        (
            "Please explain the refund policy for bill_ascii_period.",
            [],
            "bill_ascii_period",
            "current_message_reference",
        ),
        (
            "Please explain the billing process for bill_ascii_colon:",
            [],
            "bill_ascii_colon",
            "current_message_reference",
        ),
    ],
)
def test_customer_owned_reference_resolution(
    message: str,
    history: list[dict[str, object]],
    expected_id: str,
    expected_reason: str,
) -> None:
    resolution = resolve_referential_billing_reference(_state(message=message, history=history))

    assert resolution.status == "resolved"
    assert resolution.billing_record_id == expected_id
    assert resolution.reason_code == expected_reason


@pytest.mark.parametrize(
    ("history", "reason"),
    [
        ([], "history_reference_missing"),
        ([_assistant("账单 bill_assistant_only 是重复扣费。")], "history_reference_missing"),
        (
            [
                _customer("请查询 bill_first_record。"),
                _customer("也请查询 bill_second_record。"),
            ],
            "history_ambiguous",
        ),
    ],
)
def test_reference_resolution_fails_closed_without_one_customer_owned_id(
    history: list[dict[str, object]],
    reason: str,
) -> None:
    resolution = resolve_referential_billing_reference(_state(history=history))

    assert resolution.status == "unresolved"
    assert resolution.billing_record_id is None
    assert resolution.reason_code == reason
    assert _graph().runtime._allowlist(_state(history=history)) == {"search_knowledge"}


def test_non_customer_authorities_cannot_supply_billing_identity() -> None:
    state = _state(
        history=[
            {
                "history_kind": "summary",
                "role": "customer",
                "content": "Earlier billing reference: bill_summary_only.",
            },
            _assistant("The relevant record is bill_assistant_only."),
        ],
        observations=[_billing_observation("bill_observation_only", run_id="run_previous")],
    )
    state["memory_summary"] = "Customer discussed bill_memory_only."
    state["provider_turns"] = [{"assistant_text": "Use bill_provider_only for the next query."}]
    state["evidence"] = [{"text": "The retrieved document mentions bill_rag_only."}]

    resolution = resolve_referential_billing_reference(state)

    assert resolution.status == "unresolved"
    assert resolution.reason_code == "history_reference_missing"
    assert resolution.billing_record_id is None
    assert _graph().runtime._allowlist(state) == {"search_knowledge"}


def test_long_bounded_history_resolves_one_unique_customer_reference() -> None:
    history = [_customer(f"第 {index} 轮只讨论一般计费问题，没有账单编号。") for index in range(80)]
    history.insert(23, _customer("请查询 bill_long_history_unique 的当前状态。"))
    history.extend(
        [
            _assistant("Assistant text must not become resource identity."),
            {
                "history_kind": "summary",
                "role": "customer",
                "content": "Summary contains bill_summary_noise.",
            },
        ]
    )

    resolution = resolve_referential_billing_reference(_state(history=history))

    assert resolution.status == "resolved"
    assert resolution.reason_code == "history_unique_reference"
    assert resolution.billing_record_id == "bill_long_history_unique"


def test_current_message_with_multiple_billing_ids_is_ambiguous() -> None:
    state = _state(
        message=("根据退款政策，bill_first_record 和 bill_second_record 应分别如何处理？"),
        history=[_customer("此前只查询 bill_history_record。")],
    )

    resolution = resolve_referential_billing_reference(state)

    assert resolution.status == "unresolved"
    assert resolution.reason_code == "current_message_ambiguous"
    assert _graph().runtime._allowlist(state) == {"search_knowledge"}


def test_action_or_non_policy_turn_does_not_activate_read_only_reference_contract() -> None:
    action_state = _state(history=[_customer("请查询 bill_demo_duplicate。")])
    action_state["classification"] = {
        **action_state["classification"],
        "requested_action": "refund",
    }
    non_policy = _state(
        message="这条重复扣费金额是多少？",
        history=[_customer("请查询 bill_demo_duplicate。")],
    )

    assert resolve_referential_billing_reference(action_state).status == "not_applicable"
    assert resolve_referential_billing_reference(non_policy).status == "not_applicable"


def test_resolved_follow_up_requires_both_current_run_reads() -> None:
    state = _state(history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")])
    graph = _graph()

    decision = graph.runtime._required_evidence_decision(state)

    assert decision is not None
    assert [item.call.name for item in decision.tool_calls] == [
        "search_knowledge",
        "query_billing_record",
    ]
    assert decision.tool_calls[0].call.arguments.model_dump() == {
        "query": state["redacted_message"]
    }
    assert decision.tool_calls[1].call.arguments.model_dump() == {
        "billing_record_id": "bill_demo_duplicate"
    }
    assert graph.runtime._allowlist(state) == {"search_knowledge", "query_billing_record"}


@pytest.mark.asyncio
async def test_agent_decide_persists_referential_billing_runtime_owner() -> None:
    state = _state(history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")])
    state.update(
        {
            "trace_id": "trace_referential_billing",
            "customer_message_id": "message_referential_billing",
            "conversation_turn_id": "turn_referential_billing",
            "classification_context": [],
            "evidence": [],
            "provider_turns": [],
            "llm_calls": 1,
            "step_index": 0,
        }
    )
    graph = _graph()
    admission_update = await graph.intake_nodes.resolve_action_admission(state)
    merged = AgentState(**{**state, **admission_update})
    event = AsyncMock()
    graph.runtime._event = event

    decision_update = await graph.decision_nodes.agent_decide(merged)

    assert admission_update["action_admission"]["status"] == "none"
    assert admission_update["action_admission"]["reason_code"] == "no_high_risk_action"
    assert decision_update["agent_decision"]["decision_type"] == "tool_calls"
    agent_event = next(call for call in event.await_args_list if call.args[1] == "agent_decision")
    assert agent_event.args[2]["deterministic_evidence_requirement"] == (
        "referential_billing_policy_follow_up"
    )
    assert agent_event.args[2]["tool_names"] == [
        "search_knowledge",
        "query_billing_record",
    ]
    assert decision_update.get("llm_calls", merged["llm_calls"]) == merged["llm_calls"]


def test_partial_read_is_closed_without_repeating_completed_knowledge() -> None:
    state = _state(
        history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")],
        observations=[_knowledge_observation()],
    )
    state["tool_rounds"] = 1
    state["tool_attempts"] = 1
    graph = _graph()

    decision = graph.runtime._required_evidence_decision(state)

    assert decision is not None
    assert [item.call.name for item in decision.tool_calls] == ["query_billing_record"]
    assert graph.runtime._allowlist(state) == {"query_billing_record"}


@pytest.mark.parametrize(
    "observations",
    [
        [_knowledge_observation(), _billing_observation("bill_different_record")],
        [
            _knowledge_observation(),
            _billing_observation("bill_demo_duplicate", run_id="run_previous"),
        ],
    ],
)
def test_wrong_or_previous_run_billing_observation_cannot_close_requirement(
    observations: list[dict[str, object]],
) -> None:
    state = _state(
        history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")],
        observations=observations,
    )
    state["tool_rounds"] = 1
    state["tool_attempts"] = 2
    graph = _graph()

    decision = graph.runtime._required_evidence_decision(state)

    assert decision is not None
    assert [item.call.name for item in decision.tool_calls] == ["query_billing_record"]


def test_matching_current_run_reads_close_surface_without_action_authority() -> None:
    state = _state(
        history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")],
        observations=[
            _knowledge_observation(),
            _billing_observation("bill_demo_duplicate"),
        ],
    )
    state["tool_rounds"] = 1
    state["tool_attempts"] = 2
    graph = _graph()

    assert graph.runtime._required_evidence_decision(state) is None
    assert graph.runtime._allowlist(state) == set()
    assert state["classification"]["requested_action"] == "none"


def test_active_approval_keeps_existing_knowledge_only_explanation_path() -> None:
    state = _state(history=[_customer("请查询 bill_demo_duplicate 的金额和状态。")])
    state["current_actions"] = [
        {
            "projection_status": "pending",
            "action_type": "refund",
            "resource_id": "bill_demo_duplicate",
        }
    ]
    graph = _graph()

    assert graph.runtime._required_evidence_decision(state) is None
    assert graph.runtime._allowlist(state) == {"search_knowledge"}
