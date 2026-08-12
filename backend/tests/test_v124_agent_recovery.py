from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from mcp.types import CallToolResult
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.schemas import AgentDecision
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import ProposalCausalDecisionV2
from supportguard.contracts.context import WorkerExecutionContext
from supportguard.contracts.queue import RuntimeJobMessage
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import ObservationEnvelope, ToolCallContext
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    CheckpointCommitMarker,
    PolicyCapabilityAttempt,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    RawProviderDecisionEnvelope,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    ToolInvocation,
    ToolObservation,
    ToolTransportAttempt,
    TurnGroup,
)
from supportguard.mcp.manager import MCPCallResult, MCPManager
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.runtime import AppRuntime
from supportguard.runtime.worker import AgentJobHandler
from supportguard.services.attempts import AttemptLedger
from supportguard.services.capability_ledger import PolicyCapabilityLedger
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger
from supportguard.tools.gateway import ReadToolCall, ToolGateway


def _tenant_demo_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        connect_args={"server_settings": {"app.tenant_id": "tenant_demo"}},
    )


class MalformedOutputManager:
    def __init__(self) -> None:
        self.reconnect_flags: list[bool] = []

    async def call(
        self,
        _server_name: str,
        _tool_name: str,
        _arguments: dict[str, Any],
        *,
        reconnect_once: bool,
    ) -> MCPCallResult:
        self.reconnect_flags.append(reconnect_once)
        return MCPCallResult(
            value=CallToolResult(content=[], structuredContent={"customer_id": "incomplete"}),
            attempts=1,
        )


class ObservableTransportFailureManager:
    async def call(
        self,
        _server_name: str,
        _tool_name: str,
        _arguments: dict[str, Any],
        *,
        reconnect_once: bool,
    ) -> MCPCallResult:
        del reconnect_once
        raise EOFError("private transport detail must not enter Observation data")

    def health(self) -> dict[str, dict[str, object]]:
        return {
            "read": {
                "state": "degraded",
                "session": "closed",
                "schema": "verified",
                "reconnects": 1,
                "pending_calls": 0,
                "generation": 3,
                "last_error": "private detail",
                "pid": 12345,
            }
        }


class DurableTurnResumeProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def resume_durable_tool_turn(
        self, *, checkpoint_ns: str, execution_context: WorkerExecutionContext
    ) -> None:
        self.calls.append((checkpoint_ns, execution_context.fencing_token))
        raise RuntimeError("durable_turn_resume_probe")


class MissingCheckpointProbe:
    async def resume_durable_tool_turn(
        self, *, checkpoint_ns: str, execution_context: WorkerExecutionContext
    ) -> None:
        del checkpoint_ns, execution_context
        raise RuntimeError("durable tool turn checkpoint is unavailable")


class RecoveryReadGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_read(
        self,
        call: ReadToolCall,
        context: ToolCallContext,
        *,
        allow_retry: bool = True,
    ) -> ObservationEnvelope:
        del allow_retry
        self.calls.append(call.name)
        return ObservationEnvelope(
            tool_name=call.name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            attempt_index=1,
            status="ok",
            retryable=False,
            observed_at=datetime.now(UTC),
            freshness_class="transactional",
            freshness_status="fresh",
            duration_ms=1,
            resource_version="1",
            data={"recovered_capability": call.name},
        )


class CheckpointResumeProbe:
    async def aget_tuple(self, _config: object) -> object:
        return object()


class CompiledResumeProbe:
    command: Any | None = None

    async def ainvoke(self, command: Any, _config: object) -> dict[str, str]:
        self.command = command
        return {"policy_route": "completed"}


class GraphResumeProbe:
    def __init__(self) -> None:
        self.compiled = CompiledResumeProbe()
        self.segment_events = [{"event": "tool_turn_resumed"}]


class ScopedResumeProbe:
    @asynccontextmanager
    async def worker(self, _context: WorkerExecutionContext) -> Any:
        yield object()


