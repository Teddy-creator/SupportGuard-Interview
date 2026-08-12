from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.schemas import AgentDecision, CandidateResponse
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BusinessAction,
    ConversationTurn,
    EscalationRecord,
    SupportTicket,
    TicketMessage,
)
from supportguard.policies.gate import PolicyRoute, decide_policy
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from supportguard.tools.gateway import ToolGateway


class _ProviderMustNotRun(DeterministicFakeProvider):
    async def generate(self, **kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("provider must not run after an inbound fail-closed result")

    async def decide(self, **kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("provider must not run after an inbound fail-closed result")


class _ActionGatewayMustNotRun:
    async def call_action(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("new human handoff must not call the Action MCP")


def _candidate(action: str) -> CandidateResponse:
    return CandidateResponse.model_validate(
        {
            "answer": "已转交人工处理。",
            "action": action,
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "proposed_arguments": (
                {"reason": "The model requested unsupported human handoff."}
                if action == "escalate"
                else {}
            ),
        }
    )


@pytest.mark.parametrize("action", ["escalate", "manual_takeover"])
def test_v1512_policy_never_grants_a_new_human_handoff_route(action: str) -> None:
    candidate = _candidate(action)

    assert decide_policy(candidate, evidence_conflict=False) == PolicyRoute.ANSWER
    assert (
        decide_policy(
            candidate,
            evidence_conflict=True,
            citation_integrity=False,
        )
        == PolicyRoute.ANSWER
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "evidence_conflict", "expected_reason"),
    [
        (_candidate("escalate"), False, "human_handoff_unavailable"),
        (_candidate("manual_takeover"), False, "human_handoff_unavailable"),
        (
            CandidateResponse(
                answer="两个相互冲突的版本都可以作为结论。",
                action="answer",
                knowledge_chunk_ids=[],
                business_source_ids=[],
            ),
            True,
            "evidence_conflict",
        ),
        (
            CandidateResponse(
                answer="使用一个未绑定来源的结论。",
                action="answer",
                knowledge_chunk_ids=["unbound-chunk"],
                business_source_ids=[],
            ),
            False,
            "citation_binding_incomplete",
        ),
    ],
)
async def test_v1512_graph_policy_replaces_unsupported_terminal_with_safe_answer(
    candidate: CandidateResponse,
    evidence_conflict: bool,
    expected_reason: str,
) -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, _ActionGatewayMustNotRun()),
    )
    state = AgentState(
        tenant_id="tenant_safe",
        customer_id="customer_safe",
        ticket_id="ticket_safe",
        run_id="run_safe",
        trace_id="trace_safe",
        redacted_message="请继续处理",
        candidate=candidate.model_dump(mode="json"),
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
        action_admission={},
        action_obligation_ledger={},
        evidence=[],
        evidence_conflict=evidence_conflict,
        citation_binding_map={},
        tool_observations=[],
        llm_calls=6,
        tool_rounds=2,
        tool_attempts=6,
        agent_finish_reason="answered",
    )

    update = await graph.action_flow_nodes.policy(state)

    safe_candidate = CandidateResponse.model_validate(update["candidate"])
    assert update["policy_route"] == PolicyRoute.ANSWER
    assert update["agent_finish_reason"] == expected_reason
    assert safe_candidate.action == "answer"
    assert safe_candidate.knowledge_chunk_ids == []
    assert safe_candidate.knowledge_citations == []
    assert safe_candidate.business_source_ids == []
    assert safe_candidate.material_claims == []
    safe_decision = AgentDecision.model_validate(update["agent_decision"])
    assert safe_decision.decision_type == "final_candidate"
    assert safe_decision.candidate == safe_candidate
    assert "no-handoff" in safe_decision.decision_summary
    assert "已转交人工" not in safe_decision.decision_summary
    assert (
        "人工队列" in update["validated_answer"]
        or "可追溯来源" in update["validated_answer"]
        or "已发布证据" in update["validated_answer"]
    )
    assert "已转交人工" not in update["validated_answer"]
    assert graph.action_flow_nodes.route_policy(cast(AgentState, {**state, **update})) == "finalize"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_route",
    [PolicyRoute.SAFE_ACTION, PolicyRoute.MANUAL_TAKEOVER],
)
async def test_v1512_finalizer_rejects_a_current_unsupported_handoff_route(
    legacy_route: PolicyRoute,
) -> None:
    """A corrupt/old checkpoint cannot make a new human-lifecycle claim."""

    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, _ActionGatewayMustNotRun()),
    )
    update = await graph.finalization_nodes.finalize(
        AgentState(
            tenant_id="tenant_safe",
            customer_id="customer_safe",
            ticket_id="ticket_safe",
            run_id="run_safe",
            trace_id="trace_safe",
            redacted_message="请继续处理",
            candidate=_candidate("manual_takeover").model_dump(mode="json"),
            policy_route=legacy_route.value,
            citation_integrity=False,
            agent_finish_reason="manual_takeover",
        )
    )

    assert update["final"]["terminal_state"] == "failed"
    assert update["final"]["knowledge_chunk_ids"] == []
    assert update["final"]["business_source_ids"] == []
    assert "人工队列" in update["final"]["answer"]
    assert "已转交人工" not in update["final"]["answer"]
    assert "不会把这次请求标记为“有人正在处理”" in update["final"]["answer"]


