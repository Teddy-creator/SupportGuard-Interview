from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import seed_closed_refund_observation_binding
from supportguard.agent.persistence import verify_ticket_event_chain
from supportguard.contracts.context import (
    WorkerExecutionContext,
    worker_execution_context,
)
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApprovalRequest,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    ProposalRecord,
    ProposalWithdrawal,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.action_effect_reconciliation import (
    ActionEffectReconciliationRunner,
)
from supportguard.services.business import action_hash
from supportguard.services.runtime_jobs import RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_postgres_finalizer_faults import (
    _final_state,
    _seed_pending_approval,
    _seed_run,
)
from test_v1512_action_effect_reconciliation_postgres import (
    _prepare_unknown_action_effect,
)
from test_v1512_phase1_postgres_proof import (
    _accept_followup,
    _approve,
    _raw_decide_and_commit,
    _withdraw,
)

pytestmark = pytest.mark.postgres


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _role_url(database_url: str, *, username: str, password: str) -> str:
    return (
        make_url(database_url)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


@dataclass(frozen=True, slots=True)
class _Harness:
    admin: Any
    factory: Any
    api: Any
    api_factory: Any
    worker: Any


@dataclass(frozen=True, slots=True)
class _ApprovalPair:
    ticket_id: str
    target_approval_id: str
    sibling_approval_id: str
    target_run_id: str
    sibling_run_id: str


@asynccontextmanager
async def _postgres_harness(database_url: str) -> AsyncIterator[_Harness]:
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    try:
        yield _Harness(
            admin=admin,
            factory=factory,
            api=api,
            api_factory=api_factory,
            worker=worker,
        )
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


async def _seed_api_key_sibling_on_ticket(
    harness: _Harness,
    *,
    prefix: str,
    ticket_id: str,
) -> tuple[str, str]:
    """Create a second real interrupt on an existing Ticket.

    The sibling travels through customer admission, RuntimeJob claim, durable
    observation binding, checkpoint, and the restricted worker finalizer.  It
    is therefore a real second Approval aggregate rather than a direct fixture
    INSERT that could bypass the lifecycle contracts under test.
    """

    accepted = await _accept_followup(
        harness.api_factory,
        prefix=f"{prefix}_sibling",
        ticket_id=ticket_id,
    )
    assert accepted.run_id is not None
    assert accepted.job_id is not None
    resource_id = f"key_{prefix}"
    proposal_id = f"proposal_{prefix}_sibling"
    resource_version = 2
    payload: dict[str, object] = {
        "api_key_id": resource_id,
        "customer_id": "cust_demo",
        "fingerprint": f"fp_{prefix}",
        "reason": "Customer reported credential exposure.",
        "business_version": resource_version,
    }

    async with harness.factory() as session, session.begin():
        session.add(
            ApiKeyMetadata(
                id=f"keymeta_{prefix}",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                key_id=resource_id,
                fingerprint=f"fp_{prefix}",
                status="active",
                version=resource_version,
                last_used_summary={},
            )
        )
        lease = await RuntimeJobRepository(session).claim(
            job_id=accepted.job_id,
            owner=f"worker-{prefix}-sibling",
        )
        observation_binding = await seed_closed_refund_observation_binding(
            session,
            lease,
            segment_id=f"segment_{prefix}_sibling",
            billing_record_id=resource_id,
            billing_version=resource_version,
            business_tool="query_api_key_metadata",
            resource_field="api_key_id",
            policy_source_id="api-key-policy:c1",
        )
        session.add(
            ProposalRecord(
                id=proposal_id,
                tenant_id="tenant_demo",
                run_id=accepted.run_id,
                proposal_identity=f"identity:{prefix}:sibling",
                action_type="api_key_revocation",
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
            checkpoint_id=f"checkpoint_{prefix}_sibling",
            checkpoint_hash="d" * 64,
            outcome="interrupted",
            state={"segment_events": []},
            proposal_id=proposal_id,
        )
        marker_id = marker.id

    worker_factory = async_sessionmaker(harness.worker, expire_on_commit=False)
    async with worker_factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        approval = await SegmentRepository(session).finalize_interrupt(
            lease,
            marker_id=marker_id,
            proposal_id=proposal_id,
            test_capability=issue_test_runtime_capability(testing=True),
        )
        assert approval.ticket_id == ticket_id
        assert approval.run_id == accepted.run_id
        assert approval.action_type == "api_key_revocation"
        assert approval.status == "pending"
        return approval.id, accepted.run_id


async def _seed_two_active_approvals(
    harness: _Harness,
    *,
    prefix: str,
) -> _ApprovalPair:
    target_approval_id, ticket_id = await _seed_pending_approval(
        harness.factory,
        f"{prefix}_target",
        action_type="refund",
    )
    sibling_approval_id, sibling_run_id = await _seed_api_key_sibling_on_ticket(
        harness,
        prefix=prefix,
        ticket_id=ticket_id,
    )
    async with harness.factory() as session:
        target = await session.get(ApprovalRequest, target_approval_id)
        sibling = await session.get(ApprovalRequest, sibling_approval_id)
        ticket = await session.get(SupportTicket, ticket_id)
        assert target is not None and target.run_id is not None
        assert sibling is not None and sibling.run_id is not None
        assert target.ticket_id == sibling.ticket_id == ticket_id
        assert target.resource_id != sibling.resource_id
        assert target.action_type != sibling.action_type
        assert target.status == sibling.status == "pending"
        assert ticket is not None and ticket.status == "awaiting_approval"
        return _ApprovalPair(
            ticket_id=ticket_id,
            target_approval_id=target.id,
            sibling_approval_id=sibling.id,
            target_run_id=target.run_id,
            sibling_run_id=sibling_run_id,
        )


async def _aggregate_snapshot(
    factory: Any,
    *,
    approval_id: str,
) -> dict[str, object]:
    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None
        proposal = await session.get(ProposalRecord, approval.proposal_id or "")
        run = await session.get(AgentRun, approval.run_id or "")
        turn = await session.get(
            ConversationTurn,
            approval.origin_turn_id,
        )
        decisions = (
            await session.scalars(
                select(HumanDecision.id)
                .where(HumanDecision.approval_id == approval_id)
                .order_by(HumanDecision.id)
            )
        ).all()
        withdrawals = (
            await session.scalars(
                select(ProposalWithdrawal.id)
                .where(ProposalWithdrawal.approval_id == approval_id)
                .order_by(ProposalWithdrawal.id)
            )
        ).all()
        actions = (
            await session.scalars(
                select(BusinessAction.id)
                .where(BusinessAction.approval_id == approval_id)
                .order_by(BusinessAction.id)
            )
        ).all()
        jobs = (
            (
                await session.execute(
                    select(
                        RuntimeJob.id,
                        RuntimeJob.kind,
                        RuntimeJob.status,
                        RuntimeJob.outcome,
                    )
                    .where(RuntimeJob.approval_id == approval_id)
                    .order_by(RuntimeJob.id)
                )
            )
            .tuples()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(
                        AgentEvent.id,
                        AgentEvent.event_type,
                        AgentEvent.status,
                        AgentEvent.visibility,
                        AgentEvent.payload_hash,
                        AgentEvent.event_hash,
                    )
                    .where(AgentEvent.run_id == approval.run_id)
                    .order_by(AgentEvent.ticket_sequence)
                )
            )
            .tuples()
            .all()
        )
        messages = (
            (
                await session.execute(
                    select(
                        TicketMessage.id,
                        TicketMessage.publication_key,
                        TicketMessage.message_kind,
                        TicketMessage.role,
                        TicketMessage.content,
                        TicketMessage.run_id,
                        TicketMessage.turn_id,
                    )
                    .where(TicketMessage.approval_id == approval_id)
                    .order_by(TicketMessage.conversation_sequence)
                )
            )
            .tuples()
            .all()
        )
        return {
            "approval": (
                approval.status,
                approval.status_version,
                approval.action_type,
                approval.resource_type,
                approval.resource_id,
                approval.business_version,
                approval.run_id,
                approval.origin_turn_id,
                approval.selected_revision_id,
                approval.selected_revision_number,
                approval.consumed_at,
            ),
            "proposal": (
                proposal.status,
                proposal.status_version,
            )
            if proposal is not None
            else None,
            "run": (
                run.status,
                run.status_version,
                run.agent_finish_reason,
                run.active_job_id,
                run.active_fencing_token,
                run.completed_at,
            )
            if run is not None
            else None,
            "turn": (
                turn.activity_state,
                turn.result_state,
                turn.completed_at,
            )
            if turn is not None
            else None,
            "decisions": tuple(decisions),
            "withdrawals": tuple(withdrawals),
            "actions": tuple(actions),
            "jobs": tuple(jobs),
            "events": tuple(events),
            "messages": tuple(messages),
        }


