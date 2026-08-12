from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import verify_ticket_event_chain
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalActionRevision,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    HumanDecision,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_postgres_finalizer_faults import (
    _approver_scope,
    _checkpoint_action_resume_intent,
    _final_state,
    _finalize_as_worker,
    _seed_pending_approval,
    _seed_run,
)
from test_v1512_runtime_action_binding_postgres import (
    _seed_production_shaped_pending_approval_fixture,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _database_url() -> str:
    url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    return url


def _role_url(url: str, role: str) -> str:
    return (
        make_url(url)
        .set(username=role, password=role)  # noqa: S106
        .render_as_string(hide_password=False)
    )


@pytest.mark.parametrize(
    ("terminal_state", "finish_reason"),
    [
        ("resolved", "answered"),
        ("needs_clarification", "needs_clarification"),
    ],
)
async def test_reconciler_preserves_finalizer_proven_agent_terminal_state(
    terminal_state: str,
    finish_reason: str,
) -> None:
    url = _database_url()
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"v141_terminal_{terminal_state}_{uuid4().hex[:8]}"
    try:
        run_id = await _seed_run(factory, prefix)
        async with factory() as session, session.begin():
            job = await RuntimeJobRepository(session).create(
                tenant_id="tenant_demo", run_id=run_id, kind="agent_start"
            )
            lease = await RuntimeJobRepository(session).claim(
                job_id=job.id, owner=f"worker-{prefix}"
            )
            marker = await SegmentRepository(session).prepare(
                lease,
                delivery_generation=1,
                segment_kind="agent_start",
                segment_input={"kind": "agent_start"},
            )
            state = _final_state(run_id)
            state["agent_finish_reason"] = finish_reason
            final = dict(state["final"])  # type: ignore[arg-type]
            final["terminal_state"] = terminal_state
            state["final"] = final
            await SegmentRepository(session).checkpoint_written(
                lease,
                marker_id=marker.id,
                checkpoint_id=f"checkpoint_{prefix}",
                checkpoint_hash="a" * 64,
                outcome="completed",
                state=state,
            )
            job_id = job.id
            marker_id = marker.id

        await _finalize_as_worker(
            url,
            lease,
            marker_id,
            customer_id="cust_demo",
            ticket_id=f"ticket_{prefix}",
            run_id=run_id,
            trace_id=f"v141-terminal:{prefix}",
        )

        worker = create_async_engine(_role_url(url, "supportguard_worker"))
        try:
            async with worker.begin() as connection:
                handoff = await connection.scalar(
                    text(
                        "SELECT supportguard_worker_finish_job("
                        ":job_id,:owner,:fence,'completed')"
                    ),
                    {"job_id": job_id, "owner": lease.owner, "fence": lease.fencing_token},
                )
            assert isinstance(handoff, dict) and handoff["outcome"] == "completed"
        finally:
            await worker.dispose()

        # Model drift after the worker's atomic Finalizer commit. Reconciliation
        # must restore the proven terminal state, not invent a generic success.
        async with factory() as session, session.begin():
            job = await session.get(RuntimeJob, job_id, with_for_update=True)
            run = await session.get(AgentRun, run_id, with_for_update=True)
            assert job is not None and run is not None
            ticket = await session.get(SupportTicket, run.ticket_id, with_for_update=True)
            assert ticket is not None
            run.status = "queued"
            ticket.status = "queued"
            expected_version = job.status_version

        reconciler = create_async_engine(_role_url(url, "supportguard_reconciler"))
        try:
            async with reconciler.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT supportguard_reconciler_prepare("
                        ":job_id,:version,'delivery_recovery')"
                    ),
                    {"job_id": job_id, "version": expected_version},
                )
        finally:
            await reconciler.dispose()

        assert isinstance(result, dict)
        assert result["result"] == "terminal_reconciled"
        assert result["ticket_status"] == terminal_state
        async with factory() as session:
            stored_run = await session.get(AgentRun, run_id)
            assert stored_run is not None
            stored_ticket = await session.scalar(
                select(SupportTicket).where(SupportTicket.id == stored_run.ticket_id)
            )
            assert stored_run.status == "completed"
            assert stored_ticket is not None and stored_ticket.status == terminal_state
    finally:
        await engine.dispose()


