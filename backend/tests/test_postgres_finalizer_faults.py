from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from conftest import seed_closed_refund_observation_binding
from current_predicate_facts import record_predicate_operands
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.persistence import AgentRunStore, verify_ticket_event_chain
from supportguard.agent.schemas import CandidateResponse, ProposalEligibility
from supportguard.approvals.service import RefundRuntime
from supportguard.contracts.context import (
    RequestContext,
    WorkerExecutionContext,
    worker_execution_context,
)
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    ApproverTenantScope,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    HumanDecision,
    InboxDelivery,
    MutationKillSwitch,
    OutboxEvent,
    ProposalRecord,
    RuntimeJob,
    Subscription,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.policies.gate import PolicyRoute
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.services.actions import (
    RuntimeActionExecutor,
    execute_runtime_action_capability,
    validate_execution_binding,
)
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.approver_scope import assert_active_approver_scope
from supportguard.services.business import action_hash
from supportguard.services.commands import CommandCoordinator
from supportguard.services.refunds import (
    evaluate_billing_refund_pair,
)
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from supportguard.tools.gateway import ToolGateway

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


async def _finalize_as_worker(
    url: str,
    lease: JobLease,
    marker_id: str,
    *,
    customer_id: str,
    ticket_id: str,
    run_id: str,
    trace_id: str,
) -> None:
    """Exercise Finalizer through the same trusted identity used in production."""

    context = WorkerExecutionContext(
        tenant_id=lease.tenant_id,
        actor_principal_id=customer_id,
        executor_service_principal=lease.owner,
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        job_id=lease.job_id,
        segment_id=marker_id,
        delivery_generation=1,
        fencing_token=lease.fencing_token,
        trace_id=trace_id,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    factory = create_scoped_session_factory(worker)
    try:
        with worker_execution_context.bind(context):
            async with factory.worker(context) as session:
                await SegmentRepository(session).finalize(lease, marker_id=marker_id)
                await session.commit()
    finally:
        await worker.dispose()


async def _seed_run(
    factory: async_sessionmaker[AsyncSession],
    prefix: str,
    *,
    customer_id: str = "cust_demo",
) -> str:
    ticket_id = f"ticket_{prefix}"
    message_id = f"message_{prefix}"
    run_id = f"run_{prefix}"
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        ticket = SupportTicket(
            id=ticket_id,
            tenant_id="tenant_demo",
            customer_id=customer_id,
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
                content="finalizer fault fixture",
            )
        )
        await session.flush()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id=customer_id,
                message_id=message_id,
                status="queued",
                model="fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="fault.v1",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
    return run_id