async def _assert_sibling_and_ticket_remain_active(
    harness: _Harness,
    *,
    pair: _ApprovalPair,
    sibling_before: dict[str, object],
) -> None:
    sibling_after = await _aggregate_snapshot(
        harness.factory,
        approval_id=pair.sibling_approval_id,
    )
    assert sibling_after == sibling_before
    async with harness.factory() as session:
        sibling = await session.get(ApprovalRequest, pair.sibling_approval_id)
        ticket = await session.get(SupportTicket, pair.ticket_id)
        assert sibling is not None and sibling.status in {"pending", "approved"}
        assert ticket is not None
        has_work = bool(
            await session.scalar(
                select(
                    (
                        select(func.count(ConversationTurn.id))
                        .where(
                            ConversationTurn.ticket_id == pair.ticket_id,
                            ConversationTurn.activity_state.in_({"queued", "running"}),
                        )
                        .scalar_subquery()
                        + select(func.count(RuntimeJob.id))
                        .where(
                            RuntimeJob.ticket_id == pair.ticket_id,
                            RuntimeJob.status.in_({"queued", "retry_wait", "leased"}),
                        )
                        .scalar_subquery()
                    )
                    > 0
                )
            )
        )
        assert ticket.status == ("queued" if has_work else "awaiting_approval")
        await verify_ticket_event_chain(session, pair.ticket_id)