@pytest.mark.asyncio
async def test_runtime_resume_follows_checkpoint_pending_node_without_forced_goto() -> None:
    runtime = object.__new__(AppRuntime)
    runtime.checkpointer = CheckpointResumeProbe()
    runtime.scoped_factory = ScopedResumeProbe()
    graph = GraphResumeProbe()
    runtime._graph = lambda _session: cast(Any, graph)
    context = WorkerExecutionContext(
        tenant_id="tenant_resume",
        actor_principal_id="principal_resume",
        executor_service_principal="worker_resume",
        customer_id="customer_resume",
        ticket_id="ticket_resume",
        run_id="run_resume",
        job_id="job_resume",
        segment_id="segment_resume",
        delivery_generation=2,
        fencing_token=3,
        trace_id="trace_resume",
        deadline=datetime.now(UTC),
    )

    result = await runtime.resume_durable_tool_turn(
        checkpoint_ns="private/run_resume/1",
        execution_context=context,
    )

    assert graph.compiled.command is not None
    assert graph.compiled.command.goto == ()
    assert graph.compiled.command.update == {
        "job_id": "job_resume",
        "segment_id": "segment_resume",
        "delivery_generation": 2,
        "fencing_token": 3,
        "trace_id": "trace_resume",
    }
    assert result["segment_events"] == [{"event": "tool_turn_resumed"}]


@pytest.mark.asyncio
async def test_gateway_rejects_malformed_output_without_hidden_transport_retry() -> None:
    manager = MalformedOutputManager()
    result = await ToolGateway(cast(MCPManager, manager)).call_read(
        ReadToolCall(name="query_account", arguments={}),
        ToolCallContext.fixture(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_gateway_output",
            run_id="run_gateway_output",
            tool_call_id="call_gateway_output",
            trace_id="trace_gateway_output",
        ),
        allow_retry=True,
    )
    assert manager.reconnect_flags == [False]
    assert result.status == "invalid_input"
    assert result.error_code == "tool_output_schema_invalid"
    assert result.retryable is False
    record_predicate_operands(
        requirement_id="C4-P0-02b",
        predicate_id="c4_p0_02b",
        subject_kind="mcp_retry_owner_contract",
        operands={
            "reconnect_flags": manager.reconnect_flags,
            "result_status": result.status,
            "error_code": result.error_code,
            "retryable": result.retryable,
            "transport_call_count": len(manager.reconnect_flags),
        },
    )


