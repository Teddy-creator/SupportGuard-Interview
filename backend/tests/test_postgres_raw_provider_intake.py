from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.schemas import AgentDecision, Classification
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.tools import ObservationEnvelope, SourceRef, ToolCallContext
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    CheckpointCommitMarker,
    CitationBinding,
    ContextLedger,
    ContextMembership,
    RawProviderDecisionEnvelope,
    SupportTicket,
    TicketMessage,
    ToolInvocation,
    ToolObservation,
    TurnGroup,
)
from supportguard.providers.base import (
    ProviderCallResult,
    ProviderUsage,
    RawProviderDecision,
    RawProviderToolCall,
    canonical_transport_record,
    raw_decision_from_typed,
)
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.services.runtime_jobs import RuntimeJobRepository
from supportguard.tools.gateway import ReadToolCall, ToolGateway

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


class InvalidNativeToolProvider(DeterministicFakeProvider):
    async def decide(self, **kwargs: Any) -> ProviderCallResult[RawProviderDecision]:
        transport = canonical_transport_record(
            {
                "system": kwargs["system"],
                "context": kwargs["context"],
                "tools": kwargs["tools"],
                "prior_turns": kwargs["prior_turns"],
            }
        )
        return ProviderCallResult(
            RawProviderDecision(
                finish_reason="tool_calls",
                content=None,
                tool_calls=(
                    RawProviderToolCall("same-id", "unknown_read", "{}", 0),
                    RawProviderToolCall("same-id", "search_knowledge", "{not-json", 1),
                    RawProviderToolCall("runtime-id", "execute_refund", "{}", 2),
                ),
            ),
            attempts=1,
            usage=ProviderUsage(),
            trace_metadata={},
            transport=transport,
        )


class OverLimitNativeToolProvider(InvalidNativeToolProvider):
    async def decide(self, **kwargs: Any) -> ProviderCallResult[RawProviderDecision]:
        transport = canonical_transport_record(
            {
                "system": kwargs["system"],
                "context": kwargs["context"],
                "tools": kwargs["tools"],
                "prior_turns": kwargs["prior_turns"],
            }
        )
        return ProviderCallResult(
            RawProviderDecision(
                finish_reason="tool_calls",
                content=None,
                tool_calls=tuple(
                    RawProviderToolCall(f"over-{ordinal}", "query_account", "{}", ordinal)
                    for ordinal in range(4)
                ),
            ),
            attempts=1,
            usage=ProviderUsage(),
            trace_metadata={},
            transport=transport,
        )


