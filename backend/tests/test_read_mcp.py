import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import (
    ApiKeyMetadataInput,
    BillingRecordInput,
    IncidentImpactInput,
    KnowledgeSearchInput,
    NoArguments,
    RequestTraceInput,
    ServiceStatusInput,
    ToolCallContext,
    UsageInput,
)
from supportguard.db.base import Base
from supportguard.db.models import (
    AgentRun,
    ConversationTurn,
    RetrievalTrace,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.seed import seed_demo_data
from supportguard.mcp.client import action_mcp_session, read_mcp_session
from supportguard.mcp.read_server import _restricted_trace_id
from supportguard.mcp.runtime import MCPCallResult, MCPManager
from supportguard.rag.embeddings import DeterministicEmbedding
from supportguard.rag.ingest import ingest_corpus
from supportguard.tools.gateway import (
    READ_TOOL_ARGUMENTS,
    ActionToolCall,
    ReadToolCall,
    ToolGateway,
    canonical_schema_hash,
    internal_mcp_transport_schema,
    model_argument_schema,
    native_read_tool_schemas,
    read_tool_schema_hashes,
)


async def prepare_database(path: Path) -> str:
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_demo_data(session)
        session.add(
            SupportTicket(
                id="ticket_mcp",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="open",
            )
        )
        session.add(
            TicketMessage(
                id="message_mcp",
                tenant_id="tenant_demo",
                ticket_id="ticket_mcp",
                role="user",
                content="MCP integration fixture",
            )
        )
        await session.flush()
        run = AgentRun(
            id="run_mcp",
            tenant_id="tenant_demo",
            ticket_id="ticket_mcp",
            customer_id="cust_demo",
            message_id="message_mcp",
            status="interrupted",
            checkpoint_stage="awaiting_approval",
            checkpoint_id="checkpoint_mcp",
            model="fake",
            provider_mode="fake",
            tool_call_mode="native",
            prompt_version="v1.1",
            schema_version="agent.v1",
            context_version="context.v1",
        )
        session.add(run)
        await session.flush()
        turn = ConversationTurn(
            id="turn_mcp",
            tenant_id="tenant_demo",
            ticket_id="ticket_mcp",
            customer_message_id="message_mcp",
            run_id=run.id,
            ordinal=1,
            activity_state="waiting_external",
            automation_mode="agent",
            model=run.model,
            provider_mode=run.provider_mode,
            tool_call_mode=run.tool_call_mode,
            context_version=run.context_version,
        )
        session.add(turn)
        run.turn_id = turn.id
        for suffix in ("escalation", "key", "entitlement"):
            ticket_id = f"ticket_mcp_{suffix}"
            message_id = f"message_mcp_{suffix}"
            session.add(
                SupportTicket(
                    id=ticket_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    status="open",
                )
            )
            session.add(
                TicketMessage(
                    id=message_id,
                    tenant_id="tenant_demo",
                    ticket_id=ticket_id,
                    role="user",
                    content=f"MCP {suffix} integration fixture",
                )
            )
            await session.flush()
            run = AgentRun(
                id=f"run_mcp_{suffix}",
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=message_id,
                status="interrupted",
                checkpoint_stage="awaiting_approval",
                checkpoint_id=f"checkpoint_mcp_{suffix}",
                model="fake",
                provider_mode="fake",
                tool_call_mode="native",
                prompt_version="v1.1",
                schema_version="agent.v1",
                context_version="context.v1",
            )
            session.add(run)
            await session.flush()
            turn = ConversationTurn(
                id=f"turn_mcp_{suffix}",
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_message_id=message_id,
                run_id=run.id,
                ordinal=1,
                activity_state="waiting_external",
                automation_mode="agent",
                model=run.model,
                provider_mode=run.provider_mode,
                tool_call_mode=run.tool_call_mode,
                context_version=run.context_version,
            )
            session.add(turn)
            run.turn_id = turn.id
        await ingest_corpus(
            session,
            root=Path.cwd(),
            manifest_path=Path("knowledge/manifests/documents.json"),
            embedding=DeterministicEmbedding(),
        )
        await session.commit()
    await engine.dispose()
    return url


@pytest.mark.asyncio
async def test_gateway_collapses_only_exact_duplicate_source_references() -> None:
    observed_at = datetime.now(UTC).replace(microsecond=0)
    exact = {
        "source_type": "business_record",
        "source_id": "api_usage_bucket:one-minute",
        "observed_at": observed_at.isoformat(),
    }
    conflicting = {
        **exact,
        "observed_at": (observed_at + timedelta(seconds=1)).isoformat(),
    }

    class DuplicateSourceTransport:
        async def call(
            self,
            server_name: str,
            tool_name: str,
            arguments: dict[str, object],
            *,
            reconnect_once: bool,
        ) -> MCPCallResult:
            assert (server_name, tool_name, reconnect_once) == (
                "read",
                "query_api_usage",
                False,
            )
            del arguments
            return MCPCallResult(
                value={
                    "tool_call_id": "tool_usage_duplicate",
                    "ticket_id": "ticket_usage_duplicate",
                    "window": "1m",
                    "window_start": (observed_at - timedelta(minutes=1)).isoformat(),
                    "window_end": observed_at.isoformat(),
                    "request_count": 5,
                    "input_token_count": 10,
                    "output_token_count": 3,
                    "concurrency_current": 2,
                    "concurrency_peak": 3,
                    "remaining_balance": "120.00",
                    "balance_currency": "USD",
                    "freshness_seconds": 0,
                    "freshness_status": "fresh",
                    "observed_at": observed_at.isoformat(),
                    "resource_version": "usage-v1",
                    "source_refs": [exact, exact, conflicting],
                },
                attempts=1,
            )

    gateway = ToolGateway(DuplicateSourceTransport())  # type: ignore[arg-type]
    observation = await gateway.call_read(
        ReadToolCall(name="query_api_usage", arguments={"window": "1m"}),
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_usage_duplicate",
            run_id="run_usage_duplicate",
            job_id="job_usage_duplicate",
            segment_id="segment_usage_duplicate",
            delivery_generation=1,
            fencing_token=1,
            tool_call_id="tool_usage_duplicate",
            trace_id="trace_usage_duplicate",
        ),
    )

    assert observation.status == "ok"
    assert len(observation.source_refs) == 2
    assert observation.source_refs[0].observed_at == observed_at
    assert observation.source_refs[1].observed_at == observed_at + timedelta(seconds=1)