async def _terminal_event_count(
    factory: Any,
    *,
    run_id: str,
    event_type: str,
) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count(AgentEvent.id)).where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.event_type == event_type,
                )
            )
            or 0
        )


@pytest.mark.asyncio
async def test_worker_finalize_atomically_activates_oldest_accepted_turn() -> None:
    """The inner Finalizer transaction must close the handoff crash window.

    ``RuntimeQueueConsumer`` normally calls ``supportguard_worker_finish_job``
    after its handler returns.  This proof stops before that outer call and
    verifies that the worker-only ``supportguard_worker_finalize`` transaction
    has already materialized the oldest accepted Turn as a new Run/Job.
    """

    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_finalize_handoff_{uuid4().hex[:8]}"
    async with _postgres_harness(database_url) as harness:
        run_id = await _seed_run(harness.factory, prefix)
        async with harness.factory() as session, session.begin():
            ticket = await session.get(
                SupportTicket,
                f"ticket_{prefix}",
                with_for_update=True,
            )
            run = await session.get(AgentRun, run_id, with_for_update=True)
            current_message = await session.get(TicketMessage, f"message_{prefix}")
            assert ticket is not None and run is not None and current_message is not None
            current_turn = await session.scalar(
                select(ConversationTurn)
                .where(
                    ConversationTurn.ticket_id == ticket.id,
                    ConversationTurn.customer_message_id == current_message.id,
                )
                .with_for_update()
            )
            assert current_turn is not None
            current_turn.run_id = run.id
            current_turn.activity_state = "queued"
            current_message.message_kind = "customer"
            current_message.conversation_sequence = 1
            ticket.next_message_sequence = 2
            accepted_message = TicketMessage(
                id=f"message_{prefix}_accepted",
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                role="user",
                message_kind="customer",
                content="Please answer this accepted follow-up after the current run.",
                conversation_sequence=2,
            )
            session.add(accepted_message)
            await session.flush()
            accepted_turn = await session.scalar(
                select(ConversationTurn)
                .where(
                    ConversationTurn.ticket_id == ticket.id,
                    ConversationTurn.customer_message_id == accepted_message.id,
                )
                .with_for_update()
            )
            assert accepted_turn is not None
            accepted_turn.activity_state = "accepted"
            accepted_turn.automation_mode = "agent"
            accepted_turn.model = "deepseek-v4-flash"
            accepted_turn.provider_mode = "production"
            accepted_turn.tool_call_mode = "native"
            accepted_turn.context_version = "context.v1.2"
            current_message.turn_id = current_turn.id
            run.turn_id = current_turn.id
            accepted_message.turn_id = accepted_turn.id
            await session.flush()
            jobs = RuntimeJobRepository(session)
            current_job = await jobs.create(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                run_id=run.id,
                kind="agent_start",
            )
            lease = await jobs.claim(
                job_id=current_job.id,
                owner=f"worker-{prefix}",
            )
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
                state=_final_state(run.id),
            )
            current_job_id = current_job.id
            current_turn_id = current_turn.id
            accepted_turn_id = accepted_turn.id
            marker_id = marker.id
            customer_id = ticket.customer_id
            ticket_id = ticket.id

        worker_factory = async_sessionmaker(harness.worker, expire_on_commit=False)
        execution_context = WorkerExecutionContext(
            tenant_id=lease.tenant_id,
            actor_principal_id=customer_id,
            executor_service_principal=lease.owner,
            customer_id=customer_id,
            ticket_id=ticket_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=marker_id,
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            trace_id=f"finalizer:{prefix}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with worker_execution_context.bind(execution_context):
            async with worker_factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                )
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )
                # Intentionally do not call supportguard_worker_finish_job here.

        async with harness.factory() as session:
            stored_ticket = await session.get(SupportTicket, f"ticket_{prefix}")
            stored_run = await session.get(AgentRun, run_id)
            stored_job = await session.get(RuntimeJob, current_job_id)
            stored_current_turn = await session.get(ConversationTurn, current_turn_id)
            stored_accepted_turn = await session.get(ConversationTurn, accepted_turn_id)
            assert stored_ticket is not None and stored_ticket.status == "queued"
            assert stored_run is not None and stored_run.status == "completed"
            assert stored_job is not None and stored_job.status == "succeeded"
            assert stored_current_turn is not None
            assert stored_current_turn.activity_state == "completed"
            assert stored_accepted_turn is not None
            assert stored_accepted_turn.activity_state == "queued"
            assert stored_accepted_turn.run_id is not None
            activated_run = await session.get(AgentRun, stored_accepted_turn.run_id)
            activated_jobs = (
                await session.scalars(
                    select(RuntimeJob).where(
                        RuntimeJob.run_id == stored_accepted_turn.run_id,
                        RuntimeJob.ticket_id == stored_ticket.id,
                    )
                )
            ).all()
            assert activated_run is not None and activated_run.status == "queued"
            assert activated_run.turn_id == stored_accepted_turn.id
            assert activated_run.model == "deepseek-v4-flash"
            assert activated_run.provider_mode == "production"
            assert activated_run.tool_call_mode == "native"
            assert len(activated_jobs) == 1
            assert activated_jobs[0].kind == "agent_start"
            assert activated_jobs[0].status == "queued"
            assert activated_jobs[0].dispatch_sequence > stored_job.dispatch_sequence
            await verify_ticket_event_chain(session, stored_ticket.id)


