from datetime import UTC, datetime
from typing import cast

import pytest

from supportguard.agent.context import build_trusted_task_state
from supportguard.agent.current_facts import (
    requested_current_fact_requirements,
    requested_current_fact_status,
)
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.schemas import CandidateResponse
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


class FakeGateway:
    """Policy-only fixture; these tests never execute transport calls."""


def _explicit_current_facts_state(
    *,
    claimed_fields: tuple[str, ...] = (
        "account_status",
        "remaining_balance",
        "concurrency_limit",
    ),
    usage_freshness: str = "fresh",
    replan_count: int = 0,
) -> AgentState:
    observations = [
        {
            "run_id": "run_current_facts",
            "tool_name": "query_account",
            "status": "ok",
            "freshness_status": "fresh",
            "observed_at": datetime.now(UTC).isoformat(),
            "fresh_until": "2999-01-01T00:00:00+00:00",
            "source_refs": [{"source_id": "customer:current"}],
            "data": {
                "account_status": "active",
                "security_status": "normal",
                "region": "eu-west",
            },
        },
        {
            "run_id": "run_current_facts",
            "tool_name": "query_subscription",
            "status": "ok",
            "freshness_status": "fresh",
            "observed_at": datetime.now(UTC).isoformat(),
            "fresh_until": "2999-01-01T00:00:00+00:00",
            "source_refs": [{"source_id": "subscription:current"}],
            "data": {
                "subscription_id": "sub_current",
                "plan": "pro",
                "status": "active",
                "rpm_limit": 60,
                "concurrency_limit": 40,
                "version": 2,
            },
        },
        {
            "run_id": "run_current_facts",
            "tool_name": "query_api_usage",
            "status": "ok",
            "freshness_status": usage_freshness,
            "observed_at": datetime.now(UTC).isoformat(),
            "fresh_until": (
                "2999-01-01T00:00:00+00:00"
                if usage_freshness == "fresh"
                else "2026-01-01T00:00:00+00:00"
            ),
            "source_refs": [{"source_id": "usage:current"}],
            "data": {
                "remaining_balance": "120.00",
                "balance_currency": "USD",
                "concurrency_current": 8,
                "concurrency_peak": 8,
                "request_count": 20,
            },
        },
    ]
    claim_specs = {
        "account_status": ("当前账户状态为 active。", "customer:current"),
        "remaining_balance": ("当前余额为 120.00 USD。", "usage:current"),
        "concurrency_limit": ("当前套餐并发上限为 40。", "subscription:current"),
    }
    claims = [
        {
            "text": claim_specs[field][0],
            "observation_source_ids": [claim_specs[field][1]],
        }
        for field in claimed_fields
    ]
    business_sources = list(
        dict.fromkeys(
            source_id for _, source_id in (claim_specs[field] for field in claimed_fields)
        )
    )
    return AgentState(
        tenant_id="tenant_demo",
        ticket_id="ticket_current_facts",
        customer_id="customer_current_facts",
        run_id="run_current_facts",
        redacted_message="请告诉我当前账户状态、余额和并发上限。",
        classification={
            "issue_type": "api_diagnostics",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "needs_realtime_facts": True,
            "support_subject": "customer_problem",
        },
        candidate=CandidateResponse.model_validate(
            {
                "answer": "\n".join(item["text"] for item in claims),
                "action": "answer",
                "knowledge_chunk_ids": [],
                "knowledge_citations": [],
                "business_source_ids": business_sources,
                "material_claims": claims,
                "proposed_arguments": {},
            }
        ).model_dump(mode="json"),
        agent_finish_reason="answered",
        llm_calls=3,
        tool_rounds=2,
        tool_attempts=3,
        tool_observations=observations,
        evidence=[],
        evidence_conflict=False,
        citation_binding_map={},
        evidence_replan_count=replan_count,
    )


def test_explicit_current_fact_surface_exposes_only_missing_requested_reads() -> None:
    complete = _explicit_current_facts_state()
    empty = AgentState(**{**complete, "tool_observations": []})
    partial = AgentState(**{**complete, "tool_observations": complete["tool_observations"][:2]})
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    assert requested_current_fact_requirements(empty) == {
        "account_status": ("query_account", ("account_status",)),
        "remaining_balance": (
            "query_api_usage",
            ("remaining_balance", "balance_currency"),
        ),
        "concurrency_limit": ("query_subscription", ("concurrency_limit",)),
    }
    assert graph.runtime._allowlist(empty) == {
        "query_account",
        "query_subscription",
        "query_api_usage",
    }
    assert graph.runtime._allowlist(partial) == {"query_api_usage"}
    assert graph.runtime._allowlist(complete) == set()

    empty["tool_rounds"] = 0
    empty["tool_attempts"] = 0
    assert graph.runtime._required_evidence_decision(empty) is None

    partial["tool_rounds"] = 1
    partial["tool_attempts"] = 2
    closure = graph.runtime._required_evidence_decision(partial)
    assert closure is not None
    assert [item.call.name for item in closure.tool_calls] == ["query_api_usage"]
    assert closure.tool_calls[0].call.arguments.model_dump() == {"window": "1m"}


def test_explanation_request_does_not_become_a_current_fact_shortcut() -> None:
    state = AgentState(
        run_id="run_429_explanation",
        redacted_message=(
            "账户还有 120 美元，但 atlas-chat 返回 429 concurrency_limit_exceeded，为什么？"
        ),
        classification={
            "issue_type": "api_diagnostics",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "needs_realtime_facts": True,
        },
        tool_observations=[],
    )
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    assert requested_current_fact_requirements(state) == {}
    assert graph.runtime._allowlist(state) == {
        "search_knowledge",
        "query_subscription",
        "query_api_usage",
    }


