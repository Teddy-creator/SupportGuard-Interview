from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.agent.graph import SupportGraph
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import ProposalCausalDecisionV2
from supportguard.contracts.context import (
    PolicyCapabilityMcpCallContext,
    ReadMcpCallContext,
    RequestContext,
    WorkerExecutionContext,
    worker_execution_context,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.services.attempts import ReservedAttempt
from supportguard.services.capability_ledger import ReservedCapability
from supportguard.services.runtime_jobs import JobLease
from supportguard.tools.gateway import ToolGateway


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=1)


def _worker(**overrides: object) -> WorkerExecutionContext:
    values: dict[str, object] = {
        "tenant_id": "tenant_a",
        "actor_principal_id": "actor_a",
        "executor_service_principal": "worker_a",
        "customer_id": "customer_a",
        "ticket_id": "ticket_a",
        "run_id": "run_a",
        "job_id": "job_a",
        "segment_id": "segment_a",
        "delivery_generation": 1,
        "fencing_token": 1,
        "trace_id": "trace_a",
        "deadline": _deadline(),
    }
    values.update(overrides)
    return WorkerExecutionContext(**values)  # type: ignore[arg-type]


def test_worker_context_rejects_missing_scope_and_invalid_fence() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        _worker(tenant_id="")
    with pytest.raises(ValueError, match="fencing_token"):
        _worker(fencing_token=0)


def test_mcp_context_is_bounded_by_worker_deadline() -> None:
    worker = _worker()
    with pytest.raises(ValueError, match="cannot exceed"):
        ReadMcpCallContext(
            logical_invocation_id="invocation_a",
            tool_attempt_id="attempt_a",
            transport_attempt_id="transport_a",
            tool_name="query_account",
            transport_attempt=1,
            agent_tool_round=1,
            call_deadline=worker.deadline + timedelta(seconds=1),
            worker_deadline=worker.deadline,
        )


def test_policy_capability_context_cannot_consume_agent_round() -> None:
    worker = _worker()
    causal_decision = {
        "variant": "proposal",
        "capability_name": "propose_refund",
        "action_type": "refund",
        "resource_id": "bill_a",
        "resource_version": 1,
        "model_arguments": {"billing_record_id": "bill_a"},
        "observation_binding_hash": "b" * 64,
        "policy_version": "supportguard-policy-gate.v1",
    }
    context = PolicyCapabilityMcpCallContext(
        capability_invocation_id="capability_a",
        capability_attempt_id="attempt_a",
        capability_name="propose_refund",
        effect_identity="c" * 64,
        capability_attempt=1,
        capability_sequence=1,
        causal_decision_hash=canonical_json_hash(causal_decision),
        causal_decision=causal_decision,
        observation_binding_hash="b" * 64,
        call_deadline=worker.deadline,
        worker_deadline=worker.deadline,
    )
    assert context.agent_tool_round is None