def bind_hermetic_mcp_database(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """Own every database capability used by an MCP child in this test process."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MCP_READ_DATABASE_URL", database_url)
    monkeypatch.setenv("MCP_ACTION_DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")


def read_transport(
    arguments: dict[str, object], *, tool_call_id: str, trace_id: str
) -> dict[str, object]:
    return {
        "arguments": arguments,
        "trusted_context": {
            "customer_id": "cust_demo",
            "ticket_id": "ticket_mcp",
            "run_id": "run_mcp",
            "tool_call_id": tool_call_id,
            "trace_id": trace_id,
            "tenant_id": "tenant_demo",
            "job_id": "job_mcp",
            "fencing_token": 1,
            "segment_id": "segment_mcp",
            "delivery_generation": 1,
        },
    }


def test_restricted_trace_identity_is_stable_per_logical_invocation() -> None:
    first = _restricted_trace_id("logical-invocation-1")
    assert first == _restricted_trace_id("logical-invocation-1")
    assert first != _restricted_trace_id("logical-invocation-2")
    assert first.startswith("retrieval_")
    assert len(first) == 64


@pytest.mark.parametrize(
    ("name", "arguments", "expected_type"),
    [
        ("query_account", {}, NoArguments),
        ("query_subscription", {}, NoArguments),
        ("query_api_usage", {"window": "1m"}, UsageInput),
        (
            "check_service_status",
            {"model": "atlas-chat", "region": "eu-west"},
            ServiceStatusInput,
        ),
        (
            "query_billing_record",
            {"billing_record_id": "bill_demo_duplicate"},
            BillingRecordInput,
        ),
        ("query_request_trace", {"request_id": "req_demo_429"}, RequestTraceInput),
        (
            "query_api_key_metadata",
            {"api_key_ref": "key_demo_leaked"},
            ApiKeyMetadataInput,
        ),
        (
            "query_incident_impact",
            {"request_id": "req_demo_429"},
            IncidentImpactInput,
        ),
        (
            "search_knowledge",
            {"query": "429 concurrency_limit_exceeded"},
            KnowledgeSearchInput,
        ),
    ],
)
def test_provider_shaped_read_tool_json_uses_name_discriminated_exact_schema(
    name: str,
    arguments: dict[str, object],
    expected_type: type[object],
) -> None:
    parsed = ReadToolCall.model_validate({"name": name, "arguments": arguments})
    assert type(parsed.arguments) is expected_type
    assert parsed.arguments.model_dump(mode="json") == arguments


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("query_request_trace", {"incident_id": "inc_demo"}),
        ("query_incident_impact", {"request": "req_demo_429"}),
        ("query_api_usage", {"window": "2m"}),
        ("query_account", {"customer_id": "forged"}),
        ("search_knowledge", {"query": 42}),
    ],
)
def test_provider_shaped_read_tool_json_rejects_wrong_fields_and_types(
    name: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        ReadToolCall.model_validate({"name": name, "arguments": arguments})


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_read_server_handshake_discovery_and_scoped_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = await prepare_database(tmp_path / "mcp.db")
    bind_hermetic_mcp_database(monkeypatch, database_url)
    async with read_mcp_session() as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "query_account",
            "query_subscription",
            "query_api_usage",
            "check_service_status",
            "query_billing_record",
            "query_request_trace",
            "query_api_key_metadata",
            "query_incident_impact",
            "search_knowledge",
        }
        assert "propose_refund" not in {tool.name for tool in tools.tools}
        discovered = {tool.name: tool.inputSchema for tool in tools.tools}
        provider = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in native_read_tool_schemas(set(READ_TOOL_ARGUMENTS))
        }
        trusted_names = {
            "tenant_id",
            "customer_id",
            "ticket_id",
            "run_id",
            "job_id",
            "segment_id",
            "delivery_generation",
            "fencing_token",
            "logical_invocation_id",
            "tool_attempt_id",
            "transport_attempt_id",
            "call_deadline",
            "worker_deadline",
            "trace_id",
        }
        model_match_count = 0
        transport_match_count = 0
        distinct_hash_count = 0
        trusted_visible_count = 0
        for name in READ_TOOL_ARGUMENTS:
            model_schema = model_argument_schema(name)
            assert provider[name] == model_schema
            assert discovered[name] == internal_mcp_transport_schema(name)
            model_match_count += int(provider[name] == model_schema)
            transport_match_count += int(discovered[name] == internal_mcp_transport_schema(name))
            model_hash, transport_hash = read_tool_schema_hashes(name)
            assert model_hash == canonical_schema_hash(provider[name])
            assert transport_hash == canonical_schema_hash(discovered[name])
            assert model_hash != transport_hash
            assert trusted_names.isdisjoint(str(provider[name]))
            distinct_hash_count += int(model_hash != transport_hash)
            trusted_visible_count += sum(field in str(provider[name]) for field in trusted_names)

    manager = MCPManager(timeout_seconds=5)
    await manager.start()
    gateway = ToolGateway(manager)
    result = await gateway.call_read(
        ReadToolCall(name="query_account", arguments={}),
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_mcp",
            run_id="run_mcp",
            job_id="job_mcp",
            segment_id="segment_mcp",
            delivery_generation=1,
            fencing_token=1,
            tool_call_id="tool_test_001",
            trace_id="trace_test_001",
        ),
    )
    assert result.status == "ok"
    assert result.data["account_status"] == "active"
    assert result.data["security_status"] == "normal"
    assert "customer_id" not in result.data
    assert result.tool_call_id == "tool_test_001"

    calls: dict[str, dict[str, object]] = {
        "query_account": {},
        "query_subscription": {},
        "query_api_usage": {"window": "1m"},
        "check_service_status": {"model": "atlas-chat", "region": "eu-west"},
        "query_billing_record": {"billing_record_id": "bill_demo_duplicate"},
        "query_request_trace": {"request_id": "req_demo_429"},
        "query_api_key_metadata": {"api_key_ref": "key_demo_leaked"},
        "query_incident_impact": {"request_id": "req_demo_429"},
        "search_knowledge": {"query": "429 concurrency_limit_exceeded"},
    }
    roundtrip_statuses: list[str] = []
    observation_field_counts: list[int] = []
    for ordinal, (name, arguments) in enumerate(calls.items(), start=1):
        observation = await gateway.call_read(
            ReadToolCall.model_validate({"name": name, "arguments": arguments}),
            ToolCallContext(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id="ticket_mcp",
                run_id="run_mcp",
                job_id="job_mcp",
                segment_id="segment_mcp",
                delivery_generation=1,
                fencing_token=1,
                tool_call_id=f"roundtrip-{ordinal}",
                trace_id=f"trace-roundtrip-{ordinal}",
            ),
        )
        assert observation.tool_name == name
        assert observation.status in {"ok", "not_found"}
        assert observation.ticket_id == "ticket_mcp"
        assert observation.run_id == "run_mcp"
        # The transport gateway cannot mint trusted scope provenance. The
        # deterministic graph binds these fields from the accepted run before
        # persistence and obligation evaluation.
        assert observation.tenant_id is None
        assert observation.customer_id is None
        assert observation.scope_hash is None
        roundtrip_statuses.append(observation.status)
        observation_field_counts.append(len(observation.model_dump()))
        assert set(observation.model_dump()) == {
            "schema_version",
            "tool_name",
            "tool_call_id",
            "ticket_id",
            "run_id",
            "tenant_id",
            "customer_id",
            "scope_hash",
            "attempt_index",
            "status",
            "retryable",
            "error_code",
            "safe_error_summary",
            "observed_at",
            "freshness_class",
            "freshness_status",
            "fresh_until",
            "duration_ms",
            "source_refs",
            "resource_version",
            "data",
        }
    knowledge = await gateway.call_read(
        ReadToolCall(
            name="search_knowledge",
            arguments=KnowledgeSearchInput(query="429 concurrency_limit_exceeded"),
        ),
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_mcp",
            run_id="run_mcp",
            job_id="job_mcp",
            segment_id="segment_mcp",
            delivery_generation=1,
            fencing_token=1,
            tool_call_id="tool_search_001",
            trace_id="trace_search_001",
        ),
    )
    assert knowledge.status == "ok"
    assert knowledge.data["evidence"]
    first_evidence = knowledge.data["evidence"][0]
    assert first_evidence["supporting_span_eligible"] is True
    assert (
        first_evidence["source_locator"]["byte_end"]
        - first_evidence["source_locator"]["byte_start"]
        <= first_evidence["chunk_locator"]["byte_end"]
        - first_evidence["chunk_locator"]["byte_start"]
    )
    credential_policy = await gateway.call_read(
        ReadToolCall(
            name="search_knowledge",
            arguments=KnowledgeSearchInput(query="API Key 疑似泄露，请立即撤销"),
        ),
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_mcp",
            run_id="run_mcp",
            job_id="job_mcp",
            segment_id="segment_mcp",
            delivery_generation=1,
            fencing_token=1,
            tool_call_id="tool_search_credential_policy",
            trace_id="trace_search_credential_policy",
        ),
    )
    assert credential_policy.status == "ok"
    assert any(
        item["supporting_span_eligible"] is True for item in credential_policy.data["evidence"]
    )
    unrelated = await gateway.call_read(
        ReadToolCall(
            name="search_knowledge",
            arguments=KnowledgeSearchInput(query="banana astronomy violin"),
        ),
        ToolCallContext(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_mcp",
            run_id="run_mcp",
            job_id="job_mcp",
            segment_id="segment_mcp",
            delivery_generation=1,
            fencing_token=1,
            tool_call_id="tool_search_unrelated",
            trace_id="trace_search_unrelated",
        ),
    )
    assert unrelated.status == "ok"
    assert unrelated.data["evidence"] == []
    assert unrelated.data["refusal_reason"] == "insufficient_relevance"
    trace_engine = create_async_engine(database_url)
    trace_factory = async_sessionmaker(trace_engine, expire_on_commit=False)
    async with trace_factory() as trace_session:
        traces = list((await trace_session.scalars(select(RetrievalTrace))).all())
        assert len(traces) == 4
        assert all(item.corpus_snapshot_id for item in traces)
        assert all(
            all("vector_similarity" in candidate for candidate in item.vector_candidates)
            for item in traces
        )
        assert all(
            all("keyword_score" in candidate for candidate in item.keyword_candidates)
            for item in traces
        )
    await trace_engine.dispose()
    await manager.stop()
    operands = {
        "tool_count": len(READ_TOOL_ARGUMENTS),
        "model_match_count": model_match_count,
        "transport_match_count": transport_match_count,
        "distinct_hash_count": distinct_hash_count,
        "trusted_visible_count": trusted_visible_count,
        "roundtrip_count": len(roundtrip_statuses),
        "roundtrip_statuses": sorted(set(roundtrip_statuses)),
        "observation_field_counts": sorted(set(observation_field_counts)),
    }
    for predicate_id in (
        "model_argument_projection_exact",
        "internal_mcp_transport_hash_exact",
        "trusted_context_not_model_visible",
        "tool_schema_impl_roundtrip",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-16",
            predicate_id=predicate_id,
            subject_kind="mcp_schema_runtime",
            operands=operands,
        )


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_action_server_isolated_discovery_and_proposal_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind_hermetic_mcp_database(monkeypatch, await prepare_database(tmp_path / "action.db"))
    async with action_mcp_session() as discovery_session:
        tools = await discovery_session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {
            "propose_refund",
            "propose_api_key_revocation",
            "propose_entitlement_change",
        }
        assert "query_account" not in names
        assert "execute_refund" not in names
        schemas = {tool.name: tool.inputSchema for tool in tools.tools}
        assert all("checkpoint_id" in schema["properties"] for schema in schemas.values())
        assert all("checkpoint_id" not in schema.get("required", []) for schema in schemas.values())

    async with action_mcp_session() as action_session:

        class SessionTransport:
            async def call(
                self,
                server_name: str,
                tool_name: str,
                arguments: dict[str, object],
                *,
                reconnect_once: bool,
            ) -> MCPCallResult:
                assert server_name == "action"
                assert reconnect_once is False
                return MCPCallResult(
                    value=await action_session.call_tool(tool_name, arguments), attempts=1
                )

        gateway = ToolGateway(
            manager=SessionTransport(),  # type: ignore[arg-type]
            test_capability=issue_test_runtime_capability(testing=True),
        )

        def action_context(
            suffix: str, ordinal: int, *, with_checkpoint: bool = True
        ) -> ToolCallContext:
            suffix_part = f"_{suffix}" if suffix else ""
            return ToolCallContext(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id=f"ticket_mcp{suffix_part}",
                run_id=f"run_mcp{suffix_part}",
                job_id="job_mcp",
                segment_id="segment_mcp",
                delivery_generation=1,
                fencing_token=1,
                checkpoint_id=(f"checkpoint_mcp{suffix_part}" if with_checkpoint else None),
                tool_call_id=f"tool_action_{ordinal}",
                trace_id=f"trace_action_{ordinal}",
            )

        result = await gateway.call_action(
            ActionToolCall(
                name="propose_refund",
                arguments={
                    "billing_record_id": "bill_demo_duplicate",
                    "refund_reason": "Explicit duplicate relation verified for review.",
                },
            ),
            action_context("", 1),
        )
        assert result.status == "ok", (result.error_code, result.safe_error_summary)
        assert result.data["status"] == "pending"
        assert result.data["approval_id"]
        proposal_cases = [
            (
                "propose_api_key_revocation",
                "key",
                {
                    "api_key_id": "key_demo_leaked",
                    "reason": "Customer reported a suspected credential exposure.",
                },
                "api_key_revocation",
            ),
            (
                "propose_entitlement_change",
                "entitlement",
                {
                    "subscription_id": "sub_demo",
                    "change_type": "quota_change",
                    "target": {"concurrency_limit": 60},
                    "reason": "Customer requested a verified concurrency increase.",
                },
                "entitlement_change",
            ),
        ]
        for ordinal, (name, suffix, arguments, action_type) in enumerate(proposal_cases, start=2):
            proposal = await gateway.call_action(
                ActionToolCall(name=name, arguments=arguments),  # type: ignore[arg-type]
                action_context(suffix, ordinal),
            )
            assert proposal.status == "ok"
            assert proposal.data["status"] == "draft"
            assert proposal.data["action_type"] == action_type
    record_predicate_operands(
        requirement_id="C4-P0-01c",
        predicate_id="c4_p0_01c",
        subject_kind="action_mcp_isolation_contract",
        operands={
            "discovered_tools": sorted(names),
            "discovered_tool_count": len(names),
            "query_account_discovered": "query_account" in names,
            "execute_refund_discovered": "execute_refund" in names,
            "proposal_status": result.data["status"],
            "approval_id": result.data["approval_id"],
            "production_shape_call_count": 3,
        },
    )


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_backend_manages_long_lived_mcp_sessions_and_clean_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind_hermetic_mcp_database(monkeypatch, await prepare_database(tmp_path / "managed.db"))
    manager = MCPManager(timeout_seconds=5)
    await manager.start()
    health = manager.health()
    assert health["read"]["session"] == "ready"
    assert health["action"]["session"] == "ready"
    assert health["read"]["state"] == "ready"
    assert health["read"]["generation"] == 1
    assert health["read"]["pending_calls"] == 0
    assert health["read"]["schema_hash"]
    assert health["read"]["process_birth_identity"]["pid"] == health["read"]["pid"]
    pids = {name: int(health[name]["pid"]) for name in ("read", "action")}
    assert pids["read"] != pids["action"]
    assert all(health[name]["process_group"] == pids[name] for name in pids)
    assert all(_process_exists(pid) for pid in pids.values())
    await manager.stop()
    stopped = manager.health()
    assert stopped["read"]["process"] == "stopped"
    assert stopped["action"]["process"] == "stopped"
    assert stopped["read"]["state"] == "closed"
    assert stopped["read"]["pending_calls"] == 0
    assert all(not _process_exists(pid) for pid in pids.values())
    assert all(stopped[name]["shutdown_sequence"][0] == "STDIO_CLOSE" for name in pids)


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_retryable_stdio_process_loss_reaps_old_pid_and_rehandshakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind_hermetic_mcp_database(monkeypatch, await prepare_database(tmp_path / "reconnect.db"))
    manager = MCPManager(timeout_seconds=0.5)
    await manager.start()
    old_pid = int(manager.health()["read"]["pid"])
    try:
        os.killpg(old_pid, signal.SIGKILL)
        await asyncio.sleep(0.05)
        result = await manager.call(
            "read",
            "query_account",
            read_transport(
                {},
                tool_call_id="reconnect_tool_call",
                trace_id="reconnect_trace",
            ),
            reconnect_once=True,
        )
        assert isinstance(result, MCPCallResult) and result.attempts == 2
        assert result.lifecycle["schema_version"] == "mcp-transport-lifecycle.v1"
        assert result.lifecycle["phase_sequence"][-3:] == [
            "rehandshake",
            "call",
            "terminal",
        ]
        assert result.lifecycle["process_birth_identity"]["pid"] == result.lifecycle["pid"]
        assert result.lifecycle["arguments_hash"]
        assert result.lifecycle["configured_timeout_seconds"] == 0.5
        health = manager.health()["read"]
        new_pid = int(health["pid"])
        assert health["generation"] == 2 and health["reconnects"] == 1
        assert new_pid != old_pid and _process_exists(new_pid)
        assert not _process_exists(old_pid)
    finally:
        final_pid = manager.health()["read"]["pid"]
        await manager.stop()
        if final_pid is not None:
            assert not _process_exists(int(final_pid))


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_graph_owned_stdio_rehandshake_occurs_between_two_physical_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind_hermetic_mcp_database(monkeypatch, await prepare_database(tmp_path / "graph-reconnect.db"))
    manager = MCPManager(timeout_seconds=0.5)
    await manager.start()
    gateway = ToolGateway(manager)
    old_pid = int(manager.health()["read"]["pid"])
    try:
        os.killpg(old_pid, signal.SIGKILL)
        await asyncio.sleep(0.05)
        first = await gateway.call_read(
            ReadToolCall(name="query_account", arguments={}),
            ToolCallContext.fixture(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id="ticket_graph_reconnect",
                run_id="run_graph_reconnect",
                tool_call_id="call_graph_reconnect",
                trace_id="trace_graph_reconnect",
            ),
            allow_retry=False,
        )
        assert first.status == "unavailable"
        assert first.transport_lifecycle is not None
        assert first.transport_lifecycle["error_family"] in {"child_exit", "stdio_closed"}
        assert first.transport_lifecycle["phase_sequence"][-2:] == ["call", "terminal"]
        assert "process_birth_identity" in first.transport_lifecycle
        assert "transport_lifecycle" not in first.model_dump(mode="json")
        assert manager.health()["read"]["state"] == "degraded"

        generation = await gateway.rehandshake_read()
        second = await gateway.call_read(
            ReadToolCall(name="query_account", arguments={}),
            ToolCallContext.fixture(
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                ticket_id="ticket_graph_reconnect",
                run_id="run_graph_reconnect",
                tool_call_id="call_graph_reconnect",
                trace_id="trace_graph_reconnect",
            ),
            allow_retry=False,
        )
        health = manager.health()["read"]
        assert second.status == "ok"
        assert second.transport_lifecycle is not None
        assert second.transport_lifecycle["outcome"] == "succeeded"
        assert second.transport_lifecycle["phase_sequence"][-3:] == [
            "rehandshake",
            "call",
            "terminal",
        ]
        assert generation == 2
        assert health["generation"] == 2
        assert health["reconnects"] == 1
        assert int(health["pid"]) != old_pid
        assert not _process_exists(old_pid)
    finally:
        final_pid = manager.health()["read"]["pid"]
        await manager.stop()
        if final_pid is not None:
            assert not _process_exists(int(final_pid))


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_real_pending_call_is_cancelled_before_exact_child_pids_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind_hermetic_mcp_database(monkeypatch, await prepare_database(tmp_path / "pending.db"))
    manager = MCPManager(timeout_seconds=10)
    await manager.start()
    pids = {name: int(manager.health()[name]["pid"]) for name in ("read", "action")}
    call = asyncio.create_task(
        manager.call(
            "read",
            "search_knowledge",
            read_transport(
                {"query": "429 concurrency limit evidence"},
                tool_call_id="pending_tool_call",
                trace_id="pending_trace",
            ),
            reconnect_once=False,
        )
    )
    for _ in range(100):
        if manager.health()["read"]["pending_calls"] == 1:
            break
        await asyncio.sleep(0)
    assert manager.health()["read"]["pending_calls"] == 1
    await manager.stop()
    outcome = await asyncio.gather(call, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert manager.health()["read"]["pending_calls"] == 0
    assert all(not _process_exists(pid) for pid in pids.values())


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