@pytest.mark.asyncio
async def test_rejecting_one_active_approval_preserves_sibling_and_ticket_projection() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_sib_reject_{uuid4().hex[:8]}"
    async with _postgres_harness(database_url) as harness:
        pair = await _seed_two_active_approvals(harness, prefix=prefix)
        sibling_before = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.sibling_approval_id,
        )

        result = await _raw_decide_and_commit(
            harness.api_factory,
            prefix=prefix,
            approval_id=pair.target_approval_id,
            decision="reject",
            idempotency_key=f"reject-{prefix}",
        )
        assert result["decision"] == "reject"

        async with harness.factory() as session:
            target = await session.get(ApprovalRequest, pair.target_approval_id)
            proposal = await session.get(ProposalRecord, target.proposal_id if target else "")
            run = await session.get(AgentRun, pair.target_run_id)
            turn = await session.get(
                ConversationTurn,
                target.origin_turn_id if target else "",
            )
            action_updates = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.approval_id == pair.target_approval_id,
                        TicketMessage.publication_key
                        == f"approval:{pair.target_approval_id}:rejected",
                    )
                )
                or 0
            )
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == pair.target_approval_id
                    )
                )
                or 0
            )
            assert target is not None and target.status == "rejected"
            assert proposal is not None and proposal.status == "stale"
            assert run is not None and run.status == "completed"
            assert run.agent_finish_reason == "rejected"
            assert turn is not None
            assert turn.activity_state == "completed"
            assert turn.result_state == "refused"
            assert action_updates == 1
            assert action_count == 0
        assert (
            await _terminal_event_count(
                harness.factory,
                run_id=pair.target_run_id,
                event_type="human_decision_accepted",
            )
            == 1
        )
        await _assert_sibling_and_ticket_remain_active(
            harness,
            pair=pair,
            sibling_before=sibling_before,
        )