class ObservationContextProvider(DeterministicFakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.decisions = 0

    async def generate(self, **kwargs: Any) -> ProviderCallResult[Any]:
        if kwargs["output_schema"] is not Classification:
            return await super().generate(**kwargs)
        transport = canonical_transport_record(
            {
                "system": kwargs["system"],
                "user": kwargs["user"],
                "output_schema": kwargs["output_schema"].model_json_schema(),
                "trace_metadata": kwargs["trace_metadata"],
            }
        )
        return ProviderCallResult(
            Classification(
                issue_type="api_diagnostics",
                risk="low",
                policy_boundary="allowed",
                requested_action="none",
                requested_concurrency_limit=None,
                needs_realtime_facts=True,
                support_subject="customer_problem",
                rationale="Fixture-owned current account fact request.",
            ),
            attempts=1,
            usage=ProviderUsage(),
            trace_metadata={},
            transport=transport,
        )

    async def decide(self, **kwargs: Any) -> ProviderCallResult[RawProviderDecision]:
        self.decisions += 1
        transport = canonical_transport_record(
            {
                "system": kwargs["system"],
                "context": kwargs["context"],
                "tools": kwargs["tools"],
                "prior_turns": kwargs["prior_turns"],
                "trace_metadata": kwargs["trace_metadata"],
            }
        )
        if self.decisions == 1:
            decision = AgentDecision.model_validate(
                {
                    "decision_type": "tool_calls",
                    "decision_summary": "Read the current scoped account.",
                    "tool_calls": [
                        {
                            "tool_call_id": "account-current",
                            "call": {"name": "query_account", "arguments": {}},
                        }
                    ],
                }
            )
        else:
            context = json.loads(str(kwargs["context"]))
            observations = {item["tool_name"]: item for item in context["latest_observations"]}
            if set(observations) == {"query_account"}:
                decision = AgentDecision.model_validate(
                    {
                        "decision_type": "tool_calls",
                        "decision_summary": "Read the current scoped balance.",
                        "tool_calls": [
                            {
                                "tool_call_id": "usage-current",
                                "call": {
                                    "name": "query_api_usage",
                                    "arguments": {"window": "1m"},
                                },
                            }
                        ],
                    }
                )
            else:
                assert set(observations) == {"query_account", "query_api_usage"}
                decision = AgentDecision.model_validate(
                    {
                        "decision_type": "final_candidate",
                        "decision_summary": "Answer from the current scoped Observations.",
                        "candidate": {
                            "answer": "当前账户状态为 active，余额为 120.00 USD。",
                            "action": "answer",
                            "knowledge_chunk_ids": [],
                            "business_source_ids": [
                                "customer:cust_demo",
                                "usage:cust_demo:1m",
                            ],
                            "material_claims": [
                                {
                                    "text": "当前账户状态为 active。",
                                    "observation_source_ids": ["customer:cust_demo"],
                                },
                                {
                                    "text": "当前余额为 120.00 USD。",
                                    "observation_source_ids": ["usage:cust_demo:1m"],
                                },
                            ],
                            "proposed_arguments": {},
                        },
                    }
                )
        return ProviderCallResult(
            raw_decision_from_typed(decision),
            attempts=1,
            usage=ProviderUsage(),
            trace_metadata={},
            transport=transport,
        )


class ObservationContextGateway:
    async def rehandshake_read(self, *, failed_generation: int | None = None) -> int:
        del failed_generation
        return 1

    async def call_read(
        self,
        call: ReadToolCall,
        context: ToolCallContext,
        *,
        allow_retry: bool = True,
    ) -> ObservationEnvelope:
        del allow_retry
        observed_at = datetime.now(UTC)
        if call.name == "query_account":
            source_id = "customer:cust_demo"
            data = {
                "customer_id": "cust_demo",
                "account_status": "active",
                "security_status": "normal",
                "region": "eu-west",
                "version": 3,
            }
        else:
            assert call.name == "query_api_usage"
            source_id = "usage:cust_demo:1m"
            data = {
                "customer_id": "cust_demo",
                "window": "1m",
                "request_count": 20,
                "concurrency_current": 8,
                "concurrency_peak": 8,
                "remaining_balance": "120.00",
                "balance_currency": "USD",
            }
        return ObservationEnvelope(
            tool_name=call.name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            attempt_index=1,
            status="ok",
            retryable=False,
            observed_at=observed_at,
            duration_ms=1,
            source_refs=[
                SourceRef(
                    source_type="business_record",
                    source_id=source_id,
                    observed_at=observed_at,
                )
            ],
            data=data,
        )


@pytest.mark.parametrize(
    ("provider", "expected_calls"),
    [(InvalidNativeToolProvider(), 3), (OverLimitNativeToolProvider(), 4)],
)
async def test_raw_native_calls_are_audited_before_typed_validation(
    provider: DeterministicFakeProvider,
    expected_calls: int,
) -> None:
    url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_raw_{suffix}"
    message_id = f"message_raw_{suffix}"
    run_id = f"run_raw_{suffix}"
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        await session.flush()
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="请诊断当前产品问题",
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
                model="invalid-native-fixture",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="fault.v1",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo", run_id=run_id, kind="agent_start"
        )
        lease = await RuntimeJobRepository(session).claim(job_id=job.id, owner="worker-raw-intake")
        state = AgentState(
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            run_id=run_id,
            job_id=job.id,
            segment_id=f"segment_raw_{suffix}",
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            trace_id=f"trace_raw_{suffix}",
            user_message="请诊断当前产品问题",
        )

    async with factory() as session:
        graph = SupportGraph(
            provider=provider,
            retrieval=None,
            gateway=ToolGateway(None),
            session=session,
        )
        output = await graph.run(state)
        assert output["agent_finish_reason"] == "provider_decision_invalid"

    async with factory() as session:
        raw = await session.scalar(
            select(RawProviderDecisionEnvelope).where(RawProviderDecisionEnvelope.run_id == run_id)
        )
        assert raw is not None
        assert raw.call_count == expected_calls
        assert raw.intake_status == "rejected"
        assert [item["ordinal"] for item in raw.call_manifest] == list(range(expected_calls))
        turn = await session.scalar(select(TurnGroup).where(TurnGroup.run_id == run_id))
        assert turn is not None and turn.status == "closed"
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation)
                    .where(ToolInvocation.turn_group_id == turn.id)
                    .order_by(ToolInvocation.ordinal)
                )
            ).all()
        )
        assert len(invocations) == expected_calls
        if expected_calls == 3:
            assert [item.provider_tool_call_id for item in invocations[:2]] == [
                "same-id",
                "same-id",
            ]
        assert all(item.lifecycle == "terminal" for item in invocations)
        assert (
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id.in_([item.id for item in invocations])
                )
            )
            == expected_calls
        )
        assert (
            await session.scalar(
                select(func.count(AgentCallAttempt.id)).where(
                    AgentCallAttempt.run_id == run_id,
                    AgentCallAttempt.call_kind == "read_mcp",
                )
            )
            == 0
        )
        stored_run = await session.get(AgentRun, run_id)
        assert stored_run is not None
        assert stored_run.tool_rounds == 1
        # Duplicate provider ids reject the entire batch before Tool Attempt or
        # physical transport reservation while preserving one Observation/ordinal.
        assert stored_run.tool_attempts == 0
        ledgers = list(
            (
                await session.scalars(select(ContextLedger).where(ContextLedger.run_id == run_id))
            ).all()
        )
        assert ledgers
        assert all(item.canonical_request_bytes is None for item in ledgers)
        assert all(item.request_storage_mode == "hash_only" for item in ledgers)
        assert all(item.canonical_request_hash for item in ledgers)
        assert all(item.runtime_provenance["provider_mode"] == provider.mode for item in ledgers)
        assert all(item.runtime_provenance["model"] == provider.model for item in ledgers)
        assert {item.runtime_provenance["runtime_manifest_hash"] for item in ledgers} == {
            graph.runtime_manifest.content_hash
        }
        assert all(
            item.runtime_provenance["prompt_version"]
            == "agent_decide.v6+bound_evidence_synthesis.v1"
            and item.runtime_provenance["schema_version"] == "agent-contract.v5.2"
            and item.runtime_provenance["code_commit"]
            for item in ledgers
        )
        attempts = list(
            (
                await session.scalars(
                    select(AgentCallAttempt).where(AgentCallAttempt.run_id == run_id)
                )
            ).all()
        )
        assert attempts
        assert all(item.runtime_provenance["model"] == provider.model for item in attempts)
        assert {item.runtime_provenance["runtime_manifest_hash"] for item in attempts} == {
            graph.runtime_manifest.content_hash
        }
    await engine.dispose()