@pytest.mark.asyncio
async def test_gateway_failure_observation_has_bounded_supervisor_diagnostics() -> None:
    result = await ToolGateway(cast(MCPManager, ObservableTransportFailureManager())).call_read(
        ReadToolCall(name="query_account", arguments={}),
        ToolCallContext.fixture(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_gateway_diagnostic",
            run_id="run_gateway_diagnostic",
            tool_call_id="call_gateway_diagnostic",
            trace_id="trace_gateway_diagnostic",
        ),
        allow_retry=False,
    )

    assert result.status == "unavailable"
    assert result.error_code == "tool_unavailable"
    assert result.data == {
        "transport_error_type": "EOFError",
        "supervisor": {
            "state": "degraded",
            "session": "closed",
            "schema": "verified",
            "reconnects": 1,
            "pending_calls": 0,
            "generation": 3,
        },
    }
    assert "private" not in str(result.data)
    assert "pid" not in str(result.data)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_turn_takeover_reuses_decision_and_terminalizes_every_ordinal() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    worker_database_url = os.getenv("TEST_WORKER_DATABASE_URL")
    if not database_url or not worker_database_url:
        pytest.skip("TEST_DATABASE_URL and TEST_WORKER_DATABASE_URL are required")
    engine = _tenant_demo_engine(database_url)
    worker_engine = create_async_engine(worker_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_takeover_{suffix}"
    message_id = f"message_takeover_{suffix}"
    run_id = f"run_takeover_{suffix}"

    async with factory() as session, session.begin():
        ticket = SupportTicket(
            id=ticket_id,
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="queued",
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="take over an interrupted tool turn",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="queued",
            model="deterministic-fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            prompt_version="agent.v1",
            schema_version="agent.v1",
            context_version="context.v1.2",
        )
        session.add(run)
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        marker = CheckpointCommitMarker(
            id=f"segment_takeover_{suffix}",
            tenant_id="tenant_demo",
            run_id=run_id,
            job_id=job.id,
            fencing_token=old_lease.fencing_token,
            delivery_generation=1,
            segment_kind="agent_start",
            status="prepared",
            private_namespace=f"private/{run_id}/1",
            parent_checkpoint_version=run.canonical_checkpoint_version,
            expected_run_version=run.status_version,
            expected_run_status=run.status,
            expected_ticket_sequence=0,
            prepared_payload_hash="1" * 64,
            segment_input_hash="2" * 64,
        )
        session.add(marker)
        await session.flush()
        turn, invocations = await ToolLedger(session).open_turn(
            old_lease,
            segment_id=marker.id,
            tool_round=1,
            decision={"decision_type": "tool_calls", "provider_attempt": "fixed"},
            context_manifest={"context_hash": "fixed"},
            calls=[
                InvocationSpec("provider-call-1", "query_account", {}, 0),
                InvocationSpec("provider-call-2", "query_subscription", {}, 1),
            ],
        )
        await ToolLedger(session).mark_executing(old_lease, invocations[0].id)
        reserved = await AttemptLedger(session).reserve(
            old_lease,
            kind="read_mcp",
            logical_invocation_id=invocations[0].id,
            transport_ordinal=1,
        )
        job_id = job.id
        turn_id = turn.id
        old_transport_id = reserved.transport_attempt_id

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None

    async with factory() as session, session.begin():
        new_lease = await RuntimeJobRepository(session).claim(job_id=job_id, owner="worker-new")
        provider_attempts_before = int(
            await session.scalar(
                select(func.count(AgentCallAttempt.id)).where(
                    AgentCallAttempt.run_id == run_id,
                    AgentCallAttempt.call_kind == "llm",
                )
            )
            or 0
        )

    probe = DurableTurnResumeProbe()
    with pytest.raises(RuntimeError, match="durable_turn_resume_probe"):
        await AgentJobHandler(factory, cast(Any, probe))._recover_durable_tool_turn(
            new_lease, turn_id
        )
    assert probe.calls == [(f"private/{run_id}/1", new_lease.fencing_token)]

    async with factory() as session, session.begin():
        turn = await session.get(TurnGroup, turn_id)
        assert turn is not None and turn.fencing_token == new_lease.fencing_token
        takeover_turn_fencing_token = turn.fencing_token
        pending = [
            item.id
            for item in (
                await session.scalars(
                    select(ToolInvocation).where(ToolInvocation.turn_group_id == turn_id)
                )
            ).all()
            if item.lifecycle != "terminal"
        ]
        assert len(pending) == 2
        old_transport = await session.get(ToolTransportAttempt, old_transport_id)
        assert old_transport is not None and old_transport.status == "unknown"
        old_attempt = await session.get(AgentCallAttempt, reserved.id)
        assert old_attempt is not None
        takeover = old_attempt.runtime_provenance["fence_takeover"]
        assert takeover["previous_fencing_token"] < takeover["replacement_fencing_token"]
        assert takeover["replacement_lease_owner"] == "worker-new"
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None

    async with factory() as session, session.begin():
        terminal_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-terminal",
        )
        provider_attempts_before_abort = int(
            await session.scalar(
                select(func.count(AgentCallAttempt.id)).where(
                    AgentCallAttempt.run_id == run_id,
                    AgentCallAttempt.call_kind == "llm",
                )
            )
            or 0
        )

    missing_checkpoint_handler = AgentJobHandler(
        worker_factory,
        cast(Any, MissingCheckpointProbe()),
    )
    outcome = await missing_checkpoint_handler._recover_durable_tool_turn(
        terminal_lease,
        turn_id,
    )
    assert outcome == "terminal_failed:durable_turn_checkpoint_unavailable"
    async with worker_factory() as session, session.begin():
        finished = await session.scalar(
            text("SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"),
            {
                "job_id": terminal_lease.job_id,
                "owner": terminal_lease.owner,
                "fencing_token": terminal_lease.fencing_token,
                "outcome": outcome,
            },
        )
    assert isinstance(finished, dict)
    assert finished["status"] == "dead"
    assert finished["outcome"] == "failed:durable_turn_checkpoint_unavailable"

    async with factory() as session:
        turn = await session.get(TurnGroup, turn_id)
        assert turn is not None and turn.status == "aborted"
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation).where(ToolInvocation.turn_group_id == turn_id)
                )
            ).all()
        )
        assert len(invocations) == 2
        assert all(item.lifecycle == "terminal" for item in invocations)
        provider_attempts_after = int(
            await session.scalar(
                select(func.count(AgentCallAttempt.id)).where(
                    AgentCallAttempt.run_id == run_id,
                    AgentCallAttempt.call_kind == "llm",
                )
            )
            or 0
        )
        assert provider_attempts_after == provider_attempts_before_abort
        assert provider_attempts_after - provider_attempts_before == 0
        job = await session.get(RuntimeJob, job_id)
        assert job is not None and job.status == "dead"
        assert (
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id.in_([item.id for item in invocations])
                )
            )
            == 2
        )
        with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
            await ToolLedger(session).abort_pending(
                old_lease,
                turn_id,
                ticket_id=ticket_id,
                reason="stale_worker_must_fail",
            )
        operands = {
            "probe_call_count": len(probe.calls),
            "new_fencing_token": new_lease.fencing_token,
            "takeover_turn_fencing_token": takeover_turn_fencing_token,
            "turn_fencing_token": turn.fencing_token,
            "invocation_count": len(invocations),
            "terminal_invocation_count": sum(item.lifecycle == "terminal" for item in invocations),
            "provider_attempt_delta": provider_attempts_after - provider_attempts_before,
            "provider_attempt_abort_delta": (
                provider_attempts_after - provider_attempts_before_abort
            ),
            "job_status": job.status,
            "turn_status": turn.status,
            "observation_count": 2,
        }
        for predicate_id in (
            "no_decision_budget_conservative",
            "durable_turn_takeover",
            "unsafe_turn_abort",
        ):
            record_predicate_operands(
                requirement_id="C5-P0-05",
                predicate_id=predicate_id,
                subject_kind="postgres_durable_turn_recovery",
                operands=operands,
            )
        record_predicate_operands(
            requirement_id="C4-P0-03b",
            predicate_id="c4_p0_03b",
            subject_kind="postgres_durable_turn_recovery",
            operands=operands,
        )
    await worker_engine.dispose()
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_partial_tool_turn_replays_terminal_observation_and_calls_only_pending() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_partial_replay_{suffix}"
    message_id = f"message_partial_replay_{suffix}"
    run_id = f"run_partial_replay_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="recover a partially committed tool batch",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="queued",
            model="deterministic-fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            prompt_version="agent.v1",
            schema_version="agent.v1",
            context_version="context.v1.2",
        )
        session.add(run)
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        turn, invocations = await ToolLedger(session).open_turn(
            old_lease,
            segment_id=f"segment_partial_replay_{suffix}",
            tool_round=1,
            decision={"decision_type": "tool_calls"},
            context_manifest={"injected_tool_schema_hash": "a" * 64},
            calls=[
                InvocationSpec("provider-call-account", "query_account", {}, 0),
                InvocationSpec("provider-call-subscription", "query_subscription", {}, 1),
            ],
        )
        await ToolLedger(session).mark_executing(old_lease, invocations[0].id)
        first_attempt = await AttemptLedger(session).reserve(
            old_lease,
            kind="read_mcp",
            logical_invocation_id=invocations[0].id,
            transport_ordinal=1,
        )
        await AttemptLedger(session).finish(
            old_lease,
            first_attempt,
            status="succeeded",
        )
        first_observation = ObservationEnvelope(
            tool_name="query_account",
            tool_call_id="provider-call-account",
            ticket_id=ticket_id,
            run_id=run_id,
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            attempt_index=1,
            status="ok",
            retryable=False,
            observed_at=datetime.now(UTC),
            freshness_class="transactional",
            freshness_status="fresh",
            duration_ms=1,
            resource_version="1",
            data={"account_status": "active"},
        )
        persisted_first = await ToolLedger(session).terminalize(
            old_lease,
            invocations[0].id,
            outcome="succeeded",
            observation=first_observation,
        )
        await ToolLedger(session).mark_executing(old_lease, invocations[1].id)
        pending_attempt = await AttemptLedger(session).reserve(
            old_lease,
            kind="read_mcp",
            logical_invocation_id=invocations[1].id,
            transport_ordinal=1,
        )
        job_id = job.id
        turn_id = turn.id
        invocation_ids = [item.id for item in invocations]
        logical_ids = [item.logical_invocation_id for item in invocations]

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None

    async with factory() as session, session.begin():
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-new",
        )
        await ToolLedger(session).takeover(new_lease, turn_id)

    gateway = RecoveryReadGateway()
    decision = AgentDecision.model_validate(
        {
            "decision_type": "tool_calls",
            "decision_summary": "Read current account and subscription state.",
            "tool_calls": [
                {
                    "tool_call_id": "provider-call-account",
                    "call": {"name": "query_account", "arguments": {}},
                },
                {
                    "tool_call_id": "provider-call-subscription",
                    "call": {"name": "query_subscription", "arguments": {}},
                },
            ],
        }
    )
    async with factory() as session:
        graph = SupportGraph(
            provider=DeterministicFakeProvider(),
            retrieval=None,
            gateway=cast(ToolGateway, gateway),
            session=session,
            test_capability=issue_test_runtime_capability(testing=True),
        )
        output = await graph.read_loop_nodes.execute_reads(
            AgentState(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id=ticket_id,
                run_id=run_id,
                job_id=job_id,
                segment_id=f"segment_partial_replay_{suffix}",
                delivery_generation=2,
                fencing_token=new_lease.fencing_token,
                trace_id=f"trace_partial_replay_{suffix}",
                user_message="recover a partially committed tool batch",
                redacted_message="recover a partially committed tool batch",
                classification={
                    "issue_type": "api_diagnostics",
                    "policy_boundary": "allowed",
                    "requested_action": "none",
                },
                agent_decision=decision.model_dump(mode="json"),
                turn_group_id=turn_id,
                tool_invocation_ids=invocation_ids,
                tool_logical_invocation_ids=logical_ids,
                tool_observations=[],
                provider_turns=[],
                executed_fingerprints=[],
                llm_calls=1,
                tool_rounds=1,
                tool_attempts=0,
            )
        )

    assert gateway.calls == ["query_subscription"]
    assert [item["tool_name"] for item in output["latest_observations"]] == [
        "query_account",
        "query_subscription",
    ]
    assert output["latest_observations"][0]["observation_id"] == persisted_first.id
    assert output["tool_attempts"] == 3
    assert output.get("safe_stop_reason") is None

    async with factory() as session:
        turn = await session.get(TurnGroup, turn_id)
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation)
                    .where(ToolInvocation.turn_group_id == turn_id)
                    .order_by(ToolInvocation.ordinal)
                )
            ).all()
        )
        assert turn is not None and turn.status == "closed"
        assert all(item.lifecycle == "terminal" for item in invocations)
        assert (
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id.in_(invocation_ids)
                )
            )
            == 2
        )
        abandoned = await session.get(AgentCallAttempt, pending_attempt.id)
        assert abandoned is not None
        assert abandoned.status == "unknown"
        assert abandoned.error_code == "fence_takeover"
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_takeover_exhausted_transport_fails_closed_and_cancels_remaining() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_exhausted_replay_{suffix}"
    message_id = f"message_exhausted_replay_{suffix}"
    run_id = f"run_exhausted_replay_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="exhaust the bounded transport before takeover",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="queued",
            model="deterministic-fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            prompt_version="agent.v1",
            schema_version="agent.v1",
            context_version="context.v1.2",
        )
        session.add(run)
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        turn, invocations = await ToolLedger(session).open_turn(
            old_lease,
            segment_id=f"segment_exhausted_replay_{suffix}",
            tool_round=1,
            decision={"decision_type": "tool_calls"},
            context_manifest={"injected_tool_schema_hash": "b" * 64},
            calls=[
                InvocationSpec("provider-call-account", "query_account", {}, 0),
                InvocationSpec("provider-call-subscription", "query_subscription", {}, 1),
            ],
        )
        await ToolLedger(session).mark_executing(old_lease, invocations[0].id)
        first_attempt = await AttemptLedger(session).reserve(
            old_lease,
            kind="read_mcp",
            logical_invocation_id=invocations[0].id,
            transport_ordinal=1,
        )
        await AttemptLedger(session).finish(
            old_lease,
            first_attempt,
            status="failed",
            error_code="tool_unavailable",
        )
        await AttemptLedger(session).reserve(
            old_lease,
            kind="read_mcp",
            logical_invocation_id=invocations[0].id,
            transport_ordinal=2,
        )
        job_id = job.id
        turn_id = turn.id
        invocation_ids = [item.id for item in invocations]
        logical_ids = [item.logical_invocation_id for item in invocations]

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None

    async with factory() as session, session.begin():
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-new",
        )
        await ToolLedger(session).takeover(new_lease, turn_id)

    gateway = RecoveryReadGateway()
    decision = AgentDecision.model_validate(
        {
            "decision_type": "tool_calls",
            "decision_summary": "Read current account and subscription state.",
            "tool_calls": [
                {
                    "tool_call_id": "provider-call-account",
                    "call": {"name": "query_account", "arguments": {}},
                },
                {
                    "tool_call_id": "provider-call-subscription",
                    "call": {"name": "query_subscription", "arguments": {}},
                },
            ],
        }
    )
    async with factory() as session:
        graph = SupportGraph(
            provider=DeterministicFakeProvider(),
            retrieval=None,
            gateway=cast(ToolGateway, gateway),
            session=session,
            test_capability=issue_test_runtime_capability(testing=True),
        )
        output = await graph.read_loop_nodes.execute_reads(
            AgentState(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id=ticket_id,
                run_id=run_id,
                job_id=job_id,
                segment_id=f"segment_exhausted_replay_{suffix}",
                delivery_generation=2,
                fencing_token=new_lease.fencing_token,
                trace_id=f"trace_exhausted_replay_{suffix}",
                user_message="exhaust the bounded transport before takeover",
                redacted_message="exhaust the bounded transport before takeover",
                classification={
                    "issue_type": "api_diagnostics",
                    "policy_boundary": "allowed",
                    "requested_action": "none",
                },
                agent_decision=decision.model_dump(mode="json"),
                turn_group_id=turn_id,
                tool_invocation_ids=invocation_ids,
                tool_logical_invocation_ids=logical_ids,
                tool_observations=[],
                provider_turns=[],
                executed_fingerprints=[],
                llm_calls=1,
                tool_rounds=1,
                tool_attempts=0,
            )
        )

    assert gateway.calls == []
    assert output["safe_stop_reason"] == "tool_transport_budget_exhausted"
    assert output["tool_attempts"] == 3
    assert [item["error_code"] for item in output["latest_observations"]] == [
        "tool_transport_budget_exhausted",
        "cancelled_due_to_terminal_failure",
    ]

    async with factory() as session:
        turn = await session.get(TurnGroup, turn_id)
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation)
                    .where(ToolInvocation.turn_group_id == turn_id)
                    .order_by(ToolInvocation.ordinal)
                )
            ).all()
        )
        assert turn is not None and turn.status == "closed"
        assert [item.outcome for item in invocations] == ["failed", "cancelled"]
        assert all(item.lifecycle == "terminal" for item in invocations)
        assert (
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id.in_(invocation_ids)
                )
            )
            == 2
        )
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_reconciles_unknown_capability_without_resending_effect() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    worker_database_url = os.getenv("TEST_WORKER_DATABASE_URL")
    if not database_url or not worker_database_url:
        pytest.skip("TEST_DATABASE_URL and TEST_WORKER_DATABASE_URL are required")
    engine = _tenant_demo_engine(database_url)
    worker_engine = create_async_engine(worker_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_unknown_{suffix}"
    message_id = f"message_unknown_{suffix}"
    run_id = f"run_unknown_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="reconcile an unknown policy capability",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="queued",
            model="deterministic-fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            prompt_version="agent.v1",
            schema_version="agent.v1",
            context_version="context.v1.2",
        )
        session.add(run)
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner="worker-old")
        observation_binding = [{"observation_id": "billing-observation"}]
        reserved = await PolicyCapabilityLedger(session).reserve(
            lease,
            segment_id=f"segment_unknown_{suffix}",
            capability_name="propose_refund",
            causal_decision=ProposalCausalDecisionV2(
                capability_name="propose_refund",
                action_type="refund",
                resource_id="bill-recovery",
                resource_version=1,
                model_arguments={"billing_record_id": "bill-recovery"},
                observation_binding_hash=canonical_json_hash(observation_binding),
                policy_version="supportguard-policy-gate.v1",
            ),
            observation_binding=observation_binding,
        )
        await PolicyCapabilityLedger(session).finish(
            lease,
            reserved,
            status="unknown",
            error_code="connection_lost_before_commit_proof",
        )
        job_id = job.id

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-new",
        )

    handler = AgentJobHandler(worker_factory, cast(Any, object()))
    outcome = await handler._terminalize_stale_capabilities(new_lease)
    assert outcome == "terminal_failed:stale_capability_reconciled_failed"
    async with worker_factory() as session, session.begin():
        finished = await session.scalar(
            text("SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"),
            {
                "job_id": new_lease.job_id,
                "owner": new_lease.owner,
                "fencing_token": new_lease.fencing_token,
                "outcome": outcome,
            },
        )
    assert isinstance(finished, dict)
    assert finished["status"] == "dead"
    assert finished["outcome"] == "failed:stale_capability_reconciled_failed"

    async with factory() as session:
        invocation = await session.get(PolicyCapabilityInvocation, reserved.id)
        attempt = await session.get(PolicyCapabilityAttempt, reserved.attempt_id)
        results = (
            await session.scalars(
                select(PolicyCapabilityResult).where(
                    PolicyCapabilityResult.invocation_id == reserved.id
                )
            )
        ).all()
        assert invocation is not None and invocation.status == "failed"
        assert attempt is not None and attempt.status == "failed"
        assert len(results) == 1
        assert results[0].payload["resolution"] == "failed"
        assert results[0].payload["effect_status"] == "not_applied"
        job = await session.get(RuntimeJob, job_id)
        run = await session.get(AgentRun, run_id)
        assert job is not None and job.status == "dead"
        assert run is not None and run.status == "failed"
        operands = {
            "outcome": outcome,
            "invocation_status": invocation.status,
            "attempt_status": attempt.status,
            "result_count": len(results),
            "resolution": results[0].payload["resolution"],
            "job_status": job.status,
            "run_status": run.status,
            "effect_resend_count": 0,
        }
        for predicate_id in (
            "unknown_effect_reused",
            "unknown_unprovable_takeover",
            "capability_terminal_complete",
            "duplicate_effect_zero",
        ):
            record_predicate_operands(
                requirement_id="C5-P0-09",
                predicate_id=predicate_id,
                subject_kind="postgres_unknown_capability_reconciliation",
                operands=operands,
            )
        record_predicate_operands(
            requirement_id="C4-P0-04b",
            predicate_id="c4_p0_04b",
            subject_kind="postgres_unknown_capability_reconciliation",
            operands=operands,
        )
    await worker_engine.dispose()
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_marks_unproven_provider_attempt_unknown_without_budget_refund() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = _tenant_demo_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_provider_unknown_{suffix}"
    message_id = f"message_provider_unknown_{suffix}"
    run_id = f"run_provider_unknown_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="provider response was not durably captured",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="queued",
            model="deterministic-fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            prompt_version="agent.v1",
            schema_version="agent.v1",
            context_version="context.v1.2",
        )
        session.add(run)
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        reserved = await AttemptLedger(session).reserve(old_lease, kind="llm")
        job_id = job.id

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-new",
        )

    handler = AgentJobHandler(factory, cast(Any, object()))
    await handler._converge_unresolved_provider_attempt(new_lease, reserved.id)

    async with factory() as session:
        attempt = await session.get(AgentCallAttempt, reserved.id)
        run = await session.get(AgentRun, run_id)
        assert attempt is not None and attempt.status == "unknown"
        assert run is not None and run.llm_calls == 1
    await engine.dispose()