async def test_edited_refund_uses_immutable_base_and_selected_revision_end_to_end() -> None:
    url = _database_url()
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"v141_edited_{uuid4().hex[:10]}"
    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        admin_factory,
        prefix,
        action_type="refund",
    )
    api = create_async_engine(_role_url(url, "supportguard_api"))
    worker = create_async_engine(_role_url(url, "supportguard_worker"))
    api_factory = create_scoped_session_factory(api)
    try:
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="edit_and_approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{prefix}",
                reason="Only the human-readable refund reason changed.",
                approver_note="v1.4.1 immutable-base regression",
                edited_payload={
                    "refund_reason": "Human review confirmed one duplicate charge."
                },
                trace_id=f"trace-{prefix}",
            )
            await session.commit()

        async with admin_factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.run_id
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            revision = await session.get(
                ApprovalActionRevision, approval.selected_revision_id or ""
            )
            assert proposal is not None and revision is not None
            assert approval.action_hash == proposal.action_hash
            assert approval.action_hash != revision.action_hash
            assert approval.selected_revision_number == revision.revision_number == 1
            lease = await RuntimeJobRepository(session).claim(
                job_id=accepted.job_id,
                owner=f"worker-{prefix}",
            )
            marker_id = await _checkpoint_action_resume_intent(
                session,
                approval=approval,
                lease=lease,
                label=prefix,
                validated_answer=validated_answer,
            )
            billing_id = str(revision.action_payload["billing_record_id"])
            run_id = approval.run_id
            customer_id = approval.customer_id

        await _finalize_as_worker(
            url,
            lease,
            marker_id,
            customer_id=customer_id,
            ticket_id=ticket_id,
            run_id=run_id,
            trace_id=f"trace-execute-{prefix}",
        )
        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fence,'completed')"
                ),
                {
                    "job_id": lease.job_id,
                    "owner": lease.owner,
                    "fence": lease.fencing_token,
                },
            )
        assert isinstance(handoff, dict) and handoff["outcome"] == "completed"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            billing = await session.get(BillingRecord, billing_id)
            action = await session.scalar(
                select(BusinessAction).where(
                    BusinessAction.approval_id == approval_id,
                    BusinessAction.status == "succeeded",
                )
            )
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            assert approval is not None and approval.status == "executed"
            assert billing is not None and billing.status == "refunded"
            assert action is not None and decision is not None
            assert action.action_hash == decision.action_hash
            assert action.action_revision_id == approval.selected_revision_id
            await verify_ticket_event_chain(session, ticket_id)
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


async def test_preexecuted_stale_resume_is_rejected_before_finalizer() -> None:
    url = _database_url()
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(url, "supportguard_api"))
    api_factory = create_scoped_session_factory(api)
    prefix = f"v141_stale_head_{uuid4().hex[:10]}"
    try:
        (
            approval_id,
            _ticket_id,
            _validated_answer,
        ) = await _seed_production_shaped_pending_approval_fixture(
            factory,
            prefix,
            action_type="refund",
        )
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{prefix}",
                reason="Exercise the pre-executed result rejection boundary.",
                approver_note="No effect may precede the Finalizer.",
                trace_id=f"trace-{prefix}",
            )
            await session.commit()
        async with factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id, with_for_update=True)
            assert approval is not None and approval.run_id and approval.selected_revision_id
            run = await session.get(AgentRun, approval.run_id, with_for_update=True)
            assert run is not None
            lease = await RuntimeJobRepository(session).claim(
                job_id=accepted.job_id,
                owner=f"worker-{prefix}",
            )
            marker = await SegmentRepository(session).prepare(
                lease,
                delivery_generation=1,
                segment_kind="approval_resume",
                segment_input={"approval_id": approval.id},
            )
            state = _final_state(run.id)
            state["human_decision"] = {
                "approval_id": approval.id,
                "action": "approve",
            }
            state["execution_result"] = {
                "status": "stale",
                "reason": "resource_snapshot_stale",
            }
            with pytest.raises(
                RuntimeConflict,
                match="preexecuted_approval_resume_not_allowed",
            ):
                await SegmentRepository(session).checkpoint_written(
                    lease,
                    marker_id=marker.id,
                    checkpoint_id=f"checkpoint-{prefix}",
                    checkpoint_hash="e" * 64,
                    outcome="completed",
                    state=state,
                    approval_id=approval.id,
                )
    finally:
        await api.dispose()
        await admin.dispose()