@pytest.mark.asyncio
async def test_withdrawing_one_active_approval_preserves_sibling_and_ticket_projection() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_sib_withdraw_{uuid4().hex[:8]}"
    async with _postgres_harness(database_url) as harness:
        pair = await _seed_two_active_approvals(harness, prefix=prefix)
        sibling_before = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.sibling_approval_id,
        )

        accepted = await _withdraw(
            harness.api_factory,
            prefix=prefix,
            approval_id=pair.target_approval_id,
            idempotency_key=f"withdraw-{prefix}",
        )
        assert accepted.approval_id == pair.target_approval_id

        async with harness.factory() as session:
            target = await session.get(ApprovalRequest, pair.target_approval_id)
            proposal = await session.get(ProposalRecord, target.proposal_id if target else "")
            run = await session.get(AgentRun, pair.target_run_id)
            turn = await session.get(
                ConversationTurn,
                target.origin_turn_id if target else "",
            )
            action_updates = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.approval_id == pair.target_approval_id,
                        TicketMessage.publication_key
                        == f"action:{pair.target_approval_id}:withdrawn:1",
                    )
                )
                or 0
            )
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == pair.target_approval_id
                    )
                )
                or 0
            )
            assert target is not None and target.status == "withdrawn"
            assert proposal is not None and proposal.status == "stale"
            assert run is not None and run.status == "completed"
            assert run.agent_finish_reason == "withdrawn"
            assert turn is not None
            assert turn.activity_state == "completed"
            assert turn.result_state == "refused"
            assert action_updates == 1
            assert action_count == 0
        assert (
            await _terminal_event_count(
                harness.factory,
                run_id=pair.target_run_id,
                event_type="proposal_withdrawn",
            )
            == 1
        )
        await _assert_sibling_and_ticket_remain_active(
            harness,
            pair=pair,
            sibling_before=sibling_before,
        )