async def _seed_pending_approval(
    factory: async_sessionmaker[AsyncSession],
    prefix: str,
    *,
    action_type: str = "refund",
) -> tuple[str, str]:
    async with factory() as session, session.begin():
        await session.execute(
            update(RuntimeJob)
            .where(RuntimeJob.status.in_({"queued", "retry_wait", "leased"}))
            .values(created_at=datetime.now(UTC), available_at=datetime.now(UTC))
        )
    customer_id = "cust_demo"
    run_id = await _seed_run(factory, prefix, customer_id=customer_id)
    proposal_id = f"proposal_{prefix}"
    resource_id = f"bill_{prefix}"
    resource_version = 2
    business_tool = "query_billing_record"
    resource_field = "billing_record_id"
    payload: dict[str, object] = {
        "billing_record_id": resource_id,
        "customer_id": customer_id,
        "amount": "49.00",
        "currency": "USD",
        "refund_reason": "Duplicate charge verified by billing lineage.",
        "business_version": resource_version,
    }
    if action_type == "api_key_revocation":
        resource_id = f"key_{prefix}"
        business_tool = "query_api_key_metadata"
        resource_field = "api_key_id"
        payload = {
            "api_key_id": resource_id,
            "customer_id": customer_id,
            "fingerprint": f"fp_{prefix}",
            "reason": "Customer reported credential exposure.",
            "business_version": resource_version,
        }
    elif action_type == "entitlement_change":
        resource_id = "sub_demo"
        async with factory() as session, session.begin():
            current_subscription = await session.scalar(
                select(Subscription).where(Subscription.id == resource_id).with_for_update()
            )
            assert current_subscription is not None
            prior_action_version = await session.scalar(
                select(func.max(BusinessAction.resource_version)).where(
                    BusinessAction.tenant_id == "tenant_demo",
                    BusinessAction.action_type == "entitlement_change",
                    BusinessAction.resource_id == resource_id,
                )
            )
            resource_version = max(
                current_subscription.version,
                int(prior_action_version or 0) + 1,
            )
            current_subscription.version = resource_version
            current_subscription.concurrency_limit = 40
        target_concurrency = 60
        business_tool = "query_subscription"
        resource_field = "subscription_id"
        payload = {
            "subscription_id": resource_id,
            "customer_id": customer_id,
            "change_type": "quota_change",
            "target": {"concurrency_limit": target_concurrency},
            "reason": "Explicit target is within the active Plan Catalog.",
            "business_version": resource_version,
        }
    async with factory() as session, session.begin():
        if action_type == "refund":
            charged_at = datetime.now(UTC) - timedelta(days=1)
            period_start = date(2026, 8, 1)
            period_end = date(2026, 9, 1)
            original_id = f"{resource_id}_original"
            session.add(
                BillingRecord(
                    id=original_id,
                    tenant_id="tenant_demo",
                    customer_id=customer_id,
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    charged_at=charged_at,
                    service_period_start=period_start,
                    service_period_end=period_end,
                    duplicate_of=None,
                    version=1,
                )
            )
            await session.flush()
            session.add(
                BillingRecord(
                    id=resource_id,
                    tenant_id="tenant_demo",
                    customer_id=customer_id,
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    charged_at=charged_at,
                    service_period_start=period_start,
                    service_period_end=period_end,
                    duplicate_of=original_id,
                    version=resource_version,
                )
            )
            await session.flush()
            billing = await session.get(BillingRecord, resource_id)
            assert billing is not None
            pair = await evaluate_billing_refund_pair(
                session,
                billing,
                now=datetime.now(UTC),
            )
            assert pair.eligible and pair.original is not None and pair.pair_hash
        elif action_type == "api_key_revocation":
            session.add(
                ApiKeyMetadata(
                    id=f"keymeta_{prefix}",
                    tenant_id="tenant_demo",
                    customer_id=customer_id,
                    key_id=resource_id,
                    fingerprint=f"fp_{prefix}",
                    status="active",
                    version=resource_version,
                    last_used_summary={},
                )
            )
        else:
            assert await session.get(Subscription, resource_id) is not None
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner=f"worker-{prefix}")
        observation_binding = await seed_closed_refund_observation_binding(
            session,
            lease,
            segment_id=f"segment_{prefix}",
            billing_record_id=resource_id,
            billing_version=resource_version,
            business_tool=business_tool,
            resource_field=resource_field,
            policy_source_id=f"{action_type}-policy:c1",
        )
        session.add(
            ProposalRecord(
                id=proposal_id,
                tenant_id="tenant_demo",
                run_id=run_id,
                proposal_identity=f"identity:{prefix}",
                action_type=action_type,
                resource_id=resource_id,
                resource_version=resource_version,
                action_payload=payload,
                observation_binding=observation_binding,
                action_hash=action_hash(payload),
                status="draft",
            )
        )
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"proposal_id": proposal_id},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="c" * 64,
            outcome="interrupted",
            state={"segment_events": []},
            proposal_id=proposal_id,
        )
        marker_id = marker.id
    raw_url = _database_url()
    assert raw_url is not None
    worker = create_async_engine(
        make_url(raw_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        async with worker_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            approval = await SegmentRepository(session).finalize_interrupt(
                lease,
                marker_id=marker_id,
                proposal_id=proposal_id,
                test_capability=issue_test_runtime_capability(testing=True),
            )
            return approval.id, approval.ticket_id
    finally:
        await worker.dispose()


def _approver_scope(prefix: str) -> RequestContext:
    return RequestContext(
        tenant_id="tenant_demo",
        authenticated_actor_id="user_approver_demo",
        authenticated_actor_role="support_approver",
        subject_customer_id=None,
        request_id=f"request-{prefix}",
        trace_id=f"trace-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


async def _checkpoint_action_resume_intent(
    session: AsyncSession,
    *,
    approval: ApprovalRequest,
    lease: JobLease,
    label: str,
    validated_answer: str,
) -> str:
    decision = await session.scalar(
        select(HumanDecision).where(
            HumanDecision.tenant_id == approval.tenant_id,
            HumanDecision.approval_id == approval.id,
        )
    )
    assert decision is not None
    snapshot = await session.scalar(
        select(ApprovalSnapshot).where(
            ApprovalSnapshot.tenant_id == approval.tenant_id,
            ApprovalSnapshot.approval_id == approval.id,
        )
    )
    assert snapshot is not None and snapshot.citation_binding_refs
    claims = [
        {
            "text": validated_answer,
            "knowledge_locator_hashes": [
                str(item["locator_hash"]) for item in snapshot.policy_binding["citation_lineage"]
            ],
            "citation_binding_ids": [str(item) for item in snapshot.citation_binding_refs],
            "observation_source_ids": [f"business_record:{approval.resource_id}"],
        }
    ]
    marker = await SegmentRepository(session).prepare(
        lease,
        delivery_generation=1,
        segment_kind="approval_resume",
        segment_input={
            "approval_id": approval.id,
            "decision_id": decision.id,
        },
    )
    await SegmentRepository(session).checkpoint_written(
        lease,
        marker_id=marker.id,
        checkpoint_id=f"checkpoint-action-resume-{label}",
        checkpoint_hash="c" * 64,
        outcome="completed",
        state={
            "ticket_id": approval.ticket_id,
            "customer_id": approval.customer_id,
            "run_id": approval.run_id,
            "trace_id": f"trace-action-resume-{label}",
            "classification": {
                "issue_type": {
                    "refund": "billing_refund",
                    "api_key_revocation": "credential_security",
                    "entitlement_change": "entitlement_change",
                }[approval.action_type],
                "risk": "high",
            },
            "agent_finish_reason": "proposed",
            "human_decision": {
                "approval_id": approval.id,
                "action": decision.decision,
            },
            "execution_result": {
                "approval_id": approval.id,
                "action_type": approval.action_type,
                "resource_id": approval.resource_id,
                "action_hash": approval.action_hash,
                "idempotency_key": approval.idempotency_key,
                "status": "execution_pending",
                "execution_intent": "execute_runtime_action",
                "expected_approval_status": "approved",
            },
            "tool_observations": [],
            "evidence": [],
            "segment_events": [],
            "validated_answer": validated_answer,
            "final": {
                "answer": "The approved effect is pending.",
                "terminal_state": "verification_pending",
                "knowledge_chunk_ids": [],
                "business_source_ids": [],
                "material_claims": claims,
                "policy_route": "await_approval",
            },
        },
        approval_id=approval.id,
    )
    return marker.id


@pytest.mark.parametrize(
    "action_type",
    ["refund", "api_key_revocation", "entitlement_change"],
)
async def test_all_high_risk_interrupt_finalizers_publish_one_pending_approval(
    action_type: str,
) -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"interrupt_{action_type}_{uuid4().hex[:10]}"

    approval_id, ticket_id = await _seed_pending_approval(
        factory,
        prefix,
        action_type=action_type,
    )

    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None
        proposal = await session.get(ProposalRecord, approval.proposal_id)
        marker = await session.get(CheckpointCommitMarker, approval.marker_id)
        approval_count = int(
            await session.scalar(
                select(func.count(ApprovalRequest.id)).where(
                    ApprovalRequest.proposal_id == approval.proposal_id
                )
            )
            or 0
        )
        assert approval.ticket_id == ticket_id
        assert approval.action_type == action_type
        assert approval.status == "pending"
        assert proposal is not None and proposal.status == "bound"
        assert marker is not None and marker.status == "finalized"
        assert approval_count == 1

    await engine.dispose()


async def test_all_api_job_entrypoints_wait_on_one_active_timing_row() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"admission_all_{uuid4().hex[:12]}"
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    approval_id, _ = await _seed_pending_approval(admin_factory, f"{prefix}_approval")
    message_ticket_id = f"ticket_{prefix}_message"
    async with admin_factory() as session, session.begin():
        session.add(
            SupportTicket(
                id=message_ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="open",
            )
        )
    api_url = (
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api = create_async_engine(api_url)
    api_factory = create_scoped_session_factory(api)

    def customer_scope(label: str) -> RequestContext:
        return RequestContext(
            tenant_id="tenant_demo",
            authenticated_actor_id="cust_demo",
            authenticated_actor_role="customer_member",
            subject_customer_id="cust_demo",
            request_id=f"request-{prefix}-{label}",
            trace_id=f"trace-{prefix}-{label}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )

    async def submit_ticket():  # type: ignore[no-untyped-def]
        async with api_factory.request(customer_scope("ticket")) as session:
            accepted = await CommandCoordinator(session).accept_new_ticket(
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=f"ticket-{prefix}",
                message=f"Admission serialization ticket {prefix}",
                trace_id=f"trace-{prefix}-ticket",
            )
            await session.commit()
            return accepted

    async def submit_message():  # type: ignore[no-untyped-def]
        async with api_factory.request(customer_scope("message")) as session:
            accepted = await CommandCoordinator(session).accept_message(
                ticket_id=message_ticket_id,
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=f"message-{prefix}",
                message=f"Admission serialization message {prefix}",
                trace_id=f"trace-{prefix}-message",
            )
            await session.commit()
            return accepted

    async def submit_approval():  # type: ignore[no-untyped-def]
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"approval-{prefix}",
                reason="Admission serialization evidence verified.",
                approver_note="All entrypoint timing-row lock fixture.",
                trace_id=f"trace-{prefix}-approval",
            )
            await session.commit()
            return accepted

    lock_connection = await admin.connect()
    transaction = await lock_connection.begin()
    tasks: list[asyncio.Task[object]] = []
    try:
        active_timing_version = int(
            await lock_connection.scalar(
                text(
                    "SELECT timing_version FROM supportguard_control.runtime_timing_snapshots "
                    "WHERE is_active FOR UPDATE"
                )
            )
            or 0
        )
        tasks = [
            asyncio.create_task(submit_ticket()),
            asyncio.create_task(submit_message()),
            asyncio.create_task(submit_approval()),
        ]
        waiting_rows: list[object] = []
        for _ in range(100):
            async with admin.connect() as observer:
                waiting_rows = list(
                    (
                        await observer.execute(
                            text(
                                "SELECT query,wait_event_type,wait_event FROM pg_stat_activity "
                                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                                "AND state='active' AND ("
                                "query LIKE '%supportguard_api_accept_ticket%' OR "
                                "query LIKE '%supportguard_api_accept_conversation_message%' OR "
                                "query LIKE "
                                "'%supportguard_api_accept_conversation_approval_decision%')"
                            )
                        )
                    ).all()
                )
            if len(waiting_rows) == 3 and all(row[1] == "Lock" for row in waiting_rows):
                break
            await asyncio.sleep(0.02)
        task_done_while_timing_locked = [task.done() for task in tasks]
        assert len(waiting_rows) == 3
        assert all(row[1] == "Lock" for row in waiting_rows)
        assert task_done_while_timing_locked == [False, False, False]
        await transaction.commit()
        accepted = await asyncio.gather(*tasks)
        accepted_job_ids = [str(item.job_id) for item in accepted]  # type: ignore[attr-defined]
        async with admin.connect() as connection:
            job_timing_versions = sorted(
                int(value)
                for value in await connection.scalars(
                    select(RuntimeJob.timing_version).where(RuntimeJob.id.in_(accepted_job_ids))
                )
            )
            definitions = list(
                await connection.scalars(
                    text(
                        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.proname=ANY(:names) "
                        "ORDER BY p.proname"
                    ),
                    {
                        "names": [
                            "supportguard_api_accept_ticket",
                            "supportguard_api_accept_conversation_message",
                            "supportguard_api_accept_conversation_approval_decision",
                        ]
                    },
                )
            )
        assert len(set(accepted_job_ids)) == 3
        assert job_timing_versions == [active_timing_version] * 3
        assert len(definitions) == 3
        assert all("WHERE is_active FOR UPDATE" in definition for definition in definitions)
        record_predicate_operands(
            requirement_id="C6-P0-07",
            predicate_id="admission_serialization_all_entrypoints",
            subject_kind="postgres_api_admission_entrypoint_lock_matrix",
            operands={
                "entrypoint_names": sorted(
                    [
                        "supportguard_api_accept_ticket",
                        "supportguard_api_accept_conversation_message",
                        "supportguard_api_accept_conversation_approval_decision",
                    ]
                ),
                "active_timing_version": active_timing_version,
                "waiting_query_count": len(waiting_rows),
                "waiting_event_types": sorted(str(row[1]) for row in waiting_rows),
                "waiting_events": sorted(str(row[2]) for row in waiting_rows),
                "task_done_while_timing_locked": task_done_while_timing_locked,
                "accepted_job_ids": sorted(accepted_job_ids),
                "distinct_accepted_job_count": len(set(accepted_job_ids)),
                "job_timing_versions": job_timing_versions,
                "expected_job_timing_versions": [active_timing_version] * 3,
                "definition_count": len(definitions),
                "timing_row_lock_clause_count": sum(
                    definition.count("WHERE is_active FOR UPDATE") for definition in definitions
                ),
            },
        )
    finally:
        if transaction.is_active:
            await transaction.rollback()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await lock_connection.close()
        await api.dispose()
        await admin.dispose()


async def test_v126_api_approval_capability_serializes_duplicate_decisions() -> None:
    from test_v1512_runtime_action_binding_postgres import (
        _seed_production_shaped_pending_approval_fixture,
    )

    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"api_approval_{uuid4().hex[:12]}"
    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        admin_factory,
        prefix,
        action_type="refund",
    )
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    key = f"decision-{prefix}"

    async def submit(trace_id: str):
        async with api_factory.request(_approver_scope(trace_id)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=key,
                reason="Evidence and immutable action revision verified.",
                approver_note="Concurrent approval fixture",
                trace_id=trace_id,
            )
            await session.commit()
            return accepted

    first, second = await asyncio.gather(submit("decision-a"), submit("decision-b"))
    assert first.job_id == second.job_id
    assert sorted([first.reused, second.reused]) == [False, True]
    async with admin_factory() as session, session.begin():
        assert (
            await session.scalar(
                select(func.count(HumanDecision.id)).where(HumanDecision.approval_id == approval_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(RuntimeJob.id)).where(
                    RuntimeJob.approval_id == approval_id,
                    RuntimeJob.kind == "approval_resume",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(OutboxEvent.job_id == first.job_id)
            )
            == 1
        )
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.run_id is not None
        lease = await RuntimeJobRepository(session).claim(
            job_id=first.job_id,
            owner=f"worker-{prefix}",
        )
        await _checkpoint_action_resume_intent(
            session,
            approval=approval,
            lease=lease,
            label=prefix,
            validated_answer=validated_answer,
        )
        run_id = approval.run_id
        customer_id = approval.customer_id
        approval_idempotency_key = approval.idempotency_key
        billing_record_id = str(approval.action_payload["billing_record_id"])

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)

    async def prepare_action(action_type: str, scenario: str):
        action_prefix = f"{scenario}_{action_type}_{uuid4().hex[:10]}"
        (
            prepared_approval_id,
            prepared_ticket_id,
            prepared_validated_answer,
        ) = await _seed_production_shaped_pending_approval_fixture(
            admin_factory,
            action_prefix,
            action_type=action_type,
        )
        async with api_factory.request(_approver_scope(action_prefix)) as session:
            acceptance = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=prepared_approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{action_prefix}",
                reason=f"{scenario} action capability fixture.",
                approver_note=f"Exercise {action_type} under {scenario}.",
                trace_id=f"trace-{action_prefix}",
            )
            await session.commit()
        async with admin_factory() as session, session.begin():
            prepared_approval = await session.get(ApprovalRequest, prepared_approval_id)
            assert prepared_approval is not None and prepared_approval.run_id is not None
            prepared_lease = await RuntimeJobRepository(session).claim(
                job_id=acceptance.job_id,
                owner=f"worker-{action_prefix}",
            )
            await _checkpoint_action_resume_intent(
                session,
                approval=prepared_approval,
                lease=prepared_lease,
                label=action_prefix,
                validated_answer=prepared_validated_answer,
            )
            prepared_resource_id = str(
                prepared_approval.action_payload[
                    {
                        "refund": "billing_record_id",
                        "api_key_revocation": "api_key_id",
                        "entitlement_change": "subscription_id",
                    }[action_type]
                ]
            )
            prepared_customer_id = prepared_approval.customer_id
            prepared_run_id = prepared_approval.run_id
        prepared_context = WorkerExecutionContext(
            tenant_id="tenant_demo",
            actor_principal_id=prepared_customer_id,
            executor_service_principal="supportguard_worker",
            customer_id=prepared_customer_id,
            ticket_id=prepared_ticket_id,
            run_id=prepared_run_id,
            job_id=prepared_lease.job_id,
            segment_id=f"segment-{action_prefix}",
            delivery_generation=1,
            fencing_token=prepared_lease.fencing_token,
            trace_id=f"trace-execute-{action_prefix}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        return (
            prepared_approval_id,
            prepared_ticket_id,
            prepared_resource_id,
            prepared_lease,
            prepared_context,
        )

    async def retire_entitlement_fixture(
        action_type: str,
        *,
        approval_id: str,
        lease: JobLease,
        context: WorkerExecutionContext,
    ) -> None:
        """End a deliberately unexecuted shared-Subscription fixture safely."""

        if action_type != "entitlement_change":
            return
        async with admin_factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.resource_id == "sub_demo"
            subscription = await session.get(
                Subscription,
                approval.resource_id,
                with_for_update=True,
            )
            assert subscription is not None
            subscription.version += 1
        async with worker_factory.worker(context) as session:
            retired = await RuntimeActionExecutor(session).execute(
                lease,
                approval_id=approval_id,
            )
            await session.commit()
        assert retired.status == "stale" and retired.business_action_id is None

    worker_context = WorkerExecutionContext(
        tenant_id="tenant_demo",
        actor_principal_id=customer_id,
        executor_service_principal="supportguard_worker",
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        job_id=lease.job_id,
        segment_id=f"segment-{prefix}",
        delivery_generation=1,
        fencing_token=lease.fencing_token,
        trace_id=f"trace-execute-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    async with worker_factory.worker(worker_context) as session:
        effect = await RefundRuntime(session).execute_refund(
            approval_id,
            idempotency_key=approval_idempotency_key,
            trace_id=worker_context.trace_id,
            lease=lease,
        )
        await session.commit()
    assert effect.status == "succeeded" and effect.reused is False, (
        effect.status,
        effect.reason,
    )

    async with admin_factory() as session:
        binding = (
            (
                await session.execute(
                    text(
                        "SELECT b.approval_id IS NOT NULL AS has_approval,"
                        "b.human_decision_id IS NOT NULL AS has_decision,"
                        "b.action_revision_id IS NOT NULL AS has_revision,"
                        "b.decision_hash IS NOT NULL AS has_decision_hash,"
                        "b.effect_identity IS NOT NULL AS has_effect_identity,"
                        "b.canonical_event_id IS NOT NULL AS has_event,"
                        "b.canonical_event_hash IS NOT NULL AS has_event_hash,"
                        "d.decision_hash=b.decision_hash AS decision_hash_matches,"
                        "d.action_hash=b.action_hash AS decision_action_matches,"
                        "ar.action_hash=b.action_hash AS revision_action_matches,"
                        "e.event_hash=b.canonical_event_hash AS event_hash_matches,"
                        "e.run_id=a.run_id AS event_run_matches "
                        "FROM business_actions b "
                        "JOIN approval_requests a ON a.tenant_id=b.tenant_id "
                        "AND a.id=b.approval_id "
                        "JOIN human_decisions d ON d.tenant_id=a.tenant_id "
                        "AND d.id=b.human_decision_id AND d.approval_id=a.id "
                        "JOIN approval_action_revisions ar ON ar.tenant_id=a.tenant_id "
                        "AND ar.id=b.action_revision_id AND ar.approval_id=a.id "
                        "JOIN agent_events e ON e.tenant_id=a.tenant_id "
                        "AND e.id=b.canonical_event_id "
                        "WHERE b.id=:business_action_id"
                    ),
                    {"business_action_id": effect.business_action_id},
                )
            )
            .mappings()
            .one()
        )
        assert all(binding.values()), dict(binding)
        approval = await session.get(ApprovalRequest, approval_id)
        billing = await session.get(BillingRecord, billing_record_id)
        action = await session.get(BusinessAction, effect.business_action_id)
        assert approval is not None and approval.status == "executed"
        assert approval.consumed_at is not None
        assert billing is not None and billing.status == "refunded" and billing.version == 3
        assert action is not None and action.canonical_event_id and action.canonical_event_hash
        await verify_ticket_event_chain(session, ticket_id)

    for action_type in ("api_key_revocation", "entitlement_change"):
        action_prefix = f"{action_type}_{uuid4().hex[:12]}"
        (
            action_approval_id,
            action_ticket_id,
            action_validated_answer,
        ) = await _seed_production_shaped_pending_approval_fixture(
            admin_factory,
            action_prefix,
            action_type=action_type,
        )
        async with api_factory.request(_approver_scope(action_prefix)) as session:
            action_acceptance = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=action_approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{action_prefix}",
                reason="Action capability positive-path fixture.",
                approver_note=f"Execute {action_type} through the restricted capability.",
                trace_id=f"trace-{action_prefix}",
            )
            await session.commit()
        async with admin_factory() as session, session.begin():
            action_approval = await session.get(ApprovalRequest, action_approval_id)
            assert action_approval is not None and action_approval.run_id is not None
            action_lease = await RuntimeJobRepository(session).claim(
                job_id=action_acceptance.job_id,
                owner=f"worker-{action_prefix}",
            )
            await _checkpoint_action_resume_intent(
                session,
                approval=action_approval,
                lease=action_lease,
                label=action_prefix,
                validated_answer=action_validated_answer,
            )
            action_run_id = action_approval.run_id
            action_customer_id = action_approval.customer_id
            action_resource_id = str(
                action_approval.action_payload[
                    "api_key_id" if action_type == "api_key_revocation" else "subscription_id"
                ]
            )
        action_context = WorkerExecutionContext(
            tenant_id="tenant_demo",
            actor_principal_id=action_customer_id,
            executor_service_principal="supportguard_worker",
            customer_id=action_customer_id,
            ticket_id=action_ticket_id,
            run_id=action_run_id,
            job_id=action_lease.job_id,
            segment_id=f"segment-{action_prefix}",
            delivery_generation=1,
            fencing_token=action_lease.fencing_token,
            trace_id=f"trace-execute-{action_prefix}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        async with worker_factory.worker(action_context) as session:
            action_effect = await RuntimeActionExecutor(session).execute(
                action_lease,
                approval_id=action_approval_id,
            )
            await session.commit()
        assert action_effect.status == "succeeded" and action_effect.reused is False
        async with worker_factory.worker(action_context) as session:
            action_replay = await RuntimeActionExecutor(session).execute(
                action_lease,
                approval_id=action_approval_id,
            )
            await session.commit()
        assert action_replay.business_action_id == action_effect.business_action_id
        assert action_replay.reused is True
        async with admin_factory() as session:
            stored_approval = await session.get(ApprovalRequest, action_approval_id)
            stored_action = await session.get(BusinessAction, action_effect.business_action_id)
            assert stored_approval is not None and stored_approval.status == "executed"
            assert stored_action is not None and stored_action.canonical_event_id
            if action_type == "api_key_revocation":
                key = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == action_resource_id)
                )
                assert key is not None and key.status == "revoked" and key.version == 3
            else:
                subscription = await session.get(Subscription, action_resource_id)
                assert subscription is not None
                assert subscription.concurrency_limit in {40, 60}
                assert subscription.version == stored_action.resource_version + 1
            await verify_ticket_event_chain(session, action_ticket_id)

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            stale_approval_id,
            _,
            stale_resource_id,
            stale_lease,
            stale_context,
        ) = await prepare_action(action_type, "resource_stale")
        async with admin_factory() as session, session.begin():
            if action_type == "refund":
                resource = await session.get(BillingRecord, stale_resource_id)
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == stale_resource_id)
                )
            else:
                resource = await session.get(Subscription, stale_resource_id)
            assert resource is not None
            resource.version += 1
            injected_version = resource.version
        async with worker_factory.worker(stale_context) as session:
            stale_result = await RuntimeActionExecutor(session).execute(
                stale_lease,
                approval_id=stale_approval_id,
            )
            await session.commit()
        assert stale_result.status == "stale"
        assert stale_result.business_action_id is None
        async with admin_factory() as session:
            stale_approval = await session.get(ApprovalRequest, stale_approval_id)
            assert stale_approval is not None and stale_approval.status == "stale"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == stale_approval_id
                    )
                )
                == 0
            )
            if action_type == "refund":
                resource = await session.get(BillingRecord, stale_resource_id)
                assert resource is not None and resource.status == "charged"
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == stale_resource_id)
                )
                assert resource is not None and resource.status == "active"
            else:
                resource = await session.get(Subscription, stale_resource_id)
                assert resource is not None
            assert resource.version == injected_version

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            revoked_approval_id,
            _,
            revoked_resource_id,
            revoked_lease,
            revoked_context,
        ) = await prepare_action(action_type, "scope_revoked")
        async with admin_factory() as session, session.begin():
            scope = await session.get(
                ApproverTenantScope,
                {"user_id": "user_approver_demo", "tenant_id": "tenant_demo"},
            )
            assert scope is not None
            await session.delete(scope)
            if action_type == "refund":
                resource = await session.get(BillingRecord, revoked_resource_id)
                resource_snapshot = (resource.status, resource.version) if resource else None
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == revoked_resource_id)
                )
                resource_snapshot = (resource.status, resource.version) if resource else None
            else:
                resource = await session.get(Subscription, revoked_resource_id)
                resource_snapshot = (
                    (resource.concurrency_limit, resource.version) if resource else None
                )
            assert resource_snapshot is not None
        async with worker_factory.worker(revoked_context) as session:
            revoked_result = await RuntimeActionExecutor(session).execute(
                revoked_lease,
                approval_id=revoked_approval_id,
            )
            await session.commit()
        assert revoked_result.status == "stale"
        assert revoked_result.business_action_id is None
        async with admin_factory() as session, session.begin():
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == revoked_approval_id
                    )
                )
                == 0
            )
            if action_type == "refund":
                resource = await session.get(BillingRecord, revoked_resource_id)
                current_snapshot = (resource.status, resource.version) if resource else None
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == revoked_resource_id)
                )
                current_snapshot = (resource.status, resource.version) if resource else None
            else:
                resource = await session.get(Subscription, revoked_resource_id)
                current_snapshot = (
                    (resource.concurrency_limit, resource.version) if resource else None
                )
            assert current_snapshot == resource_snapshot
            session.add(
                ApproverTenantScope(
                    user_id="user_approver_demo",
                    tenant_id="tenant_demo",
                )
            )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            disabled_approval_id,
            _,
            _,
            disabled_lease,
            disabled_context,
        ) = await prepare_action(action_type, "kill_switch")
        async with admin_factory() as session, session.begin():
            switch = await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_demo", "action_type": action_type},
            )
            assert switch is not None and switch.enabled is True
            switch.enabled = False
            switch.version += 1
        with pytest.raises(DBAPIError, match="mutation_disabled"):
            async with worker_factory.worker(disabled_context) as session:
                await RuntimeActionExecutor(session).execute(
                    disabled_lease,
                    approval_id=disabled_approval_id,
                )
                await session.commit()
        async with admin_factory() as session, session.begin():
            disabled_approval = await session.get(ApprovalRequest, disabled_approval_id)
            assert disabled_approval is not None and disabled_approval.status == "approved"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == disabled_approval_id
                    )
                )
                == 0
            )
            switch = await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_demo", "action_type": action_type},
            )
            assert switch is not None and switch.enabled is False
            switch.enabled = True
            switch.version += 1
        await retire_entitlement_fixture(
            action_type,
            approval_id=disabled_approval_id,
            lease=disabled_lease,
            context=disabled_context,
        )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            rollback_approval_id,
            _,
            rollback_resource_id,
            rollback_lease,
            rollback_context,
        ) = await prepare_action(action_type, "commit_guard_rollback")
        async with admin_factory() as session:
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == rollback_approval_id)
            )
            assert decision is not None
            rollback_decision_id = decision.id
            if action_type == "refund":
                resource = await session.get(BillingRecord, rollback_resource_id)
                resource_snapshot = (resource.status, resource.version) if resource else None
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == rollback_resource_id)
                )
                resource_snapshot = (resource.status, resource.version) if resource else None
            else:
                resource = await session.get(Subscription, rollback_resource_id)
                resource_snapshot = (
                    (resource.concurrency_limit, resource.version) if resource else None
                )
            assert resource_snapshot is not None
        with pytest.raises(DBAPIError, match="business_action_commit_binding_invalid"):
            async with worker_factory.worker(rollback_context) as session:
                unbound = await execute_runtime_action_capability(
                    session,
                    approval_id=rollback_approval_id,
                    human_decision_id=rollback_decision_id,
                    lease=rollback_lease,
                )
                assert unbound.status == "succeeded" and unbound.reused is False
                await session.commit()
        async with admin_factory() as session:
            rollback_approval = await session.get(ApprovalRequest, rollback_approval_id)
            assert rollback_approval is not None and rollback_approval.status == "approved"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == rollback_approval_id
                    )
                )
                == 0
            )
            if action_type == "refund":
                resource = await session.get(BillingRecord, rollback_resource_id)
                current_snapshot = (resource.status, resource.version) if resource else None
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == rollback_resource_id)
                )
                current_snapshot = (resource.status, resource.version) if resource else None
            else:
                resource = await session.get(Subscription, rollback_resource_id)
                current_snapshot = (
                    (resource.concurrency_limit, resource.version) if resource else None
                )
            assert current_snapshot == resource_snapshot
        await retire_entitlement_fixture(
            action_type,
            approval_id=rollback_approval_id,
            lease=rollback_lease,
            context=rollback_context,
        )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            fenced_approval_id,
            _,
            _,
            fenced_lease,
            fenced_context,
        ) = await prepare_action(action_type, "stale_fence")
        bad_lease = replace(
            fenced_lease,
            fencing_token=fenced_lease.fencing_token + 1,
        )
        bad_context = replace(fenced_context, fencing_token=bad_lease.fencing_token)
        with pytest.raises((DBAPIError, RuntimeConflict), match="stale_fencing_token"):
            async with worker_factory.worker(bad_context) as session:
                await RuntimeActionExecutor(session).execute(
                    bad_lease,
                    approval_id=fenced_approval_id,
                )
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, fenced_approval_id)
            assert approval is not None and approval.status == "approved"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == fenced_approval_id
                    )
                )
                == 0
            )
        await retire_entitlement_fixture(
            action_type,
            approval_id=fenced_approval_id,
            lease=fenced_lease,
            context=fenced_context,
        )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            bound_approval_id,
            _,
            _,
            bound_lease,
            bound_context,
        ) = await prepare_action(action_type, "cross_decision_bound")
        cross_approval_id, _, _, _, _ = await prepare_action(
            "refund" if action_type == "entitlement_change" else action_type,
            "cross_decision_foreign",
        )
        async with admin_factory() as session:
            foreign_decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == cross_approval_id)
            )
            assert foreign_decision is not None
            foreign_decision_id = foreign_decision.id
        with pytest.raises(
            DBAPIError,
            match="action_(?:finalizer_)?binding_invalid",
        ):
            async with worker_factory.worker(bound_context) as session:
                await execute_runtime_action_capability(
                    session,
                    approval_id=bound_approval_id,
                    human_decision_id=foreign_decision_id,
                    lease=bound_lease,
                )
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, bound_approval_id)
            assert approval is not None and approval.status == "approved"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == bound_approval_id
                    )
                )
                == 0
            )
        await retire_entitlement_fixture(
            action_type,
            approval_id=bound_approval_id,
            lease=bound_lease,
            context=bound_context,
        )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            tenant_approval_id,
            _,
            _,
            tenant_lease,
            tenant_context,
        ) = await prepare_action(action_type, "cross_tenant")
        async with admin_factory() as session:
            tenant_decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == tenant_approval_id)
            )
            assert tenant_decision is not None
            tenant_decision_id = tenant_decision.id
        foreign_context = replace(
            tenant_context,
            tenant_id="tenant_other",
            actor_principal_id="cust_other",
            customer_id="cust_other",
        )
        with pytest.raises(
            DBAPIError,
            match="(?:stale_fencing_token|action_binding_invalid)",
        ):
            async with worker_factory.worker(foreign_context) as session:
                await execute_runtime_action_capability(
                    session,
                    approval_id=tenant_approval_id,
                    human_decision_id=tenant_decision_id,
                    lease=tenant_lease,
                )
        async with admin_factory() as session:
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == tenant_approval_id
                    )
                )
                == 0
            )
        await retire_entitlement_fixture(
            action_type,
            approval_id=tenant_approval_id,
            lease=tenant_lease,
            context=tenant_context,
        )

    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        (
            concurrent_approval_id,
            _,
            _,
            concurrent_lease,
            concurrent_context,
        ) = await prepare_action(action_type, "concurrent_effect")

        async def execute_concurrently(
            context: WorkerExecutionContext = concurrent_context,
            lease: JobLease = concurrent_lease,
            approval_id: str = concurrent_approval_id,
        ):
            async with worker_factory.worker(context) as session:
                result = await RuntimeActionExecutor(session).execute(
                    lease,
                    approval_id=approval_id,
                )
                await session.commit()
                return result

        concurrent_first, concurrent_second = await asyncio.gather(
            execute_concurrently(),
            execute_concurrently(),
        )
        assert concurrent_first.business_action_id == concurrent_second.business_action_id
        assert sorted([concurrent_first.reused, concurrent_second.reused]) == [False, True]
        async with admin_factory() as session:
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == concurrent_approval_id
                    )
                )
                == 1
            )
    await worker.dispose()
    await api.dispose()
    await admin.dispose()