def _accepted_state(**overrides: Any) -> AgentState:
    payload: dict[str, Any] = {
        "tenant_id": "tenant_safe",
        "customer_id": "customer_safe",
        "ticket_id": "ticket_safe",
        "run_id": "run_safe",
        "job_id": "job_safe",
        "segment_id": "segment_safe",
        "delivery_generation": 1,
        "fencing_token": 1,
        "trace_id": "trace_safe",
        "user_message": "请告诉我当前状态",
    }
    payload.update(overrides)
    return AgentState(**payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_reason", "expected_next_step"),
    [
        (
            _accepted_state(
                current_actions=[
                    {
                        "schema_version": "conversation-action-state.v1",
                        "approval_id": "approval_corrupt",
                        "projection_status": "executed",
                        "grants_action_authority": True,
                    }
                ]
            ),
            "action_state_unavailable",
            "资源引用",
        ),
        (
            _accepted_state(
                classification_context=[
                    {
                        "role": "customer",
                        "message_id": "message_long_customer",
                        "content": "完整客户上下文" * 500,
                    },
                    {
                        "role": "assistant",
                        "message_id": "message_long_assistant",
                        "content": "完整助手上下文" * 500,
                    },
                ]
            ),
            "context_budget_exhausted",
            "把问题拆成一个目标",
        ),
    ],
)
async def test_v1512_accepted_turn_inbound_failure_gets_one_safe_terminal_reply(
    state: AgentState,
    expected_reason: str,
    expected_next_step: str,
) -> None:
    graph = SupportGraph(
        provider=_ProviderMustNotRun(),
        retrieval=None,
        gateway=cast(ToolGateway, _ActionGatewayMustNotRun()),
    )

    output = await graph.run(state)

    assert output["safe_stop_reason"] == expected_reason
    assert output["final"]["terminal_state"] == "failed"
    assert expected_next_step in output["final"]["answer"]
    assert "已转交人工" not in output["final"]["answer"]
    assert "有人正在处理" not in output["final"]["answer"]
    assert "action_result" not in output
    assert [
        event["event_type"]
        for event in graph.segment_events
        if event["event_type"] == "agent_stopped"
    ] == ["agent_stopped"]
    assert all(
        event["event_type"] not in {"escalation_created", "human_queue_update"}
        for event in graph.segment_events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [
        ("action_state", "action_state_unavailable"),
        ("protected_context", "context_budget_exhausted"),
    ],
)
async def test_v1512_inbound_failure_finalizes_the_accepted_turn_once(
    db_session: AsyncSession,
    failure_kind: str,
    expected_reason: str,
) -> None:
    """Exercise the same checkpoint/finalizer boundary used by Worker jobs."""

    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    turn = await db_session.get(ConversationTurn, "turn_demo")
    message = await db_session.get(TicketMessage, "message_demo")
    assert run is not None and ticket is not None and turn is not None
    assert message is not None
    run.status = "queued"
    run.checkpoint_stage = "queued"
    run.checkpoint_id = None
    turn.activity_state = "queued"
    turn.result_state = None
    ticket.status = "queued"
    ticket.automation_mode = "agent"
    await db_session.flush()

    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        kind="agent_start",
    )
    lease = await jobs.claim(
        job_id=job.id,
        owner=f"worker-safe-{failure_kind}",
        now=datetime.now(UTC),
    )
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"message_id": message.id, "kind": "agent_start"},
    )
    failure_state: dict[str, Any]
    if failure_kind == "action_state":
        failure_state = {
            "current_actions": [
                {
                    "schema_version": "conversation-action-state.v1",
                    "approval_id": "approval_corrupt",
                    "projection_status": "executed",
                    "grants_action_authority": True,
                }
            ]
        }
    else:
        failure_state = {
            "classification_context": [
                {
                    "role": "customer",
                    "message_id": "message_long_customer",
                    "content": "完整客户上下文" * 500,
                },
                {
                    "role": "assistant",
                    "message_id": "message_long_assistant",
                    "content": "完整助手上下文" * 500,
                },
            ]
        }
    graph = SupportGraph(
        provider=_ProviderMustNotRun(),
        retrieval=None,
        gateway=cast(ToolGateway, _ActionGatewayMustNotRun()),
    )
    output = await graph.run(
        _accepted_state(
            run_id=run.id,
            ticket_id=ticket.id,
            customer_id=ticket.customer_id,
            job_id=job.id,
            segment_id=marker.id,
            trace_id=f"trace-safe-{failure_kind}",
            user_message=message.content,
            **failure_state,
        )
    )
    output["segment_events"] = graph.segment_events

    await segments.checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id=f"checkpoint-safe-{failure_kind}",
        checkpoint_hash=("a" if failure_kind == "action_state" else "b") * 64,
        outcome="completed",
        state=dict(output),
    )
    await segments.finalize(lease, marker_id=marker.id)
    # A stale direct finalizer replay is rejected before publication and may
    # not manufacture a second customer response.
    with pytest.raises(RuntimeConflict, match="marker_not_finalizable"):
        await segments.finalize(lease, marker_id=marker.id)

    await db_session.refresh(run)
    await db_session.refresh(turn)
    await db_session.refresh(ticket)
    replies = list(
        (
            await db_session.scalars(
                select(TicketMessage).where(
                    TicketMessage.tenant_id == ticket.tenant_id,
                    TicketMessage.ticket_id == ticket.id,
                    TicketMessage.publication_key == f"assistant:{run.id}",
                    TicketMessage.message_kind == "assistant",
                )
            )
        ).all()
    )
    assert run.status == "completed"
    assert run.agent_finish_reason == expected_reason
    assert turn.activity_state == "completed"
    assert turn.result_state == "failed"
    assert ticket.status == "failed"
    assert ticket.automation_mode == "agent"
    assert len(replies) == 1
    assert "执行任何变更" in replies[0].content
    assert "已转交人工" not in replies[0].content
    assert await db_session.scalar(select(func.count()).select_from(EscalationRecord)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BusinessAction)) == 0