@pytest.mark.asyncio
async def test_reconciled_execute_success_preserves_sibling_and_stops_candidate_churn() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_sib_reconcile_{uuid4().hex[:8]}"
    fixture = await _prepare_unknown_action_effect(
        database_url,
        prefix=f"{prefix}_target",
        action_type="refund",
        evidence="executed",
    )
    async with _postgres_harness(database_url) as harness:
        sibling_approval_id, sibling_run_id = await _seed_api_key_sibling_on_ticket(
            harness,
            prefix=prefix,
            ticket_id=fixture.ticket_id,
        )
        pair = _ApprovalPair(
            ticket_id=fixture.ticket_id,
            target_approval_id=fixture.approval_id,
            sibling_approval_id=sibling_approval_id,
            target_run_id=fixture.run_id,
            sibling_run_id=sibling_run_id,
        )
        sibling_before = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.sibling_approval_id,
        )
        target_before = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.target_approval_id,
        )
        reconciler = create_async_engine(
            _role_url(
                database_url,
                username="supportguard_reconciler",
                password="supportguard_reconciler",  # noqa: S106
            )
        )
        try:
            runner = ActionEffectReconciliationRunner(
                async_sessionmaker(reconciler, expire_on_commit=False)
            )
            report = await runner.reconcile_candidates(
                [
                    {
                        "job_id": fixture.job_id,
                        "job_status": "succeeded",
                        "status_version": fixture.job_status_version,
                    }
                ]
            )
            assert report.handled_job_ids == (fixture.job_id,)
            assert report.resolved_executed == 1

            target_after_reconcile = await _aggregate_snapshot(
                harness.factory,
                approval_id=pair.target_approval_id,
            )
            sibling_after_reconcile = await _aggregate_snapshot(
                harness.factory,
                approval_id=pair.sibling_approval_id,
            )
            async with harness.factory() as session:
                ticket_after_reconcile = await session.get(
                    SupportTicket,
                    pair.ticket_id,
                )
                job_after_reconcile = await session.get(RuntimeJob, fixture.job_id)
                assert ticket_after_reconcile is not None
                assert job_after_reconcile is not None
                convergence_snapshot = (
                    ticket_after_reconcile.status,
                    ticket_after_reconcile.version,
                    ticket_after_reconcile.final_response,
                    job_after_reconcile.status,
                    job_after_reconcile.status_version,
                    job_after_reconcile.outcome,
                    job_after_reconcile.available_at,
                    job_after_reconcile.terminal_at,
                )
            candidate_scans: list[set[str]] = []
            async with reconciler.connect() as connection:
                for _ in range(2):
                    candidate_scans.append(
                        {
                            str(row["job_id"])
                            for row in (
                                (
                                    await connection.execute(
                                        text(
                                            "SELECT * FROM "
                                            "supportguard_reconciler_candidates(500)"
                                        )
                                    )
                                )
                                .mappings()
                                .all()
                            )
                        }
                    )
            assert all(fixture.job_id not in scan for scan in candidate_scans)

            assert (
                await _aggregate_snapshot(
                    harness.factory,
                    approval_id=pair.target_approval_id,
                )
                == target_after_reconcile
            )
            assert (
                await _aggregate_snapshot(
                    harness.factory,
                    approval_id=pair.sibling_approval_id,
                )
                == sibling_after_reconcile
            )
            async with harness.factory() as session:
                ticket_after_scans = await session.get(SupportTicket, pair.ticket_id)
                job_after_scans = await session.get(RuntimeJob, fixture.job_id)
                assert ticket_after_scans is not None
                assert job_after_scans is not None
                assert (
                    ticket_after_scans.status,
                    ticket_after_scans.version,
                    ticket_after_scans.final_response,
                    job_after_scans.status,
                    job_after_scans.status_version,
                    job_after_scans.outcome,
                    job_after_scans.available_at,
                    job_after_scans.terminal_at,
                ) == convergence_snapshot
        finally:
            await reconciler.dispose()

        async with harness.factory() as session:
            target = await session.get(ApprovalRequest, pair.target_approval_id)
            proposal = await session.get(ProposalRecord, fixture.proposal_id)
            run = await session.get(AgentRun, pair.target_run_id)
            job = await session.get(RuntimeJob, fixture.job_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == pair.target_approval_id
                    )
                )
                or 0
            )
            target_messages = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.approval_id == pair.target_approval_id,
                    )
                )
                or 0
            )
            assert target is not None and target.status == "executed"
            assert proposal is not None and proposal.status == "stale"
            assert run is not None and run.status == "completed"
            assert job is not None and job.outcome == "verification_executed"
            assert action_count == fixture.action_count == 1
            assert target_messages >= 1
        target_after = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.target_approval_id,
        )
        assert target_after["messages"] == target_before["messages"]
        assert set(target_before["events"]) <= set(target_after["events"])
        assert (
            await _terminal_event_count(
                harness.factory,
                run_id=pair.target_run_id,
                event_type="action_effect_authority_observed",
            )
            == 1
        )
        await _assert_sibling_and_ticket_remain_active(
            harness,
            pair=pair,
            sibling_before=sibling_before,
        )