async def test_marker_prepare_commits_a_cross_run_boundary_event() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"marker_parent_{uuid4().hex[:12]}"
    prior_run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        prior_run = await session.get(AgentRun, prior_run_id)
        assert prior_run is not None
        parent = await AgentRunStore(session).append_event(
            prior_run,
            event_type="prepared_parent",
            payload={"fixture": "cross-run-marker-parent"},
        )
        parent_id = parent.id
        parent_hash = parent.event_hash
        ticket_id = prior_run.ticket_id
        current_message_id = f"message_{prefix}_current"
        current_run_id = f"run_{prefix}_current"
        session.add(
            TicketMessage(
                id=current_message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="cross-run marker boundary fixture",
            )
        )
        await session.flush()
        session.add(
            AgentRun(
                id=current_run_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                customer_id="cust_demo",
                message_id=current_message_id,
                status="queued",
                model="fake",
                provider_mode="fake",
                tool_call_mode="native_fixture",
                prompt_version="fault.v1",
                schema_version="agent.v1",
                context_version="context.v1.2",
            )
        )
        await session.flush()
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=current_run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner=f"worker-{prefix}")
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"fixture": "cross-run-marker-parent"},
        )
        marker_id = marker.id
        boundary_id = marker.expected_ticket_head_event_id
        assert boundary_id != parent_id
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}_current",
            checkpoint_hash="a" * 64,
            outcome="completed",
            state={
                "ticket_id": ticket_id,
                "customer_id": "cust_demo",
                "run_id": current_run_id,
                "trace_id": f"trace-{prefix}",
                "classification": {"issue_type": "product_knowledge", "risk": "low"},
                "agent_finish_reason": "answered",
                "segment_events": [],
                "tool_observations": [],
                "evidence": [],
                "final": {
                    "answer": "Cross-run boundary finalized safely.",
                    "terminal_state": "resolved",
                    "policy_route": "answer",
                },
            },
        )
        execution_context = WorkerExecutionContext(
            tenant_id="tenant_demo",
            actor_principal_id="cust_demo",
            executor_service_principal=lease.owner,
            customer_id="cust_demo",
            ticket_id=ticket_id,
            run_id=current_run_id,
            job_id=lease.job_id,
            segment_id=marker.id,
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            trace_id=f"trace-{prefix}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        with worker_execution_context.bind(execution_context):
            async with worker_factory.worker(execution_context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )
                await session.commit()
    finally:
        await worker.dispose()

    async with factory() as session:
        stored = await session.get(CheckpointCommitMarker, marker_id)
        assert stored is not None and stored.status == "finalized"
        boundary = await session.get(AgentEvent, boundary_id)
        assert boundary is not None
        assert boundary.run_id == current_run_id
        assert boundary.previous_event_id is None
        assert boundary.parent_event_hash == parent_hash
        assert stored.expected_ticket_head_event_id == boundary.id
        final_event = await session.scalar(
            select(AgentEvent)
            .where(AgentEvent.run_id == current_run_id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        assert final_event is not None and final_event.event_type == "final_outcome"
        assert final_event.previous_event_id == boundary.id
        assert await verify_ticket_event_chain(session, ticket_id) == final_event.event_hash
    await admin.dispose()


async def test_v156_provider_error_code_survives_atomic_segment_finalization() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"v156_provider_error_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        run = await session.get(AgentRun, run_id)
        assert run is not None
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
        )
        lease = await RuntimeJobRepository(session).claim(
            job_id=job.id,
            owner=f"worker-{prefix}",
        )
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"fixture": "v156-provider-error"},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="f" * 64,
            outcome="completed",
            state={
                "ticket_id": run.ticket_id,
                "customer_id": run.customer_id,
                "run_id": run.id,
                "trace_id": f"trace-{prefix}",
                "classification": {"issue_type": "api_diagnostics", "risk": "low"},
                "agent_finish_reason": "provider_failed",
                "safe_stop_reason": "provider_failed",
                "safe_stop_error_code": "provider_http_503",
                "segment_events": [],
                "tool_observations": [],
                "evidence": [],
                "final": {
                    "answer": "暂时无法完成自动诊断，尚未执行任何操作。",
                    "terminal_state": "failed",
                    "policy_route": "answer",
                },
            },
        )
        marker_id = marker.id
        customer_id = run.customer_id
        ticket_id = run.ticket_id

    await _finalize_as_worker(
        url,
        lease,
        marker_id,
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        trace_id=f"trace-{prefix}",
    )

    async with factory() as session:
        stored_run = await session.get(AgentRun, run_id)
        assert stored_run is not None
        assert stored_run.agent_finish_reason == "provider_failed"
        assert stored_run.error_code == "provider_http_503"
        stored_ticket = await session.get(SupportTicket, stored_run.ticket_id)
        assert stored_ticket is not None and stored_ticket.status == "failed"
    await admin.dispose()