def test_current_saturation_question_requires_usage_and_configured_limit() -> None:
    state = AgentState(
        run_id="run_current_saturation",
        redacted_message="我现在是不是已经打满并发了？",
        classification={
            "issue_type": "api_diagnostics",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "needs_realtime_facts": True,
        },
        tool_observations=[],
    )
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    assert requested_current_fact_requirements(state) == {
        "current_concurrency": ("query_api_usage", ("concurrency_current",)),
        "concurrency_limit": ("query_subscription", ("concurrency_limit",)),
    }
    assert graph.runtime._allowlist(state) == {"query_api_usage", "query_subscription"}


@pytest.mark.parametrize(
    ("fact_name", "claim_text", "expected_missing"),
    [
        (
            "account_status",
            "当前账户状态不是 active，而是 suspended。",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "当前账户状态不是 suspended，而是 active。",
            [],
        ),
        (
            "account_status",
            "当前账户状态为 inactive。",
            ["current_fact_claim:account_status"],
        ),
        (
            "concurrency_limit",
            "当前并发上限不是 40，应为 20。",
            ["current_fact_claim:concurrency_limit"],
        ),
        (
            "account_status",
            "当前账户状态不能确认是 active。",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "The account status cannot be confirmed as active.",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "当前账户状态未确认是 active。",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "当前账户状态是 active 吗？",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "active 不是当前账户状态，当前状态为 suspended。",
            ["current_fact_claim:account_status"],
        ),
        (
            "account_status",
            "当前账户状态 active 不正确，应该是 suspended。",
            ["current_fact_claim:account_status"],
        ),
        (
            "concurrency_limit",
            "当前并发上限无法确认，订阅版本为 40。",
            ["current_fact_claim:concurrency_limit"],
        ),
        (
            "concurrency_limit",
            "订阅版本为 40，当前并发上限为 20。",
            ["current_fact_claim:concurrency_limit"],
        ),
        (
            "concurrency_limit",
            "订阅版本为 40，当前并发上限为 40。",
            [],
        ),
        (
            "remaining_balance",
            "当前余额无法确认，当前请求数为 120.00 USD。",
            ["current_fact_claim:remaining_balance"],
        ),
    ],
)
def test_current_fact_claim_validation_uses_final_affirmative_assertion(
    fact_name: str,
    claim_text: str,
    expected_missing: list[str],
) -> None:
    state = _explicit_current_facts_state()
    candidate = CandidateResponse.model_validate(state["candidate"])
    source_by_fact = {
        "account_status": "customer:current",
        "concurrency_limit": "subscription:current",
        "remaining_balance": "usage:current",
    }
    source_id = source_by_fact[fact_name]
    for claim in candidate.material_claims:
        if source_id in claim.observation_source_ids:
            claim.text = claim_text
            break

    missing, stale = requested_current_fact_status(state, candidate)

    assert missing == expected_missing
    assert stale == []


@pytest.mark.asyncio
async def test_policy_rewrites_when_an_explicit_current_value_is_omitted() -> None:
    state = _explicit_current_facts_state(claimed_fields=("account_status", "concurrency_limit"))
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["policy_route"] == "replan"
    assert update["evidence_assessment"]["error_code"] == ("explicit_current_fact_incomplete")
    assert update["evidence_assessment"]["missing_groups"] == [
        "current_fact_claim:remaining_balance"
    ]
    trusted = build_trusted_task_state(
        AgentState(
            **{
                **state,
                "candidate": {},
                "evidence_assessment": update["evidence_assessment"],
            }
        )
    )
    correction = trusted["previous_provider_decision_rejected"]
    assert correction["reason_code"] == "explicit_current_fact_incomplete"
    assert trusted["requested_current_facts"]["reads_complete"] is True
    assert {item["field"] for item in trusted["requested_current_facts"]["facts"]} == {
        "account_status",
        "remaining_balance",
        "concurrency_limit",
    }


@pytest.mark.asyncio
async def test_policy_accepts_all_explicit_current_values_with_matching_sources() -> None:
    state = _explicit_current_facts_state()
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["policy_route"] == "answer"
    assert update["evidence_assessment"]["result"] == "accept"
    assert "active" in update["validated_answer"]
    assert "120.00 USD" in update["validated_answer"]
    assert "40" in update["validated_answer"]


@pytest.mark.asyncio
async def test_policy_fails_closed_after_explicit_current_fact_rewrite_still_omits_value() -> None:
    state = _explicit_current_facts_state(
        claimed_fields=("account_status", "concurrency_limit"),
        replan_count=1,
    )
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["policy_route"] == "answer"
    assert update["agent_finish_reason"] == "explicit_current_fact_incomplete"
    assert update["evidence_assessment"]["result"] == "terminal"
    assert update["candidate"]["material_claims"] == []
    assert "每一项当前值" in update["validated_answer"]


@pytest.mark.asyncio
async def test_stale_requested_current_fact_is_not_published_as_current() -> None:
    state = _explicit_current_facts_state(usage_freshness="stale")
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, FakeGateway()),
        test_capability=issue_test_runtime_capability(testing=True),
    )

    update = await graph.action_flow_nodes.policy(state)

    assert update["policy_route"] == "answer"
    assert update["agent_finish_reason"] == "evidence_freshness_insufficient"
    assert "120.00" not in update["validated_answer"]
    assert "实时数据已过期" in update["validated_answer"]