def test_mcp_context_uses_heartbeat_refreshed_lease_deadline() -> None:
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )
    refreshed_deadline = datetime.now(UTC) + timedelta(minutes=2)
    lease = JobLease(
        job_id="job_a",
        run_id="run_a",
        tenant_id="tenant_a",
        owner="worker_a",
        fencing_token=1,
        expires_at=refreshed_deadline,
    )
    state = {
        "tenant_id": "tenant_a",
        "customer_id": "customer_a",
        "ticket_id": "ticket_a",
        "run_id": "run_a",
        "job_id": "job_a",
        "segment_id": "segment_a",
        "delivery_generation": 1,
        "fencing_token": 1,
        "trace_id": "trace_a",
        "user_message": "这两个版本最主要的区别是什么？",
    }
    read = graph.runtime._read_tool_context(
        state,  # type: ignore[arg-type]
        "tool_call_a",
        tool_name="query_account",
        reservation=(
            lease,
            ReservedAttempt(
                id="attempt_a",
                kind="read_mcp",
                ordinal=1,
                logical_invocation_id="invocation_a",
                transport_ordinal=1,
                transport_attempt_id="transport_a",
            ),
        ),
        logical_invocation_id="invocation_a",
        transport_attempt=1,
        tool_round=1,
    )
    search = graph.runtime._read_tool_context(
        state,  # type: ignore[arg-type]
        "tool_call_search",
        tool_name="search_knowledge",
        reservation=(
            lease,
            ReservedAttempt(
                id="attempt_search",
                kind="read_mcp",
                ordinal=1,
                logical_invocation_id="invocation_search",
                transport_ordinal=1,
                transport_attempt_id="transport_search",
            ),
        ),
        logical_invocation_id="invocation_search",
        transport_attempt=1,
        tool_round=1,
    )
    decision = ProposalCausalDecisionV2(
        capability_name="propose_refund",
        action_type="refund",
        resource_id="bill_a",
        resource_version=1,
        model_arguments={"billing_record_id": "bill_a"},
        observation_binding_hash="b" * 64,
        policy_version="supportguard-policy-gate.v1",
    )
    action = graph.runtime._tool_context(
        state,  # type: ignore[arg-type]
        approval=True,
        observation_binding=[],
        lease=lease,
        capability=ReservedCapability(
            id="capability_a",
            capability_name="propose_refund",
            sequence=1,
            causal_decision_hash=canonical_json_hash(decision.model_dump(mode="python")),
            causal_decision=decision,
            observation_binding_hash="b" * 64,
            effect_identity="c" * 64,
            attempt_id="capability_attempt_a",
            attempt_ordinal=1,
        ),
    )
    assert read.mcp_context is not None
    assert action.mcp_context is not None
    assert read.mcp_context.worker_deadline == refreshed_deadline
    assert action.mcp_context.worker_deadline == refreshed_deadline
    assert read.mcp_context.call_deadline > datetime.now(UTC)
    assert action.mcp_context.call_deadline > datetime.now(UTC)
    assert isinstance(search.mcp_context, ReadMcpCallContext)
    assert search.mcp_context.retrieval_intent is not None
    assert search.mcp_context.retrieval_intent.intent == "compare"


def test_worker_context_binding_is_lexical_and_task_local() -> None:
    outer = _worker(job_id="job_outer")
    inner = _worker(job_id="job_inner")
    with pytest.raises(RuntimeError, match="not bound"):
        worker_execution_context.get()
    with worker_execution_context.bind(outer):
        assert worker_execution_context.get() is outer
        with worker_execution_context.bind(inner):
            assert worker_execution_context.get() is inner
        assert worker_execution_context.get() is outer
    with pytest.raises(RuntimeError, match="not bound"):
        worker_execution_context.get()


@pytest.mark.asyncio
async def test_scoped_factory_rebinds_after_commit_and_rollback() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = create_scoped_session_factory(engine)
    context = RequestContext(
        tenant_id="tenant_a",
        authenticated_actor_id="actor_a",
        authenticated_actor_role="support_agent",
        request_id="request_a",
        trace_id="trace_a",
        deadline=_deadline(),
    )
    try:
        with pytest.raises(RuntimeError, match=r"\.request\(\) or \.worker\(\)"):
            factory()
        async with factory.request(context) as session:
            await session.execute(text("SELECT 1"))
            assert session.sync_session.info["supportguard_scope_bound"] == (
                "tenant_a",
                "actor_a",
                "support_agent",
            )
            await session.commit()
            session.sync_session.info.pop("supportguard_scope_bound")
            await session.execute(text("SELECT 1"))
            assert "supportguard_scope_bound" in session.sync_session.info
            await session.rollback()
            session.sync_session.info.pop("supportguard_scope_bound")
            await session.execute(text("SELECT 1"))
            assert "supportguard_scope_bound" in session.sync_session.info
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_does_not_leak_to_new_task_after_binding_exits() -> None:
    context = _worker()

    async def read_bound() -> str:
        await asyncio.sleep(0)
        return worker_execution_context.get().job_id

    with worker_execution_context.bind(context):
        assert await read_bound() == "job_a"
    with pytest.raises(RuntimeError, match="not bound"):
        await read_bound()