async def test_v156_billing_id_clarification_is_published_without_business_side_effects() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"v156_billing_clarification_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)
    rendered = AgentRuntimeServices._render_validated_answer(
        CandidateResponse(
            answer="Please provide the relevant resource reference.",
            action="answer",
            knowledge_chunk_ids=[],
            business_source_ids=[],
        ),
        route=PolicyRoute.ANSWER,
        finish_reason="needs_clarification",
        integrity=True,
        issue_type="billing_refund",
        requested_action="refund",
    )

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        run = await session.get(AgentRun, run_id)
        assert run is not None
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
        )
        lease = await RuntimeJobRepository(session).claim(
            job_id=job.id,
            owner=f"worker-{prefix}",
        )
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"fixture": "v156-billing-id-clarification"},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="c" * 64,
            outcome="completed",
            state={
                "ticket_id": run.ticket_id,
                "customer_id": run.customer_id,
                "run_id": run.id,
                "trace_id": f"trace-{prefix}",
                "classification": {
                    "issue_type": "billing_refund",
                    "risk": "high",
                    "requested_action": "refund",
                },
                "agent_finish_reason": "needs_clarification",
                "segment_events": [],
                "tool_observations": [],
                "evidence": [],
                "final": {
                    "answer": rendered,
                    "terminal_state": "needs_clarification",
                    "policy_route": "answer",
                    "source_refs": [],
                },
            },
        )
        marker_id = marker.id
        customer_id = run.customer_id
        ticket_id = run.ticket_id

    await _finalize_as_worker(
        url,
        lease,
        marker_id,
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        trace_id=f"trace-{prefix}",
    )

    async with factory() as session:
        stored_run = await session.get(AgentRun, run_id)
        assert stored_run is not None
        assistant = await session.scalar(
            select(TicketMessage).where(
                TicketMessage.run_id == run_id,
                TicketMessage.role == "assistant",
            )
        )
        assert assistant is not None
        assert "账单 ID" in assistant.content
        assert "Billing ID" in assistant.content
        assert "账单编号" in assistant.content
        assert "不会创建审批" in assistant.content
        assert "不会执行" in assistant.content
        proposal_count = int(
            await session.scalar(
                select(func.count(ProposalRecord.id)).where(ProposalRecord.run_id == run_id)
            )
            or 0
        )
        approval_count = int(
            await session.scalar(
                select(func.count(ApprovalRequest.id)).where(ApprovalRequest.run_id == run_id)
            )
            or 0
        )
        action_count = int(
            await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.ticket_id == stored_run.ticket_id
                )
            )
            or 0
        )
        assert (proposal_count, approval_count, action_count) == (0, 0, 0)
    await admin.dispose()


