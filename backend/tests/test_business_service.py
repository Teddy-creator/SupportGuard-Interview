from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import (
    ApiKeyMetadataInput,
    EscalationInput,
    RefundProposalInput,
    RequestTraceInput,
    ToolCallContext,
    UsageInput,
)
from supportguard.db.models import (
    AgentRun,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApprovalRequest,
    AuditEvent,
    EscalationRecord,
    ProposalRecord,
    RuntimeJob,
    Subscription,
    SupportTicket,
)
from supportguard.services.business import (
    BusinessService,
    _mcp_boundary_reason,
    _usage_bucket_complete,
)
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.tools.gateway import ToolGateway

TEST_CAPABILITY = issue_test_runtime_capability(testing=True)


def test_mcp_boundary_reason_is_bounded_and_does_not_retain_raw_database_text() -> None:
    assert (
        _mcp_boundary_reason(
            "55000",
            "ERROR: search_terminal_order_invalid DETAIL: private database diagnostics",
        )
        == "search_terminal_order_invalid"
    )
    assert _mcp_boundary_reason("P0002", "untrusted missing-row detail") == (
        "scoped_resource_missing"
    )
    assert _mcp_boundary_reason("55000", "untrusted state detail") == "state_rejected"
    assert _mcp_boundary_reason("XX000", "untrusted internal detail") == (
        "database_boundary_failed"
    )

    public_observation = ToolGateway._domain_failure(
        "search_knowledge",
        context(),
        0.0,
        {
            "status": "conflict",
            "error_code": "ticket_state_conflict",
            "safe_error_summary": "The capability rejected inconsistent input.",
            "internal_boundary_reason": "search_terminal_order_invalid",
        },
    )
    assert "internal_boundary_reason" not in public_observation.model_dump(mode="json")


def fixture_service(session: AsyncSession) -> BusinessService:
    return BusinessService(session, test_capability=TEST_CAPABILITY)


def context(*, customer_id: str = "cust_demo", ticket_id: str = "ticket_demo") -> ToolCallContext:
    return ToolCallContext.fixture(
        tenant_id="tenant_demo",
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id="run_demo",
        checkpoint_id="checkpoint_demo",
        tool_call_id="tool_call_001",
        trace_id="trace_001",
    )