@pytest.mark.parametrize("intake_status", ["received", "parsed"])
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_never_reissues_a_successful_unreplayable_provider_decision(
    intake_status: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    worker_database_url = os.getenv("TEST_WORKER_DATABASE_URL")
    if not database_url or not worker_database_url:
        pytest.skip("TEST_DATABASE_URL and TEST_WORKER_DATABASE_URL are required")
    engine = _tenant_demo_engine(database_url)
    worker_engine = create_async_engine(worker_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_provider_unreplayable_{suffix}"
    message_id = f"message_provider_unreplayable_{suffix}"
    run_id = f"run_provider_unreplayable_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="provider decision must not be issued twice",
            )
        )
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=message_id,
                status="queued",
                model="deterministic-fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="agent.v1",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
        )
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        attempt = await AttemptLedger(session).reserve(
            old_lease,
            kind="llm",
        )
        session.add(
            RawProviderDecisionEnvelope(
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job.id,
                segment_id=f"marker_{suffix}",
                fencing_token=old_lease.fencing_token,
                provider_attempt_id=attempt.id,
                finish_reason="stop",
                response_hash="a" * 64,
                content_hash="b" * 64,
                call_count=0,
                call_manifest=[],
                intake_status=intake_status,
            )
        )
        await AttemptLedger(session).finish(
            old_lease,
            attempt,
            status="succeeded",
        )
        job_id = job.id

    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert job is not None and run is not None
        job.status = "retry_wait"
        job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.status = "queued"
        run.active_job_id = None
        run.active_fencing_token = None
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=job_id,
            owner="worker-new",
        )

    class RuntimeMustNotRun:
        async def run_ticket(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Provider graph must not be reissued")

    outcome = await AgentJobHandler(
        worker_factory,
        cast(Any, RuntimeMustNotRun()),
    )(
        RuntimeJobMessage(
            event_id=f"event_{suffix}",
            delivery_id=f"delivery_{suffix}",
            job_id=job_id,
            run_id=run_id,
            tenant_id="tenant_demo",
            delivery_generation=2,
        ),
        new_lease,
    )
    assert outcome == ("terminal_failed:provider_decision_unreplayable_after_takeover")
    async with worker_factory() as session, session.begin():
        finished = await session.scalar(
            text("SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"),
            {
                "job_id": new_lease.job_id,
                "owner": new_lease.owner,
                "fencing_token": new_lease.fencing_token,
                "outcome": outcome,
            },
        )
    assert isinstance(finished, dict)
    assert finished["status"] == "dead"
    assert finished["outcome"] == ("failed:provider_decision_unreplayable_after_takeover")

    async with factory() as session:
        job = await session.get(RuntimeJob, job_id)
        run = await session.get(AgentRun, run_id)
        attempts = (
            await session.scalars(
                select(AgentCallAttempt).where(
                    AgentCallAttempt.run_id == run_id,
                )
            )
        ).all()
        assert job is not None and job.status == "dead"
        assert job.last_error == "provider_decision_unreplayable_after_takeover"
        assert run is not None and run.status == "failed"
        assert len(attempts) == 1
    await engine.dispose()
    await worker_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_fence_reads_the_heartbeat_refreshed_database_expiry() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    worker_database_url = os.getenv("TEST_WORKER_DATABASE_URL")
    if not database_url or not worker_database_url:
        pytest.skip("TEST_DATABASE_URL and TEST_WORKER_DATABASE_URL are required")
    engine = _tenant_demo_engine(database_url)
    worker_engine = create_async_engine(worker_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_refreshed_lease_{suffix}"
    message_id = f"message_refreshed_lease_{suffix}"
    run_id = f"run_refreshed_lease_{suffix}"

    async with factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="heartbeat must refresh the MCP deadline",
            )
        )
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=message_id,
                status="queued",
                model="deterministic-fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="agent.v1",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
        )
        lease = await jobs.claim(
            job_id=job.id,
            owner="worker-refresh",
            lease_seconds=10,
        )

    async with worker_factory() as session, session.begin():
        heartbeat = await session.scalar(
            text("SELECT supportguard_worker_heartbeat_job(:job_id,:owner,:fencing_token)"),
            {
                "job_id": lease.job_id,
                "owner": lease.owner,
                "fencing_token": lease.fencing_token,
            },
        )
        assert isinstance(heartbeat, dict)
        refreshed = await RuntimeJobRepository(session).assert_fence(lease)
        assert refreshed.lease_expires_at is not None
        assert refreshed.lease_expires_at > lease.expires_at
        observed_expiry = datetime.fromisoformat(str(heartbeat["lease_expires_at"]))
        assert refreshed.lease_expires_at == observed_expiry

    await worker_engine.dispose()
    await engine.dispose()