@pytest.mark.parametrize(
    ("action_name", "action_type", "resource_id"),
    [
        ("propose_refund", "refund", "bill_v156"),
        ("propose_api_key_revocation", "api_key_revocation", "key_v156"),
        ("propose_entitlement_change", "entitlement_change", "sub_v156"),
    ],
)
async def test_v156_graph_accepts_only_scoped_durable_proposal_records(
    action_name: str,
    action_type: str,
    resource_id: str,
) -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"v156_proposal_{action_type}_{uuid4().hex[:10]}"
    run_id = await _seed_run(factory, prefix)
    proposal_id = f"proposal_{prefix}"
    invalid_proposal_id = f"proposal_{prefix}_invalid"
    action_hash = "a" * 64
    action_payload = {
        "refund": {
            "billing_record_id": resource_id,
            "business_version": 2,
            "customer_id": "cust_demo",
            "refund_reason": "Verified duplicate charge fixture.",
        },
        "api_key_revocation": {
            "api_key_id": resource_id,
            "reason": "Verified active key revocation fixture.",
        },
        "entitlement_change": {
            "subscription_id": resource_id,
            "change_type": "quota_change",
            "target": {"concurrency_limit": 40},
            "reason": "Verified entitlement target fixture.",
        },
    }[action_type]
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        if action_type == "refund":
            charged_at = datetime.now(UTC) - timedelta(days=1)
            session.add_all(
                [
                    BillingRecord(
                        id=f"{resource_id}_original",
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        amount=Decimal("49.00"),
                        currency="USD",
                        status="charged",
                        charged_at=charged_at,
                        service_period_start=date(2026, 8, 1),
                        service_period_end=date(2026, 9, 1),
                        duplicate_of=None,
                        version=1,
                    ),
                    BillingRecord(
                        id=resource_id,
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        amount=Decimal("49.00"),
                        currency="USD",
                        status="charged",
                        charged_at=charged_at,
                        service_period_start=date(2026, 8, 1),
                        service_period_end=date(2026, 9, 1),
                        duplicate_of=f"{resource_id}_original",
                        version=2,
                    ),
                ]
            )
            await session.flush()
        session.add(
            ProposalRecord(
                id=proposal_id,
                tenant_id="tenant_demo",
                run_id=run_id,
                proposal_identity=f"identity:{prefix}",
                action_type=action_type,
                resource_id=resource_id,
                resource_version=2,
                action_payload=action_payload,
                observation_binding=[],
                action_hash=action_hash,
                status="draft",
            )
        )
        if action_type == "entitlement_change":
            session.add(
                ProposalRecord(
                    id=invalid_proposal_id,
                    tenant_id="tenant_demo",
                    run_id=run_id,
                    proposal_identity=f"identity:{prefix}:invalid",
                    action_type=action_type,
                    resource_id=resource_id,
                    resource_version=2,
                    action_payload={
                        **action_payload,
                        "target": {"concurrency_limit": None},
                    },
                    observation_binding=[],
                    action_hash="c" * 64,
                    status="draft",
                )
            )

    resource_field = {
        "refund": "billing_record_id",
        "api_key_revocation": "api_key_id",
        "entitlement_change": "subscription_id",
    }[action_type]
    observation_binding = [
        {
            "tool_name": {
                "refund": "query_billing_record",
                "api_key_revocation": "query_api_key_metadata",
                "entitlement_change": "query_subscription",
            }[action_type],
            "resource_field": resource_field,
            "resource_id": resource_id,
            "resource_version": 2,
        },
        {"tool_name": "search_knowledge"},
    ]
    eligibility = ProposalEligibility(
        eligible=True,
        action_type=cast(Any, action_type),
        resource_type=resource_field,
        resource_id=resource_id,
        resource_version=2,
        trusted_arguments=action_payload,
        observation_binding=observation_binding,
    )
    async with factory() as session:
        graph = SupportGraph(
            provider=DeterministicFakeProvider(),
            retrieval=None,
            gateway=ToolGateway(None),
            session=session,
        )
        assert await graph.runtime._proposal_is_durable(  # noqa: SLF001
            AgentState(tenant_id="tenant_demo", run_id=run_id),
            {"proposal_id": proposal_id, "action_hash": action_hash},
            action_name=action_name,
            eligibility=eligibility,
        )
        assert not await graph.runtime._proposal_is_durable(  # noqa: SLF001
            AgentState(tenant_id="tenant_demo", run_id=run_id),
            {"proposal_id": proposal_id, "action_hash": "b" * 64},
            action_name=action_name,
            eligibility=eligibility,
        )
        assert not await graph.runtime._proposal_is_durable(  # noqa: SLF001
            AgentState(tenant_id="tenant_other", run_id=run_id),
            {"proposal_id": proposal_id, "action_hash": action_hash},
            action_name=action_name,
            eligibility=eligibility,
        )
        if action_type == "entitlement_change":
            assert not await graph.runtime._proposal_is_durable(  # noqa: SLF001
                AgentState(tenant_id="tenant_demo", run_id=run_id),
                {"proposal_id": invalid_proposal_id, "action_hash": "c" * 64},
                action_name=action_name,
                eligibility=eligibility,
            )
    await admin.dispose()


async def test_v156_completed_finalizer_rejects_proposed_state_without_proposal() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"v156_missing_proposal_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        run = await session.get(AgentRun, run_id)
        assert run is not None
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
        )
        lease = await RuntimeJobRepository(session).claim(
            job_id=job.id,
            owner=f"worker-{prefix}",
        )
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"fixture": "v156-missing-proposal"},
        )
        with pytest.raises(RuntimeConflict, match="proposal_not_durable"):
            await SegmentRepository(session).checkpoint_written(
                lease,
                marker_id=marker.id,
                checkpoint_id=f"checkpoint_{prefix}",
                checkpoint_hash="e" * 64,
                outcome="completed",
                state={
                    "ticket_id": run.ticket_id,
                    "customer_id": run.customer_id,
                    "run_id": run.id,
                    "trace_id": f"trace-{prefix}",
                    "classification": {"issue_type": "billing_refund", "risk": "high"},
                    "agent_finish_reason": "proposed",
                    "segment_events": [],
                    "tool_observations": [],
                    "evidence": [],
                    "final": {
                        "answer": "错误的已提交审批文案",
                        "terminal_state": "resolved",
                        "policy_route": "await_approval",
                    },
                },
            )
        assert marker.status == "prepared"
    await admin.dispose()


async def test_v1211_terminal_finish_replay_acks_the_persisted_delivery() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"terminal_replay_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner=f"worker-{prefix}")
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"fixture": "terminal_finish_replay"},
        )
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert run is not None
        # Force the controlled Worker handoff down its exhausted-attempt path;
        # never bypass the database-owned dead-transition capability in tests.
        job.attempt = 100
        job_id = job.id
        marker_id = marker.id
        fence = lease.fencing_token

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    try:
        async with worker.begin() as connection:
            terminal = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'failed:RuntimeConflict')"
                ),
                {
                    "job_id": job_id,
                    "owner": f"worker-{prefix}",
                    "fencing_token": fence,
                },
            )
        assert isinstance(terminal, dict)
        assert terminal["job_id"] == job_id
        assert terminal["ticket_id"] == f"ticket_{prefix}"
        assert terminal["status"] == "dead"
        assert terminal["outcome"] == "failed:RuntimeConflict"
        assert terminal["dispatch_sequence"] == 1

        # Model the transport replay window: canonical terminal state committed,
        # but a late/duplicated delivery still needs acknowledgement.
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            marker = await session.get(CheckpointCommitMarker, marker_id)
            assert marker is not None and marker.status == "aborted"
            session.add(
                InboxDelivery(
                    tenant_id="tenant_demo",
                    job_id=job_id,
                    delivery_id=f"delivery_{prefix}",
                    redis_message_id="1-0",
                    consumer_group="supportguard-workers",
                    status="claimed",
                )
            )

        async with worker.begin() as connection:
            result = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'failed:RuntimeConflict')"
                ),
                {
                    "job_id": job_id,
                    "owner": f"worker-{prefix}",
                    "fencing_token": fence,
                },
            )
        assert result == {
            "job_id": job_id,
            "ticket_id": f"ticket_{prefix}",
            "status": "dead",
            "outcome": "infrastructure_exhausted",
            "dispatch_sequence": 1,
        }
        async with factory() as session:
            stored = await session.scalar(
                select(InboxDelivery).where(InboxDelivery.job_id == job_id)
            )
            assert stored is not None and stored.status == "acked"
            assert stored.outcome == "infrastructure_exhausted"
            assert stored.terminal_at is not None
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_v1211_retry_exhaustion_converges_approved_action_without_trigger_error() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"retry_exhaust_{uuid4().hex[:12]}"
    approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="Retry exhaustion convergence fixture.",
            approver_note="No business effect is executed in this fault fixture.",
            trace_id=f"trace-{prefix}",
        )
        await session.commit()

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    terminal: dict[str, object] | None = None
    try:
        for attempt in range(1, 10):
            async with factory() as session, session.begin():
                await session.execute(
                    update(RuntimeJob)
                    .where(RuntimeJob.id == accepted.job_id)
                    .values(available_at=datetime.now(UTC) - timedelta(seconds=5))
                )
                lease = await RuntimeJobRepository(session).claim(
                    job_id=accepted.job_id,
                    owner=f"worker-{prefix}",
                )
            async with worker.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": accepted.job_id,
                        "owner": f"worker-{prefix}",
                        "fencing_token": lease.fencing_token,
                        "outcome": f"failed:fixture_attempt_{attempt}",
                    },
                )
            assert isinstance(result, dict)
            if result["status"] == "dead":
                terminal = result
                break
            assert result["status"] == "retry_wait"

        assert terminal is not None
        assert terminal["job_id"] == accepted.job_id
        assert terminal["ticket_id"] == ticket_id
        assert terminal["status"] == "dead"
        assert terminal["outcome"] == f"failed:fixture_attempt_{attempt}"
        assert terminal["dispatch_sequence"] == 2
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, accepted.job_id)
            run = await session.get(AgentRun, job.run_id if job is not None else "")
            ticket = await session.get(SupportTicket, ticket_id)
            assert approval is not None and approval.status == "failed"
            assert job is not None and job.status == "dead"
            assert job.outcome == "infrastructure_exhausted"
            assert job.lease_owner is None and job.lease_expires_at is None
            assert run is not None and run.status == "failed"
            assert run.active_job_id is None and run.active_fencing_token is None
            # v1.5.12 removed technical-failure creation of manual_takeover:
            # there is no operator inbox to truthfully receive this ticket.
            assert ticket is not None and ticket.status == "failed"
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