async def test_provider_visible_business_observation_has_durable_context_membership() -> None:
    url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_context_{suffix}"
    message_id = f"message_context_{suffix}"
    run_id = f"run_context_{suffix}"
    marker_id = f"marker_context_{suffix}"
    provider = ObservationContextProvider()
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="queued",
            )
        )
        await session.flush()
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="请告诉我当前账户状态和余额。",
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
                model=provider.model,
                provider_mode=provider.mode,
                tool_call_mode=provider.tool_call_mode,
                prompt_version="agent_decide.v5",
                schema_version="agent-contract.v5",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo", run_id=run_id, kind="agent_start"
        )
        lease = await RuntimeJobRepository(session).claim(
            job_id=job.id, owner="worker-observation-context"
        )
        session.add(
            CheckpointCommitMarker(
                id=marker_id,
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job.id,
                fencing_token=lease.fencing_token,
                delivery_generation=1,
                segment_kind="agent",
                status="prepared",
                private_namespace=f"private/{run_id}/1",
                expected_run_version=0,
                expected_run_status="queued",
                expected_ticket_sequence=0,
                prepared_payload_hash="1" * 64,
                segment_input_hash="2" * 64,
            )
        )
        state = AgentState(
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            run_id=run_id,
            job_id=job.id,
            segment_id=marker_id,
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            trace_id=f"trace_context_{suffix}",
            user_message="请告诉我当前账户状态和余额。",
        )

    async with factory() as session:
        graph = SupportGraph(
            provider=provider,
            retrieval=None,
            gateway=cast(ToolGateway, ObservationContextGateway()),
            session=session,
        )
        output = await graph.run(state)
        assert output["final"]["terminal_state"] == "resolved", {
            key: output.get(key)
            for key in (
                "final",
                "classification",
                "agent_decision",
                "agent_finish_reason",
                "safe_stop_reason",
                "tool_rounds",
                "tool_attempts",
                "tool_observations",
            )
        }
        assert provider.decisions >= 2

    async with factory() as session:
        memberships = list(
            (
                await session.scalars(
                    select(ContextMembership)
                    .where(ContextMembership.run_id == run_id)
                    .order_by(ContextMembership.payload_ordinal)
                )
            ).all()
        )
        assert len(memberships) == 2
        assert {item.schema_version for item in memberships} == {"context-membership.v2"}
        assert [item.payload_ordinal for item in memberships] == [0, 1]
        assert [item.payload_json_pointer for item in memberships] == [
            "/latest_observations/0",
            "/latest_observations/1",
        ]
        assert {len(item.serialized_evidence_fragment_hash) for item in memberships} == {64}
        observations = list(
            (
                await session.scalars(
                    select(ToolObservation).where(
                        ToolObservation.invocation_id.in_(
                            [item.logical_invocation_id for item in memberships]
                        )
                    )
                )
            ).all()
        )
        assert len(observations) == 2
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation).where(
                        ToolInvocation.id.in_([item.logical_invocation_id for item in memberships])
                    )
                )
            ).all()
        )
        assert {item.tool_name for item in invocations} == {
            "query_account",
            "query_api_usage",
        }
        observation_by_invocation = {item.invocation_id: item for item in observations}
        for membership in memberships:
            observation = observation_by_invocation[membership.logical_invocation_id]
            assert membership.origin_job_id == observation.job_id
            assert membership.origin_marker_id == observation.segment_id == marker_id
            assert membership.origin_fencing_token == observation.fencing_token
        ledger_ids = {item.context_ledger_id for item in memberships}
        assert len(ledger_ids) == 1
        ledger = await session.get(ContextLedger, ledger_ids.pop())
        assert ledger is not None
        root_hashes = {item.ordered_membership_root_hash for item in memberships}
        assert root_hashes == {ledger.component_manifest["observation_membership_root_hash"]}
        assert (
            await session.scalar(
                select(func.count(CitationBinding.id)).where(
                    CitationBinding.context_ledger_id == ledger.id
                )
            )
            == 0
        )
        root_input = [
            {
                "payload_ordinal": membership.payload_ordinal,
                "payload_json_pointer": membership.payload_json_pointer,
                "fragment_hash": membership.serialized_evidence_fragment_hash,
            }
            for membership in memberships
        ]
        assert root_hashes == {canonical_json_hash(root_input)}
    await engine.dispose()