@pytest.mark.asyncio
async def test_read_tools_return_scoped_business_facts(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    service = fixture_service(db_session)

    account = await service.query_account(context())
    usage = await service.query_api_usage(context(), UsageInput(window="1m"))

    assert account.account_status == "active"
    assert account.security_status == "normal"
    assert account.region == "eu-west"
    assert set(account.model_dump()) == {
        "tool_call_id",
        "ticket_id",
        "source_refs",
        "account_status",
        "security_status",
        "region",
        "observed_at",
        "resource_version",
    }
    assert usage.concurrency_current == 40
    assert usage.request_count == 32
    assert usage.remaining_balance == Decimal("120.00")
    assert all(ref.source_type == "business_record" for ref in account.source_refs)

    subscription = await service.query_subscription(context())
    trace = await service.query_request_trace(
        context(), RequestTraceInput(request_id="req_demo_429")
    )
    key = await service.query_api_key_metadata(
        context(), ApiKeyMetadataInput(api_key_ref="key_demo_leaked")
    )
    assert subscription.version == 3 and subscription.concurrency_limit == 40
    assert (
        trace.error_class == "concurrency_limit_exceeded" and trace.stage_latency_ms["queue"] == 900
    )
    assert key.fingerprint == "fp_demo_leaked" and "secret" not in key.model_dump()


@pytest.mark.asyncio
async def test_usage_windows_aggregate_distinct_real_buckets(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_business_facts(db_session)
    logical_time = datetime(2026, 7, 14, 12, 0, 37, tzinfo=UTC)
    window_end = logical_time.replace(second=0, microsecond=0)
    monkeypatch.setattr("supportguard.services.business.utc_now", lambda: logical_time)
    await db_session.execute(delete(ApiUsageBucket))
    snapshot = await db_session.get(ApiUsageSnapshot, "usage_demo")
    subscription = await db_session.get(Subscription, "sub_demo")
    assert snapshot is not None and subscription is not None
    snapshot.observed_at = window_end
    # A current authoritative read must not confuse the resource's last
    # mutation time with the observation time of the read itself.
    subscription.updated_at = window_end - timedelta(days=30)
    db_session.add_all(
        [
            ApiUsageBucket(
                id=f"usage_window_{minute:04d}",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                bucket_start=window_end - timedelta(minutes=minute),
                bucket_end=window_end - timedelta(minutes=minute - 1),
                request_count=1,
                input_token_count=10,
                output_token_count=5,
                concurrency_peak=(minute % 17) + 1,
                concurrency_end=minute % 11,
                source_version=1,
            )
            for minute in range(1, 1441)
        ]
    )
    await db_session.flush()

    service = fixture_service(db_session)
    results = {
        window: await service.query_api_usage(context(), UsageInput(window=window))
        for window in ("1m", "5m", "1h", "24h")
    }

    assert {window: result.request_count for window, result in results.items()} == {
        "1m": 1,
        "5m": 5,
        "1h": 60,
        "24h": 1440,
    }
    assert all(result.window_end == window_end for result in results.values())
    assert all(result.freshness_seconds == 37 for result in results.values())
    assert all(result.freshness_status == "fresh" for result in results.values())
    assert len({result.resource_version for result in results.values()}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["gap", "overlap"])
async def test_usage_reference_path_marks_non_contiguous_buckets_unknown(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    await seed_business_facts(db_session)
    logical_time = datetime(2026, 7, 14, 12, 0, 37, tzinfo=UTC)
    window_end = logical_time.replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=1)
    monkeypatch.setattr("supportguard.services.business.utc_now", lambda: logical_time)
    await db_session.execute(delete(ApiUsageBucket))
    snapshot = await db_session.get(ApiUsageSnapshot, "usage_demo")
    subscription = await db_session.get(Subscription, "sub_demo")
    assert snapshot is not None and subscription is not None
    snapshot.observed_at = window_end
    subscription.updated_at = window_end
    bounds = {
        "gap": [(window_start - timedelta(minutes=1), window_start)],
        "overlap": [(window_start - timedelta(seconds=30), window_end)],
    }[shape]
    db_session.add_all(
        [
            ApiUsageBucket(
                id=f"usage_{shape}_{index}",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                bucket_start=start,
                bucket_end=end,
                request_count=1,
                input_token_count=10,
                output_token_count=5,
                concurrency_peak=1,
                concurrency_end=1,
                source_version=1,
            )
            for index, (start, end) in enumerate(bounds)
        ]
    )
    await db_session.flush()

    result = await fixture_service(db_session).query_api_usage(context(), UsageInput(window="1m"))

    assert result.freshness_status == "unknown"
    assert len(result.resource_version) == 64


def test_usage_reference_path_rejects_duplicate_bucket_bounds() -> None:
    window_end = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    window_start = window_end - timedelta(minutes=2)
    duplicate = (window_start, window_start + timedelta(minutes=1))

    assert not _usage_bucket_complete(
        [duplicate, duplicate],
        window_start=window_start,
        window_end=window_end,
        expected_count=2,
    )


@pytest.mark.asyncio
async def test_escalation_is_scoped_and_idempotent(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    service = fixture_service(db_session)
    arguments = EscalationInput(
        reason="Evidence remains conflicting after bounded checks.",
        idempotency_key="escalation-key-001",
    )

    first = await service.create_support_escalation(context(), arguments)
    second = await service.create_support_escalation(context(), arguments)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    count = await db_session.scalar(select(func.count()).select_from(EscalationRecord))

    assert first.escalation_id == second.escalation_id
    assert count == 1
    assert ticket is not None and ticket.status == "manual_takeover"


@pytest.mark.asyncio
async def test_refund_proposal_derives_money_and_waits_for_approval(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    service = fixture_service(db_session)
    arguments = RefundProposalInput(
        billing_record_id="bill_duplicate",
        refund_reason="Customer reported an explicit duplicate charge.",
        idempotency_key="refund-proposal-001",
    )

    first = await service.propose_refund(context(), arguments)
    second = await service.propose_refund(context(), arguments)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    approval_count = await db_session.scalar(select(func.count()).select_from(ApprovalRequest))
    proposal_count = await db_session.scalar(select(func.count()).select_from(ProposalRecord))
    audit_count = await db_session.scalar(select(func.count()).select_from(AuditEvent))

    assert first.approval_id == second.approval_id
    assert first.proposal_id == second.proposal_id
    assert first.amount == Decimal("49.00")
    assert first.currency == "USD"
    assert first.status == "pending"
    assert approval_count == 1
    assert proposal_count == 1
    assert audit_count == 1
    assert ticket is not None and ticket.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_refund_proposal_rejects_cross_customer_billing_record(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    service = fixture_service(db_session)

    with pytest.raises(DomainError) as error:
        await service.propose_refund(
            context(),
            RefundProposalInput(
                billing_record_id="bill_other",
                refund_reason="Try a record outside the current customer scope.",
                idempotency_key="refund-proposal-002",
            ),
        )

    assert error.value.code is ErrorCode.BILLING_SCOPE_VIOLATION


@pytest.mark.asyncio
async def test_action_tool_rejects_cross_customer_ticket(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    service = fixture_service(db_session)

    with pytest.raises(DomainError) as error:
        await service.create_support_escalation(
            context(ticket_id="ticket_other"),
            EscalationInput(
                reason="Attempt to operate on another customer's ticket.",
                idempotency_key="escalation-key-002",
            ),
        )

    assert error.value.code is ErrorCode.TICKET_SCOPE_VIOLATION


@pytest.mark.asyncio
async def test_read_mcp_fence_rejects_a_late_worker(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    job = RuntimeJob(
        id="test-fixture-job",
        tenant_id="tenant_demo",
        ticket_id=run.ticket_id,
        run_id=run.id,
        dispatch_sequence=1,
        kind="agent_start",
        status="leased",
        attempt=1,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        heartbeat_at=datetime.now(UTC),
        fencing_token=1,
    )
    run.active_job_id = job.id
    run.active_fencing_token = 1
    db_session.add(job)
    await db_session.flush()

    await BusinessService(db_session).assert_fenced_context(context())
    job.fencing_token = 2
    run.active_fencing_token = 2
    await db_session.flush()
    with pytest.raises(DomainError, match="Stale MCP fence"):
        await BusinessService(db_session).assert_fenced_context(context())


def test_model_visible_action_arguments_cannot_set_customer_scope() -> None:
    assert "customer_id" not in EscalationInput.model_fields
    assert "customer_id" not in RefundProposalInput.model_fields
    assert "amount" not in RefundProposalInput.model_fields
    assert "currency" not in RefundProposalInput.model_fields