async def test_v126_api_edit_and_approve_hash_matches_application_contract() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"api_edit_{uuid4().hex[:12]}"
    from test_v1512_runtime_action_binding_postgres import (
        _seed_production_shaped_pending_approval_fixture,
    )

    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        admin_factory,
        prefix,
        action_type="refund",
    )
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    replacement_reason = "Duplicate charge confirmed after immutable evidence review."
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="edit_and_approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="Only the refund reason was edited.",
            approver_note="Edit contract fixture",
            edited_payload={"refund_reason": replacement_reason},
            trace_id=f"trace-{prefix}",
        )
        await session.commit()
    async with admin_factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.selected_revision_number == 1
        revision = await session.get(ApprovalActionRevision, approval.selected_revision_id)
        assert revision is not None
        assert revision.action_payload["refund_reason"] == replacement_reason
        assert revision.action_hash == action_hash(revision.action_payload)
        await verify_ticket_event_chain(session, ticket_id)
    async with admin_factory() as session, session.begin():
        lease = await RuntimeJobRepository(session).claim(
            job_id=accepted.job_id,
            owner=f"worker-{prefix}",
        )
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None
        revision = await session.get(ApprovalActionRevision, approval.selected_revision_id or "")
        assert revision is not None
        decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        assert decision is not None
        await assert_active_approver_scope(
            session,
            tenant_id=approval.tenant_id,
            actor_id=decision.actor_id,
        )
        await validate_execution_binding(session, approval, revision)
        await _checkpoint_action_resume_intent(
            session,
            approval=approval,
            lease=lease,
            label=prefix,
            validated_answer=validated_answer,
        )
        assert approval.run_id is not None
        run_id = approval.run_id
        customer_id = approval.customer_id

    execution_context = WorkerExecutionContext(
        tenant_id="tenant_demo",
        actor_principal_id=customer_id,
        executor_service_principal=lease.owner,
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        job_id=lease.job_id,
        segment_id=f"segment-{prefix}",
        delivery_generation=1,
        fencing_token=lease.fencing_token,
        trace_id=f"trace-execute-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        with worker_execution_context.bind(execution_context):
            async with worker_factory.worker(execution_context) as session:
                first = await RuntimeActionExecutor(session).execute(
                    lease,
                    approval_id=approval_id,
                )
                await session.commit()
            async with worker_factory.worker(execution_context) as session:
                replay = await RuntimeActionExecutor(session).execute(
                    lease,
                    approval_id=approval_id,
                )
                await session.commit()
        assert first.status == "succeeded" and first.reused is False
        assert replay.business_action_id == first.business_action_id
        assert replay.reused is True
    finally:
        await worker.dispose()

    async with admin_factory() as session, session.begin():
        approval = await session.get(ApprovalRequest, approval_id)
        job = await session.get(RuntimeJob, accepted.job_id)
        assert approval is not None and job is not None
        assert job.lease_owner is not None and job.lease_expires_at is not None
        assert job.fencing_token == lease.fencing_token
        assert job.id == lease.job_id
        action_count = int(
            await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == approval_id
                )
            )
            or 0
        )
        assert action_count == 1
        stored_action = await session.scalar(
            select(BusinessAction).where(BusinessAction.approval_id == approval_id)
        )
        stored_decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval_id)
        )
        assert stored_action is not None and stored_decision is not None
        operands = {
            "selected_revision_id": approval.selected_revision_id,
            "executed_revision_id": stored_action.action_revision_id,
            "decision_revision_id": stored_decision.action_revision_id,
            "selected_revision_number": approval.selected_revision_number,
            "action_status": stored_action.status,
            "job_fencing_token": job.fencing_token,
            "lease_fencing_token": lease.fencing_token,
            "first_action_id": first.business_action_id,
            "replay_action_id": replay.business_action_id,
            "replay_reused": replay.reused,
            "business_action_count": action_count,
            "approval_status": approval.status,
        }
        for predicate_id in (
            "execution_revision_exact",
            "current_action_executor_fence_required",
            "edited_async_effect_once",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-15",
                predicate_id=predicate_id,
                subject_kind="postgres_edited_action_execution",
                operands=operands,
            )
    await api.dispose()
    await admin.dispose()


async def _assert_approval_update_guard(
    factory: async_sessionmaker[AsyncSession], approval_id: str
) -> None:
    async def rejected(values: dict[str, object], code: str) -> None:
        with pytest.raises(DBAPIError, match=code):
            async with factory() as session, session.begin():
                await session.execute(
                    update(ApprovalRequest)
                    .where(ApprovalRequest.id == approval_id)
                    .values(**values)
                )

    await rejected(
        {"customer_id": "cust_other", "status": "stale", "status_version": 2},
        "approval_update_invalid",
    )
    await rejected(
        {"status": "stale", "status_version": 1},
        "approval_status_transition_invalid",
    )
    await rejected(
        {
            "status": "approved",
            "selected_revision_number": 999,
            "status_version": 2,
        },
        "approval_revision_binding_invalid",
    )


def _final_state(run_id: str) -> dict[str, object]:
    return {
        "ticket_id": run_id.replace("run_", "ticket_", 1),
        "customer_id": "cust_demo",
        "run_id": run_id,
        "trace_id": f"trace:{run_id}",
        "classification": {"issue_type": "product_knowledge", "risk": "low"},
        "agent_finish_reason": "answered",
        "tool_observations": [],
        "evidence": [],
        "segment_events": [],
        "final": {
            "answer": "Recovered by deterministic finalizer.",
            "terminal_state": "resolved",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "policy_route": "answer",
        },
    }


async def test_k4_real_postgres_finalizer_takeover_adds_no_external_attempt() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"k4_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        old_lease = await jobs.claim(job_id=job.id, owner="worker-old")
        marker = await SegmentRepository(session).prepare(
            old_lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"kind": "agent_start"},
        )
        await SegmentRepository(session).checkpoint_written(
            old_lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="a" * 64,
            outcome="completed",
            state=_final_state(run_id),
        )
        marker_id = marker.id
        job_id = job.id

    worker_handoff = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    try:
        async with worker_handoff.begin() as connection:
            failed = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'failed:takeover_fixture')"
                ),
                {
                    "job_id": job_id,
                    "owner": old_lease.owner,
                    "fencing_token": old_lease.fencing_token,
                },
            )
        assert isinstance(failed, dict) and failed["status"] == "retry_wait"
    finally:
        await worker_handoff.dispose()

    async with factory() as session, session.begin():
        loaded_job = await session.get(RuntimeJob, job_id, with_for_update=True)
        assert loaded_job is not None and loaded_job.status == "retry_wait"
        loaded_job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        new_lease = await RuntimeJobRepository(session).claim(
            job_id=loaded_job.id, owner="worker-new"
        )
        attempts_before = int(await session.scalar(select(func.count(AgentCallAttempt.id))) or 0)
        replacement = await SegmentRepository(session).takeover_finalizer(
            new_lease, source_marker_id=marker_id
        )
        replacement_marker_id = replacement.id

    await _finalize_as_worker(
        url,
        new_lease,
        replacement_marker_id,
        customer_id="cust_demo",
        ticket_id=run_id.replace("run_", "ticket_", 1),
        run_id=run_id,
        trace_id=f"trace:{run_id}",
    )

    async with factory() as session, session.begin():
        attempts_after = int(await session.scalar(select(func.count(AgentCallAttempt.id))) or 0)
        assert attempts_after == attempts_before
        version_before_handoff = int(
            await session.scalar(select(RuntimeJob.status_version).where(RuntimeJob.id == job_id))
        )
    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    try:
        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'completed')"
                ),
                {
                    "job_id": job_id,
                    "owner": new_lease.owner,
                    "fencing_token": new_lease.fencing_token,
                },
            )
        assert isinstance(handoff, dict)
        assert handoff["job_id"] == job_id
        assert handoff["ticket_id"] == f"ticket_{prefix}"
        assert handoff["status"] == "succeeded"
        assert handoff["outcome"] == "completed"
        assert handoff["dispatch_sequence"] == 1
        async with factory() as session:
            first_handoff = (
                await session.execute(
                    select(RuntimeJob.updated_at, RuntimeJob.status_version).where(
                        RuntimeJob.id == job_id
                    )
                )
            ).one()
            first_updated_at = first_handoff.updated_at
            assert first_handoff.status_version == version_before_handoff + 1
        async with worker.begin() as connection:
            replay = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'completed')"
                ),
                {
                    "job_id": job_id,
                    "owner": new_lease.owner,
                    "fencing_token": new_lease.fencing_token,
                },
            )
        assert replay == handoff
        async with factory() as session:
            replay_handoff = (
                await session.execute(
                    select(RuntimeJob.updated_at, RuntimeJob.status_version).where(
                        RuntimeJob.id == job_id
                    )
                )
            ).one()
            replay_updated_at = replay_handoff.updated_at
            assert replay_handoff.status_version == first_handoff.status_version
        assert replay_updated_at == first_updated_at
        with pytest.raises(DBAPIError, match="stale_fencing_token") as stale_write:
            async with worker.begin() as connection:
                await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,'completed')"
                    ),
                    {
                        "job_id": job_id,
                        "owner": new_lease.owner,
                        "fencing_token": new_lease.fencing_token - 1,
                    },
                )
    finally:
        await worker.dispose()
    async with factory() as session, session.begin():
        damaged_job = await session.get(RuntimeJob, job_id, with_for_update=True)
        damaged_run = await session.get(AgentRun, run_id, with_for_update=True)
        assert damaged_job is not None and damaged_run is not None
        damaged_ticket = await session.get(
            SupportTicket, damaged_run.ticket_id, with_for_update=True
        )
        assert damaged_ticket is not None
        damaged_run.status = "queued"
        damaged_ticket.status = "queued"
        expected_job_version = damaged_job.status_version
    reconciler = create_async_engine(
        make_url(url)
        .set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
        .render_as_string(hide_password=False)
    )
    try:
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {"job_id": job_id, "job_version": expected_job_version},
            )
        assert isinstance(repaired, dict)
        assert repaired["result"] == "terminal_reconciled"
        async with reconciler.begin() as connection:
            stale_replay = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {"job_id": job_id, "job_version": expected_job_version},
            )
        assert stale_replay == {"result": "stale"}
    finally:
        await reconciler.dispose()
    async with factory() as session:
        stored_run = await session.get(AgentRun, run_id)
        stored_job = await session.get(RuntimeJob, job_id)
        stored_ticket = await session.get(SupportTicket, stored_run.ticket_id if stored_run else "")
        source_marker = await session.get(CheckpointCommitMarker, marker_id)
        replacement_marker = await session.get(CheckpointCommitMarker, replacement.id)
        assert stored_run is not None and stored_run.status == "completed"
        assert stored_job is not None and stored_job.status == "succeeded"
        assert stored_job.outcome == "completed"
        assert stored_ticket is not None and stored_ticket.status == "resolved"
        assert source_marker is not None and source_marker.status == "aborted"
        assert replacement_marker is not None and replacement_marker.status == "finalized"
        record_predicate_operands(
            requirement_id="C6-P0-04",
            predicate_id="checkpoint_written_finalizer_only",
            subject_kind="postgres_finalizer_takeover",
            operands={
                "source_marker_initial_status": "checkpoint_written",
                "source_marker_terminal_status": source_marker.status,
                "replacement_marker_status": replacement_marker.status,
                "canonical_checkpoint_id": stored_run.canonical_checkpoint_id,
                "expected_checkpoint_id": f"checkpoint_{prefix}",
                "agent_attempt_count_before": attempts_before,
                "agent_attempt_count_after": attempts_after,
                "worker_handoff_status": handoff["status"],
                "worker_handoff_outcome": handoff["outcome"],
            },
        )
        record_predicate_operands(
            requirement_id="C6-P0-04",
            predicate_id="old_fence_write_zero",
            subject_kind="postgres_finalizer_takeover",
            operands={
                "old_fencing_token": old_lease.fencing_token,
                "new_fencing_token": new_lease.fencing_token,
                "stale_finish_fencing_token": new_lease.fencing_token - 1,
                "stale_finish_error": str(stale_write.value),
                "successful_stale_finish_count": 0,
                "persisted_job_status": stored_job.status,
                "persisted_job_outcome": stored_job.outcome,
            },
        )
    await engine.dispose()