async def test_reconciler_deadline_converges_approved_resume_without_crashing() -> None:
    url = _database_url()
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(url, "supportguard_api"))
    api_factory = create_scoped_session_factory(api)
    reconciler = create_async_engine(_role_url(url, "supportguard_reconciler"))
    prefix = f"v141_reconciler_approval_cas_{uuid4().hex[:10]}"
    try:
        approval_id, ticket_id = await _seed_pending_approval(admin_factory, prefix)
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{prefix}",
                reason="The immutable refund proposal is ready for execution.",
                approver_note="Reconciler Approval CAS regression",
                trace_id=f"trace-{prefix}",
            )
            await session.commit()

        async with admin_factory() as session:
            job = await session.get(RuntimeJob, accepted.job_id)
            approval = await session.get(ApprovalRequest, approval_id)
            assert job is not None and approval is not None
            assert job.status == "queued" and job.status_version == 1
            assert approval.status == "approved" and approval.status_version == 2
            expected_job_version = job.status_version

        async with reconciler.begin() as connection:
            prepared = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare("
                    ":job_id,:job_version,'delivery_recovery')"
                ),
                {
                    "job_id": accepted.job_id,
                    "job_version": expected_job_version,
                },
            )
        assert isinstance(prepared, dict) and prepared["result"] == "prepared"

        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE reconcile_intents SET expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE id=:intent_id"
                ),
                {"intent_id": prepared["intent_id"]},
            )

        observation = {
            "schema_version": "redis-delivery-observation.v1",
            "intent_id": prepared["intent_id"],
            "observation_nonce": prepared["observation_nonce"],
            "job_id": accepted.job_id,
            "outbox_id": prepared["outbox_id"],
            "delivery_generation": prepared["delivery_generation"],
            "runner_nonce": uuid4().hex,
            "observed_at": datetime.now(UTC).isoformat(),
            "status": "unknown",
            "error_code": "redis_timeout",
        }
        async with reconciler.begin() as connection:
            result = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_repair("
                    ":job_id,:job_version,:intent_id,CAST(:observation AS jsonb))"
                ),
                {
                    "job_id": accepted.job_id,
                    "job_version": expected_job_version,
                    "intent_id": prepared["intent_id"],
                    "observation": json.dumps(
                        observation,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            )
        assert result == "dead"

        async with admin_factory() as session:
            job = await session.get(RuntimeJob, accepted.job_id)
            approval = await session.get(ApprovalRequest, approval_id)
            run = await session.get(AgentRun, accepted.run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            assert job is not None and job.status == "dead"
            assert job.last_error == "delivery_state_unknown_deadline"
            assert approval is not None and approval.status == "failed"
            assert approval.status_version == 3
            assert run is not None and run.status == "failed"
            assert ticket is not None and ticket.status == "failed"
    finally:
        await reconciler.dispose()
        await api.dispose()
        await admin.dispose()


async def test_publication_preflight_failure_is_fail_closed_and_zero_write() -> None:
    url = _database_url()
    admin = create_async_engine(url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(url, "supportguard_api"))
    api_factory = create_scoped_session_factory(api)
    prefix = f"v159_publication_stale_{uuid4().hex[:10]}"
    try:
        (
            approval_id,
            ticket_id,
            _validated_answer,
        ) = await _seed_production_shaped_pending_approval_fixture(
            factory,
            prefix,
            action_type="refund",
        )
        async with api_factory.request(_approver_scope(prefix)) as session:
            await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{prefix}",
                reason="Publication binding must be revalidated.",
                approver_note="Exercise stale publication convergence.",
                trace_id=f"trace-{prefix}",
            )
            await session.commit()

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            head_before = await session.scalar(
                select(AgentEvent)
                .where(AgentEvent.ticket_id == ticket_id)
                .order_by(AgentEvent.ticket_sequence.desc())
                .limit(1)
            )
            assert head_before is not None
            head_identity_before = (
                head_before.id,
                head_before.ticket_sequence,
                head_before.event_hash,
            )
            result = await ApprovalCoordinator._stale_response(  # noqa: SLF001
                session, approval
            )

        assert result["status"] == "execution_precondition_failed"
        assert result["execution_state"] == "verification_pending"
        assert result["effect_status"] == "not_attempted"
        assert result["reason"] == "publication_binding_stale"
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            head = await session.scalar(
                select(AgentEvent)
                .where(AgentEvent.ticket_id == ticket_id)
                .order_by(AgentEvent.ticket_sequence.desc())
                .limit(1)
            )
            assert approval is not None and approval.status == "approved"
            assert head is not None
            assert (head.id, head.ticket_sequence, head.event_hash) == head_identity_before
            await verify_ticket_event_chain(session, ticket_id)
    finally:
        await api.dispose()
        await admin.dispose()
