import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from supportguard.contracts.tools import ObservationEnvelope, SourceRef
from supportguard.db.base import Base
from supportguard.db.models import (
    AgentRun,
    ApiKeyMetadata,
    ApiRequestTrace,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApproverTenantScope,
    BillingRecord,
    ConversationTurn,
    Customer,
    Membership,
    MutationKillSwitch,
    PlanCatalog,
    Subscription,
    SupportTicket,
    Tenant,
    TicketMessage,
    User,
)
from supportguard.services.runtime_jobs import JobLease
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger


@pytest.fixture(autouse=True)
def v129_mcp_owned_session_registry(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    registry_raw = os.getenv("SUPPORTGUARD_V129_OWNED_SESSION_REGISTRY")
    partition_id = os.getenv("SUPPORTGUARD_V129_PARTITION_ID")
    leader_raw = os.getenv("SUPPORTGUARD_V129_PARTITION_LEADER_PID")
    if not registry_raw or not partition_id or not leader_raw:
        return
    from supportguard.evidence.mcp_test_registry import OWNER_NODE_ENV

    partition_leader = os.getpid() if leader_raw == "self" else int(leader_raw)
    if partition_leader != os.getpid():
        raise RuntimeError("mcp_registry_partition_leader_mismatch")
    monkeypatch.setenv(OWNER_NODE_ENV, request.node.nodeid)


# This local file is protected evidence from the invalid v5 diagnostic.  The
# v1.2.3 execution contract forbids collecting or executing it; keeping the
# exclusion here also makes the stable test command safe while the file is
# intentionally left untracked in the working tree.
collect_ignore = ["test_eval_v5_retrieval.py"]


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


async def seed_business_facts(session: AsyncSession) -> None:
    usage_window_end = datetime.now(UTC).replace(second=0, microsecond=0)
    session.add_all(
        [
            Tenant(id="tenant_demo", name="Demo Tenant", status="active"),
            Tenant(id="tenant_other", name="Other Tenant", status="active"),
            User(
                id="user_approver_demo",
                external_subject="oidc-approver-demo",
                display_name="Demo Approver",
            ),
            Membership(
                tenant_id="tenant_demo",
                user_id="user_approver_demo",
                role="support_approver",
                status="active",
            ),
            ApproverTenantScope(user_id="user_approver_demo", tenant_id="tenant_demo"),
            MutationKillSwitch(
                tenant_id="tenant_demo",
                action_type="refund",
                enabled=True,
                changed_by="test-fixture",
            ),
            MutationKillSwitch(
                tenant_id="tenant_demo",
                action_type="api_key_revocation",
                enabled=True,
                changed_by="test-fixture",
            ),
            MutationKillSwitch(
                tenant_id="tenant_demo",
                action_type="entitlement_change",
                enabled=True,
                changed_by="test-fixture",
            ),
            PlanCatalog(
                id="catalog_pro_eu_v1",
                plan="pro",
                region="eu-west",
                min_rpm=10,
                max_rpm=120,
                min_concurrency=2,
                max_concurrency=80,
                version=1,
            ),
            Customer(
                id="cust_demo",
                tenant_id="tenant_demo",
                display_name="Demo Customer",
                email="demo@example.test",
                status="active",
                security_status="normal",
                region="eu-west",
                version=1,
            ),
            Customer(
                id="cust_other",
                tenant_id="tenant_other",
                display_name="Other Customer",
                email="other@example.test",
                status="active",
                security_status="normal",
                region="us-east",
                version=1,
            ),
            Subscription(
                id="sub_demo",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                plan="pro",
                status="active",
                balance=Decimal("120.00"),
                currency="USD",
                rpm_limit=60,
                concurrency_limit=40,
                version=3,
            ),
            ApiUsageSnapshot(
                id="usage_demo",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                observed_at=datetime(2026, 7, 12, 2, 0, tzinfo=UTC),
                requests_last_minute=32,
                concurrency_current=40,
                remaining_balance=Decimal("120.00"),
            ),
            ApiUsageBucket(
                id="usage_bucket_demo_current",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                bucket_start=usage_window_end - timedelta(minutes=1),
                bucket_end=usage_window_end,
                request_count=32,
                input_token_count=3840,
                output_token_count=1280,
                concurrency_peak=40,
                concurrency_end=40,
                source_version=1,
            ),
            # Keep the wall-clock fixture deterministic if a long suite crosses
            # a UTC minute boundary between seeding and the read-tool call.
            ApiUsageBucket(
                id="usage_bucket_demo_next",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                bucket_start=usage_window_end,
                bucket_end=usage_window_end + timedelta(minutes=1),
                request_count=32,
                input_token_count=3840,
                output_token_count=1280,
                concurrency_peak=40,
                concurrency_end=40,
                source_version=1,
            ),
            ApiRequestTrace(
                id="trace_demo_429",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                request_id="req_demo_429",
                model="atlas-chat",
                region="eu-west",
                status_code=429,
                error_class="concurrency_limit_exceeded",
                stage_latency_ms={"connect": 20, "queue": 900, "total": 1100},
                observed_at=datetime(2026, 7, 12, 2, 0, tzinfo=UTC),
                version=1,
            ),
            ApiKeyMetadata(
                id="keymeta_demo",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                key_id="key_demo_leaked",
                fingerprint="fp_demo_leaked",
                status="active",
                version=2,
                last_used_summary={"region": "eu-west", "request_count": 8},
            ),
            BillingRecord(
                id="bill_original",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                amount=Decimal("49.00"),
                currency="USD",
                status="charged",
                duplicate_of=None,
                version=1,
            ),
            BillingRecord(
                id="bill_duplicate",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                amount=Decimal("49.00"),
                currency="USD",
                status="charged",
                duplicate_of="bill_original",
                version=2,
            ),
            BillingRecord(
                id="bill_other",
                tenant_id="tenant_other",
                customer_id="cust_other",
                amount=Decimal("19.00"),
                currency="USD",
                status="charged",
                duplicate_of=None,
                version=1,
            ),
            SupportTicket(
                id="ticket_demo",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="open",
                issue_type="billing",
                risk="low",
                version=1,
            ),
            SupportTicket(
                id="ticket_other",
                tenant_id="tenant_other",
                customer_id="cust_other",
                status="open",
                issue_type="billing",
                risk="low",
                version=1,
            ),
        ]
    )
    await session.flush()
    message = TicketMessage(
        id="message_demo",
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        role="user",
        message_kind="customer",
        conversation_sequence=1,
        content="Seeded test request",
    )
    session.add(message)
    await session.flush()
    run = AgentRun(
        id="run_demo",
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        message_id=message.id,
        status="interrupted",
        checkpoint_stage="awaiting_approval",
        checkpoint_id="checkpoint_demo",
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
        id="turn_demo",
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_message_id=message.id,
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
    message.turn_id = turn.id
    ticket = await session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.next_message_sequence = 1
    await session.flush()


async def seed_closed_refund_observation_binding(
    session: AsyncSession,
    lease: JobLease,
    *,
    segment_id: str,
    billing_record_id: str = "bill_duplicate",
    billing_version: int = 2,
    business_tool: str = "query_billing_record",
    resource_field: str = "billing_record_id",
    policy_source_id: str = "refund-policy:c1",
) -> list[dict[str, object]]:
    """Build the same durable closed-ledger binding required by production proposals."""

    ledger = ToolLedger(session)
    turn, invocations = await ledger.open_turn(
        lease,
        segment_id=segment_id,
        tool_round=1,
        decision={"decision_type": "tool_calls"},
        context_manifest={"fixture": "closed_refund_binding"},
        calls=[
            InvocationSpec("business_call", business_tool, {}, 0),
            InvocationSpec("knowledge_call", "search_knowledge", {}, 1),
        ],
    )
    bindings: list[dict[str, object]] = []
    for invocation in invocations:
        await ledger.mark_executing(lease, invocation.id)
        if invocation.tool_name == business_tool:
            data = {resource_field: billing_record_id, "version": billing_version}
            source_id = f"business_record:{billing_record_id}"
        else:
            data = {"evidence": [{"evidence_id": policy_source_id}]}
            source_id = policy_source_id
        observation = ObservationEnvelope(
            tool_name=invocation.tool_name,
            tool_call_id=invocation.provider_tool_call_id,
            ticket_id="ticket_demo",
            run_id=lease.run_id,
            attempt_index=1,
            status="ok",
            retryable=False,
            observed_at=datetime.now(UTC),
            duration_ms=1,
            source_refs=[
                SourceRef(
                    source_type=(
                        "business_record"
                        if invocation.tool_name == business_tool
                        else "knowledge_chunk"
                    ),
                    source_id=source_id,
                    observed_at=datetime.now(UTC),
                )
            ],
            data=data,
        )
        stored = await ledger.terminalize(
            lease, invocation.id, outcome="succeeded", observation=observation
        )
        binding: dict[str, object] = {
            "tool_name": invocation.tool_name,
            "tool_call_id": invocation.provider_tool_call_id,
            "invocation_id": invocation.id,
            "observation_id": stored.id,
            "observation_content_hash": stored.content_hash,
            "turn_group_id": turn.id,
            "status": "ok",
            "source_refs": [{"source_id": source_id}],
        }
        if invocation.tool_name == business_tool:
            binding.update(
                {
                    "resource_field": resource_field,
                    "resource_id": billing_record_id,
                    "resource_version": billing_version,
                }
            )
        bindings.append(binding)
    await ledger.close_turn(lease, turn.id)
    await session.flush()
    return bindings