async def test_real_postgres_reconciler_repairs_interrupted_terminal_from_payload() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"terminal_interrupt_{uuid4().hex[:12]}"
    approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
    async with factory() as session, session.begin():
        approval = await session.get(ApprovalRequest, approval_id, with_for_update=True)
        assert approval is not None and approval.run_id and approval.marker_id
        marker = await session.get(CheckpointCommitMarker, approval.marker_id, with_for_update=True)
        assert marker is not None
        job = await session.get(RuntimeJob, marker.job_id, with_for_update=True)
        run = await session.get(AgentRun, approval.run_id, with_for_update=True)
        ticket = await session.get(SupportTicket, ticket_id, with_for_update=True)
        assert job is not None and job.status == "succeeded" and job.outcome == "interrupted"
        assert run is not None and ticket is not None
        run.status = "queued"
        ticket.status = "queued"
        job_id = job.id
        expected_job_version = job.status_version
    reconciler = create_async_engine(
        make_url(url)
        .set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
        .render_as_string(hide_password=False)
    )
    try:
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {"job_id": job_id, "job_version": expected_job_version},
            )
        assert isinstance(repaired, dict)
        assert repaired["result"] == "terminal_reconciled"
        async with factory() as session:
            stored_approval = await session.get(ApprovalRequest, approval_id)
            stored_run = await session.get(AgentRun, approval.run_id)
            stored_ticket = await session.get(SupportTicket, ticket_id)
            stored_marker = await session.get(CheckpointCommitMarker, approval.marker_id)
            stored_job = await session.get(RuntimeJob, job_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            assert stored_approval is not None and stored_approval.status == "pending"
            assert stored_run is not None and stored_run.status == "interrupted"
            assert stored_ticket is not None and stored_ticket.status == "awaiting_approval"
            assert stored_marker is not None and stored_marker.status == "finalized"
            assert stored_job is not None
            record_predicate_operands(
                requirement_id="C6-P0-04",
                predicate_id="approval_marker_terminal_complete",
                subject_kind="postgres_terminal_approval_aggregate",
                operands={
                    "repair_disposition": repaired["result"],
                    "job_status": stored_job.status,
                    "job_outcome": stored_job.outcome,
                    "run_status": stored_run.status,
                    "ticket_status": stored_ticket.status,
                    "approval_status": stored_approval.status,
                    "marker_status": stored_marker.status,
                    "marker_segment_kind": stored_marker.segment_kind,
                    "business_action_count": action_count,
                },
            )
    finally:
        await reconciler.dispose()
        await admin.dispose()


@pytest.mark.parametrize(
    ("decision_name", "terminal_state"),
    [("reject", "rejected")],
)
async def test_real_postgres_reconciler_repairs_no_action_resume_from_decision(
    decision_name: str,
    terminal_state: str,
) -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"terminal_{decision_name}_{uuid4().hex[:10]}"
    from test_v1512_runtime_action_binding_postgres import (
        _seed_production_shaped_pending_approval_fixture,
    )

    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        factory,
        prefix,
        action_type="refund",
    )
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision=decision_name,
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason=f"Deterministic {decision_name} fixture.",
            approver_note="Exercise the no-action approval-resume path.",
            trace_id=f"trace-{prefix}",
        )
        await session.commit()

    assert accepted.job_id is None
    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.run_id is not None
        run = await session.get(AgentRun, approval.run_id)
        ticket = await session.get(SupportTicket, ticket_id)
        decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        assert decision is not None and decision.decision == decision_name
        resume_job_count = int(
            await session.scalar(
                select(func.count(RuntimeJob.id)).where(RuntimeJob.approval_id == approval_id)
            )
            or 0
        )
        action_count = int(
            await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == approval_id
                )
            )
            or 0
        )
        assert approval.status == terminal_state
        assert run is not None and run.status == "completed"
        assert ticket is not None and ticket.status == terminal_state
        assert resume_job_count == 0
        assert action_count == 0
        operands = {
            "decision_name": decision_name,
            "terminal_state": terminal_state,
            "job_status": "not_created",
            "run_status": run.status,
            "ticket_status": ticket.status,
            "approval_status": approval.status,
            "business_action_count": action_count,
            "finish_outcome": terminal_state,
            "reconcile_result": "not_required",
        }
        for predicate_id in (
            "legacy_approval_outcome_matrix_exact",
            "invalid_resume_effect_zero",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-14",
                predicate_id=predicate_id,
                subject_kind="postgres_noaction_resume_convergence",
                operands=operands,
            )
    await api.dispose()
    await admin.dispose()


async def test_real_postgres_reconciler_repairs_typed_binding_stale_terminal() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"terminal_stale_{uuid4().hex[:10]}"
    from test_v1512_runtime_action_binding_postgres import (
        _seed_production_shaped_pending_approval_fixture,
    )

    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        factory,
        prefix,
        action_type="refund",
    )
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="Binding changed before execution.",
            approver_note="Exercise typed stale terminal convergence.",
            trace_id=f"trace-{prefix}",
        )
        await session.commit()
    assert accepted.job_id is not None

    async with factory() as session, session.begin():
        approval = await session.get(ApprovalRequest, approval_id, with_for_update=True)
        assert approval is not None and approval.run_id and approval.selected_revision_id
        run = await session.get(AgentRun, approval.run_id, with_for_update=True)
        assert run is not None
        decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        assert decision is not None and decision.canonical_event_id is not None
        job = await session.get(RuntimeJob, accepted.job_id)
        assert job is not None
        lease = await RuntimeJobRepository(session).claim(job_id=job.id, owner=f"worker-{prefix}")
        billing = await session.get(
            BillingRecord,
            approval.resource_id,
            with_for_update=True,
        )
        assert billing is not None
        billing.version += 1
        marker_id = await _checkpoint_action_resume_intent(
            session,
            approval=approval,
            lease=lease,
            label=prefix,
            validated_answer=validated_answer,
        )
        job_id = job.id
        run_id = run.id

    await _finalize_as_worker(
        url,
        lease,
        marker_id,
        customer_id="cust_demo",
        ticket_id=ticket_id,
        run_id=run_id,
        trace_id=f"trace-{prefix}",
    )

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    try:
        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'completed')"
                ),
                {"job_id": job_id, "owner": lease.owner, "fencing_token": lease.fencing_token},
            )
        assert isinstance(handoff, dict) and handoff["outcome"] == "domain_terminal"
    finally:
        await worker.dispose()

    async with factory() as session, session.begin():
        stored_job = await session.get(RuntimeJob, job_id, with_for_update=True)
        stored_run = await session.get(AgentRun, run_id, with_for_update=True)
        stored_ticket = await session.get(SupportTicket, ticket_id, with_for_update=True)
        stored_approval = await session.get(ApprovalRequest, approval_id)
        assert stored_job is not None and stored_run is not None and stored_ticket is not None
        assert stored_approval is not None and stored_approval.status == "stale"
        assert stored_run.agent_finish_reason == "binding_stale"
        typed_evidence = await session.scalar(
            text(
                """
                SELECT jsonb_build_object(
                  'marker_finalized', m.status='finalized',
                  'schema_v2', f.schema_version='finalizer.v2',
                  'fence_matches', f.fencing_token=m.fencing_token,
                  'tenant_matches', f.payload->>'tenant_id'=j.tenant_id,
                  'run_matches', f.payload->>'run_id'=j.run_id,
                  'job_matches', f.payload->>'job_id'=j.id,
                  'marker_matches', f.payload->>'marker_id'=m.id,
                  'segment_matches', f.payload->>'segment_kind'=m.segment_kind,
                  'action_intent',
                    f.domain_delta->>'variant'='approval_action_intent',
                  'domain_completed',
                    f.domain_delta->>'outcome'='completed',
                  'approval_stale', a.status='stale',
                  'action_hash_matches',
                    f.domain_delta->>'action_hash'=a.action_hash,
                  'decision_matches', EXISTS (
                    SELECT 1 FROM human_decisions d
                    WHERE d.tenant_id=a.tenant_id
                      AND d.approval_id=a.id
                      AND d.id=f.domain_delta->>'human_decision_id'
                      AND d.decision=f.domain_delta->>'decision'
                      AND d.decision IN ('approve','edit_and_approve')
                  ),
                  'zero_effect', NOT EXISTS (
                    SELECT 1 FROM business_actions b
                    WHERE b.tenant_id=a.tenant_id AND b.approval_id=a.id
                  ),
                  'stale_event', EXISTS (
                    SELECT 1 FROM agent_events e
                    WHERE e.tenant_id=j.tenant_id
                      AND e.ticket_id=j.ticket_id
                      AND e.run_id=j.run_id
                      AND e.event_type='runtime_action_reconciliation'
                      AND e.payload->>'approval_id'=a.id
                      AND e.payload->>'status'='stale'
                      AND e.payload->>'reason' IN (
                        'binding_stale',
                        'publication_binding_stale',
                        'approver_scope_stale',
                        'resource_snapshot_stale',
                        'policy_stale'
                      )
                  ),
                  'event_reason', (
                    SELECT e.payload->>'reason' FROM agent_events e
                    WHERE e.tenant_id=j.tenant_id
                      AND e.ticket_id=j.ticket_id
                      AND e.run_id=j.run_id
                      AND e.event_type='runtime_action_reconciliation'
                    ORDER BY e.ticket_sequence DESC LIMIT 1
                  ),
                  'stale_receipt',
                    supportguard_internal_action_intent_stale_receipt(
                      j.tenant_id,j.id,j.run_id,f.domain_delta::jsonb
                    )
                )
                FROM runtime_jobs j
                JOIN checkpoint_commit_markers m
                  ON m.tenant_id=j.tenant_id AND m.run_id=j.run_id
                 AND m.job_id=j.id
                JOIN finalizer_payloads f
                  ON f.tenant_id=j.tenant_id AND f.run_id=j.run_id
                 AND f.job_id=j.id AND f.marker_id=m.id
                JOIN approval_requests a
                  ON a.tenant_id=j.tenant_id AND a.id=j.approval_id
                WHERE j.tenant_id=:tenant_id AND j.id=:job_id
                ORDER BY m.fencing_token DESC,m.created_at DESC,m.id DESC
                LIMIT 1
                """
            ),
            {"tenant_id": "tenant_demo", "job_id": job_id},
        )
        assert typed_evidence == {
            "marker_finalized": True,
            "schema_v2": True,
            "fence_matches": True,
            "tenant_matches": True,
            "run_matches": True,
            "job_matches": True,
            "marker_matches": True,
            "segment_matches": True,
            "action_intent": True,
            "domain_completed": True,
            "approval_stale": True,
            "action_hash_matches": True,
            "decision_matches": True,
            "zero_effect": True,
            "stale_event": True,
            "event_reason": "resource_snapshot_stale",
            "stale_receipt": True,
        }
        stored_run.status = "queued"
        stored_ticket.status = "queued"
        expected_version = stored_job.status_version

    reconciler = create_async_engine(
        make_url(url)
        .set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
        .render_as_string(hide_password=False)
    )
    try:
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {"job_id": job_id, "job_version": expected_version},
            )
        assert isinstance(repaired, dict) and repaired["result"] == "terminal_reconciled"
        async with factory() as session:
            repaired_run = await session.get(AgentRun, run_id)
            repaired_ticket = await session.get(SupportTicket, ticket_id)
            repaired_approval = await session.get(ApprovalRequest, approval_id)
            assert repaired_run is not None and repaired_run.status == "completed"
            assert repaired_ticket is not None and repaired_ticket.status == "failed"
            assert repaired_approval is not None and repaired_approval.status == "stale"
    finally:
        await reconciler.dispose()
        await api.dispose()
        await admin.dispose()