@pytest.mark.asyncio
async def test_dead_approval_resume_preserves_sibling_and_ticket_projection() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_sib_dead_{uuid4().hex[:8]}"
    async with _postgres_harness(database_url) as harness:
        pair = await _seed_two_active_approvals(harness, prefix=prefix)
        sibling_before = await _aggregate_snapshot(
            harness.factory,
            approval_id=pair.sibling_approval_id,
        )
        accepted = await _approve(
            harness.api_factory,
            prefix=prefix,
            approval_id=pair.target_approval_id,
        )

        terminal: dict[str, object] | None = None
        for attempt in range(1, 10):
            async with harness.factory() as session, session.begin():
                await session.execute(
                    update(RuntimeJob)
                    .where(RuntimeJob.id == accepted.job_id)
                    .values(available_at=datetime.now(UTC) - timedelta(seconds=5))
                )
                lease = await RuntimeJobRepository(session).claim(
                    job_id=accepted.job_id,
                    owner=f"worker-{prefix}",
                )
            async with harness.worker.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fencing_token,:outcome)"
                    ),
                    {
                        "job_id": accepted.job_id,
                        "owner": f"worker-{prefix}",
                        "fencing_token": lease.fencing_token,
                        "outcome": f"failed:sibling_fixture_{attempt}",
                    },
                )
            assert isinstance(result, dict)
            if result.get("status") == "dead":
                terminal = result
                break
            assert result.get("status") == "retry_wait"
        assert terminal is not None

        async with harness.factory() as session:
            target = await session.get(ApprovalRequest, pair.target_approval_id)
            proposal = await session.get(ProposalRecord, target.proposal_id if target else "")
            run = await session.get(AgentRun, pair.target_run_id)
            turn = await session.get(
                ConversationTurn,
                target.origin_turn_id if target else "",
            )
            job = await session.get(RuntimeJob, accepted.job_id)
            failure_messages = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.approval_id == pair.target_approval_id,
                        TicketMessage.publication_key
                        == f"runtime-failure:{accepted.job_id}",
                    )
                )
                or 0
            )
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == pair.target_approval_id
                    )
                )
                or 0
            )
            assert target is not None and target.status == "failed"
            assert proposal is not None and proposal.status == "stale"
            assert run is not None and run.status == "failed"
            assert run.agent_finish_reason == "infrastructure_exhausted"
            assert run.completed_at is not None
            assert turn is not None
            assert turn.activity_state == "failed"
            assert turn.result_state == "failed"
            assert turn.completed_at is not None
            assert job is not None and job.status == "dead"
            assert failure_messages == 1
            assert action_count == 0
        assert (
            await _terminal_event_count(
                harness.factory,
                run_id=pair.target_run_id,
                event_type="runtime_failed",
            )
            == 1
        )
        await _assert_sibling_and_ticket_remain_active(
            harness,
            pair=pair,
            sibling_before=sibling_before,
        )