async def test_finalizer_actual_ticket_head_conflict_commits_fail_closed_terminal() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"head_{uuid4().hex[:12]}"
    run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner="worker-head")
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"kind": "agent_start"},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="e" * 64,
            outcome="completed",
            state=_final_state(run_id),
        )
        job_id = job.id
        marker_id = marker.id

    async with factory() as concurrent, concurrent.begin():
        run = await concurrent.get(AgentRun, run_id)
        assert run is not None
        await AgentRunStore(concurrent).append_event(
            run,
            event_type="concurrent_ticket_fact",
            payload={"source": "second_connection"},
        )

    async with factory() as session:
        job = await session.get(RuntimeJob, job_id)
        assert job is not None and job.lease_owner and job.lease_expires_at
        lease = JobLease(
            job.id,
            job.run_id,
            job.tenant_id,
            job.lease_owner,
            job.fencing_token,
            job.lease_expires_at,
        )
    with pytest.raises(RuntimeConflict, match="finalizer_actual_head_conflict"):
        await _finalize_as_worker(
            url,
            lease,
            marker_id,
            customer_id="cust_demo",
            ticket_id=run_id.replace("run_", "ticket_", 1),
            run_id=run_id,
            trace_id=f"trace:{run_id}",
        )

    async with factory() as session:
        run = await session.get(AgentRun, run_id)
        job = await session.get(RuntimeJob, job_id)
        marker = await session.get(CheckpointCommitMarker, marker_id)
        failure_replies = (
            await session.scalars(
                select(TicketMessage).where(
                    TicketMessage.tenant_id == "tenant_demo",
                    TicketMessage.publication_key == f"runtime-failure:{job_id}",
                )
            )
        ).all()
        final_events = await session.scalar(
            select(func.count(AgentEvent.id)).where(
                AgentEvent.run_id == run_id,
                AgentEvent.event_type == "final_outcome",
            )
        )
        assert run is not None and run.status == "failed"
        assert run.completed_at is not None
        assert run.canonical_checkpoint_id is None
        assert job is not None and job.status == "dead" and job.lease_owner is None
        assert marker is not None and marker.status == "aborted"
        assert len(failure_replies) == 1
        assert failure_replies[0].run_id == run_id
        failure_lines = failure_replies[0].content.splitlines()
        assert len(failure_lines) == 5
        assert failure_lines[0].startswith("已检查：")
        assert failure_lines[1].startswith("已确认：")
        assert failure_lines[2].startswith("仍未知（公开类别：runtime）：")
        assert failure_lines[3].startswith("审批与业务副作用状态：")
        assert failure_lines[4].startswith("可执行下一步：")
        assert final_events == 0
        record_predicate_operands(
            requirement_id="C4-P0-05c",
            predicate_id="c4_p0_05c",
            subject_kind="postgres_finalizer_actual_head_cas",
            operands={
                "run_status": run.status,
                "canonical_checkpoint_id": run.canonical_checkpoint_id,
                "job_status": job.status,
                "job_lease_owner": job.lease_owner,
                "marker_status": marker.status,
                "run_completed_at": run.completed_at.isoformat(),
                "failure_reply_count": len(failure_replies),
                "final_event_count": int(final_events or 0),
            },
        )
    await engine.dispose()


async def test_k5_k8_k9_real_postgres_interrupt_effect_reconcile_and_late_finalize() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"k5_{uuid4().hex[:12]}"
    from test_v1512_runtime_action_binding_postgres import (
        _seed_production_shaped_pending_approval_fixture,
    )

    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        factory,
        prefix,
        action_type="refund",
    )
    async with factory() as session:
        seeded_approval = await session.get(ApprovalRequest, approval_id)
        assert (
            seeded_approval is not None
            and seeded_approval.run_id is not None
            and seeded_approval.status == "pending"
        )
        run_id = seeded_approval.run_id

    await _assert_approval_update_guard(factory, approval_id)

    # K7 boundary: use the production API capability to persist the decision
    # and enqueue exactly one resume job before any high-risk effect exists.
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="K8 committed effect reconciliation fixture",
            approver_note="",
            trace_id=f"trace:{prefix}:decision",
        )
        await session.commit()
    assert accepted.job_id is not None

    async with factory() as session, session.begin():
        approval_for_decision = await session.get(
            ApprovalRequest, approval_id, with_for_update=True
        )
        run = await session.get(AgentRun, run_id, with_for_update=True)
        decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval_id)
        )
        resume_job = await session.get(RuntimeJob, accepted.job_id)
        assert (
            approval_for_decision is not None
            and approval_for_decision.status == "approved"
            and run is not None
            and decision is not None
            and decision.canonical_event_id is not None
            and resume_job is not None
        )
        resume_lease = await RuntimeJobRepository(session).claim(
            job_id=resume_job.id, owner="worker-resume"
        )
        resume_marker_id = await _checkpoint_action_resume_intent(
            session,
            approval=approval_for_decision,
            lease=resume_lease,
            label=prefix,
            validated_answer=validated_answer,
        )
        resume_job_id = resume_job.id
        decision_id = decision.id

    # K8 boundary: the Finalizer and Runtime-only effect are one root
    # transaction. COMMIT ambiguity is covered by the dedicated b189
    # verification path; this fixture must not manufacture an impossible
    # separately committed effect followed by an ordinary Finalizer call.
    await _finalize_as_worker(
        url,
        resume_lease,
        resume_marker_id,
        customer_id="cust_demo",
        ticket_id=ticket_id,
        run_id=run_id,
        trace_id=f"trace:{prefix}:reconcile",
    )
    async with factory() as session:
        committed_action = await session.scalar(
            select(BusinessAction).where(
                BusinessAction.approval_id == approval_id,
                BusinessAction.status == "succeeded",
            )
        )
        assert committed_action is not None
        action_id = committed_action.id

    worker = create_async_engine(
        make_url(url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    try:
        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'completed')"
                ),
                {
                    "job_id": resume_job_id,
                    "owner": resume_lease.owner,
                    "fencing_token": resume_lease.fencing_token,
                },
            )
        assert isinstance(handoff, dict) and handoff["outcome"] == "completed"
    finally:
        await worker.dispose()

    async with factory() as session, session.begin():
        damaged_job = await session.get(RuntimeJob, resume_job_id, with_for_update=True)
        damaged_run = await session.get(AgentRun, run_id, with_for_update=True)
        assert damaged_job is not None and damaged_run is not None
        damaged_ticket = await session.get(
            SupportTicket, damaged_run.ticket_id, with_for_update=True
        )
        assert damaged_ticket is not None
        damaged_run.status = "queued"
        damaged_ticket.status = "queued"
        expected_job_version = damaged_job.status_version
    reconciler = create_async_engine(
        make_url(url)
        .set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
        .render_as_string(hide_password=False)
    )
    try:
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {"job_id": resume_job_id, "job_version": expected_job_version},
            )
        assert isinstance(repaired, dict) and repaired["result"] == "terminal_reconciled"
    finally:
        await reconciler.dispose()

    # K9 boundary: commit succeeded but the worker/ACK observer retries. The
    # stale lease cannot advance anything and the effect remains exactly once.
    async with factory() as session, session.begin():
        final_job = await session.get(RuntimeJob, resume_job_id)
        assert final_job is not None
        late_lease = JobLease(
            final_job.id,
            final_job.run_id,
            final_job.tenant_id,
            "worker-resume",
            final_job.fencing_token,
            datetime.now(UTC),
        )
        with pytest.raises(RuntimeConflict):
            await SegmentRepository(session).finalize(late_lease, marker_id=resume_marker_id)
        effect_count = await session.scalar(
            select(func.count(BusinessAction.id)).where(
                BusinessAction.id == action_id,
                BusinessAction.status == "succeeded",
            )
        )
        assert effect_count == 1
        decision_count = await session.scalar(
            select(func.count(HumanDecision.id)).where(HumanDecision.id == decision_id)
        )
        assert decision_count == 1
        stored_run = await session.get(AgentRun, run_id)
        stored_ticket = await session.get(SupportTicket, stored_run.ticket_id if stored_run else "")
        stored_approval = await session.get(ApprovalRequest, approval_id)
        stored_snapshot = await session.scalar(
            select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval_id)
        )
        assert stored_run is not None and stored_run.status == "completed"
        assert stored_ticket is not None and stored_ticket.status == "resolved"
        assert stored_approval is not None and stored_approval.status == "executed"
        assert stored_snapshot is not None
        lineage_operands = {
            "origin_job_id": stored_snapshot.origin_job_id,
            "executor_job_id": resume_job_id,
            "origin_marker_id": stored_snapshot.origin_marker_id,
            "executor_marker_id": resume_marker_id,
            "origin_fencing_token": stored_snapshot.origin_fencing_token,
            "executor_fencing_token": final_job.fencing_token,
            "same_run_id": final_job.run_id == stored_run.id,
            "effect_count": int(effect_count or 0),
            "decision_count": int(decision_count or 0),
            "approval_status": stored_approval.status,
        }
        for predicate_id in (
            "approval_resume_cross_job_fence_bridge_exact",
            "origin_and_executor_identity_separately_validated",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-14",
                predicate_id=predicate_id,
                subject_kind="postgres_hitl_origin_executor_bridge",
                operands=lineage_operands,
            )
        record_predicate_operands(
            requirement_id="C4-P0-06d",
            predicate_id="c4_p0_06d",
            subject_kind="postgres_hitl_origin_executor_bridge",
            operands=lineage_operands,
        )
    await api.dispose()
    await engine.dispose()
