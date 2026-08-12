from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.context import worker_execution_context
from supportguard.contracts.finalizer import canonical_hash
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    HumanDecision,
    RuntimeJob,
    SupportTicket,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.runtime_jobs import RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_postgres_finalizer_faults import (
    _final_state,
    _finalize_as_worker,
    _seed_pending_approval,
    _seed_run,
)
from test_v1512_runtime_action_binding_postgres import _prepare

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _role_url(url: str, role: str) -> str:
    return (
        make_url(url)
        .set(username=role, password=role)  # noqa: S106
        .render_as_string(hide_password=False)
    )


async def _finish_job(url: str, job_id: str, owner: str, fence: int) -> dict[str, Any]:
    worker = create_async_engine(_role_url(url, "supportguard_worker"))
    try:
        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text("SELECT supportguard_worker_finish_job(:job_id,:owner,:fence,'completed')"),
                {"job_id": job_id, "owner": owner, "fence": fence},
            )
        assert isinstance(handoff, dict)
        return handoff
    finally:
        await worker.dispose()


async def _repair_terminal(
    url: str,
    factory: async_sessionmaker[AsyncSession],
    *,
    job_id: str,
    approval_id: str | None = None,
) -> dict[str, Any]:
    async with factory() as session, session.begin():
        job = await session.get(RuntimeJob, job_id, with_for_update=True)
        assert job is not None
        run = await session.get(AgentRun, job.run_id, with_for_update=True)
        assert run is not None
        ticket = await session.get(SupportTicket, run.ticket_id, with_for_update=True)
        assert ticket is not None
        run.status = "queued"
        ticket.status = "queued"
        expected_version = job.status_version
        action_count_before = int(
            await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == (approval_id or "")
                )
            )
            or 0
        )

    reconciler = create_async_engine(_role_url(url, "supportguard_reconciler"))
    try:
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare(:job_id,:version,'delivery_recovery')"
                ),
                {"job_id": job_id, "version": expected_version},
            )
    finally:
        await reconciler.dispose()
    assert isinstance(repaired, dict)

    async with factory() as session:
        job = await session.get(RuntimeJob, job_id)
        assert job is not None
        run = await session.get(AgentRun, job.run_id)
        assert run is not None
        ticket = await session.get(SupportTicket, run.ticket_id)
        assert ticket is not None
        approval = await session.get(ApprovalRequest, approval_id or "")
        marker_rows = (
            (
                await session.execute(
                    select(CheckpointCommitMarker.status)
                    .where(CheckpointCommitMarker.job_id == job_id)
                    .order_by(CheckpointCommitMarker.created_at, CheckpointCommitMarker.id)
                )
            )
            .scalars()
            .all()
        )
        action_count_after = int(
            await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == (approval_id or "")
                )
            )
            or 0
        )
        return {
            "disposition": repaired["result"],
            "job_status": job.status,
            "job_outcome": job.outcome,
            "lease_cleared": job.lease_owner is None,
            "run_status": run.status,
            "run_active_job_cleared": run.active_job_id is None,
            "ticket_status": ticket.status,
            "approval_status": approval.status if approval is not None else None,
            "marker_statuses": list(marker_rows),
            "action_count_before": action_count_before,
            "action_count_after": action_count_after,
        }


async def _completed_agent_start(
    url: str, factory: async_sessionmaker[AsyncSession], prefix: str
) -> dict[str, Any]:
    run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo", run_id=run_id, kind="agent_start"
        )
        lease = await RuntimeJobRepository(session).claim(job_id=job.id, owner=f"worker-{prefix}")
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
            checkpoint_hash="a" * 64,
            outcome="completed",
            state=_final_state(run_id),
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
        trace_id=f"terminal-matrix:{prefix}",
    )
    handoff = await _finish_job(url, job_id, lease.owner, lease.fencing_token)
    assert handoff["outcome"] == "completed"
    return await _repair_terminal(url, factory, job_id=job_id)


async def _interrupted_agent_start(
    url: str, factory: async_sessionmaker[AsyncSession], prefix: str
) -> dict[str, Any]:
    approval_id, _ = await _seed_pending_approval(factory, prefix)
    async with factory() as session:
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.marker_id
        marker = await session.get(CheckpointCommitMarker, approval.marker_id)
        assert marker is not None
        job_id = marker.job_id
    return await _repair_terminal(url, factory, job_id=job_id, approval_id=approval_id)


async def _approval_resume(
    url: str,
    factory: async_sessionmaker[AsyncSession],
    prefix: str,
    *,
    decision_name: str,
    execution_status: str,
) -> tuple[dict[str, Any], str, str]:
    if decision_name == "approve" and execution_status == "succeeded":
        return await _approval_resume_actionful(url, prefix)
    if decision_name == "approve" and execution_status == "stale":
        return await _approval_resume_actionful_stale(url, prefix)
    if decision_name != "approve":
        raise ValueError("no-action reject is converged by the API without a resume job")
    approval_id, _ = await _seed_pending_approval(factory, prefix)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        approval = await session.get(ApprovalRequest, approval_id, with_for_update=True)
        assert approval is not None and approval.run_id and approval.selected_revision_id
        run = await session.get(AgentRun, approval.run_id, with_for_update=True)
        assert run is not None
        decision = HumanDecision(
            tenant_id="tenant_demo",
            approval_id=approval.id,
            action_revision_id=approval.selected_revision_id,
            actor_id="user_approver_demo",
            decision=decision_name,
            reason=f"terminal matrix {decision_name}",
            action_hash=approval.action_hash,
            decision_hash=canonical_hash(
                {"approval_id": approval.id, "decision": decision_name, "fixture": prefix}
            ),
        )
        session.add(decision)
        await session.flush()
        event = await AgentRunStore(session).append_event(
            run,
            event_type="human_decision_accepted",
            payload={
                "approval_id": approval.id,
                "human_decision_id": decision.id,
                "decision": decision.decision,
                "decision_hash": decision.decision_hash,
                "action_hash": decision.action_hash,
            },
            visibility="approver",
            expected_ticket_head_event_id=approval.expected_ticket_head_event_id,
            expected_ticket_sequence=approval.expected_ticket_sequence,
            expected_ticket_event_hash=approval.expected_ticket_event_hash,
        )
        decision.canonical_event_id = event.id
        decision.canonical_event_hash = event.event_hash
        approval.status = "approved"
        approval.decided_at = datetime.now(UTC)
        run.status = "queued"
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo",
            run_id=run.id,
            kind="approval_resume",
            approval_id=approval.id,
        )
        lease = await RuntimeJobRepository(session).claim(job_id=job.id, owner=f"worker-{prefix}")
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="approval_resume",
            segment_input={"approval_id": approval.id, "decision_id": decision.id},
        )
        state = _final_state(run.id)
        final = dict(state["final"])  # type: ignore[arg-type]
        final["terminal_state"] = "resolved" if execution_status == "succeeded" else "failed"
        state["final"] = final
        state["human_decision"] = {"approval_id": approval.id, "action": decision_name}
        state["execution_result"] = {
            "status": (
                "execution_precondition_failed"
                if execution_status == "logical_degradation"
                else execution_status
            ),
            **({"reason": "fixture_binding_changed"} if execution_status == "stale" else {}),
        }
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_resume_{prefix}",
            checkpoint_hash="b" * 64,
            outcome="completed",
            state=state,
            approval_id=approval.id,
        )
        job_id = job.id
        original_marker_id = approval.marker_id
        marker_id = marker.id
        customer_id = approval.customer_id
        ticket_id = approval.ticket_id
        run_id = run.id

    await _finalize_as_worker(
        url,
        lease,
        marker_id,
        customer_id=customer_id,
        ticket_id=ticket_id,
        run_id=run_id,
        trace_id=f"terminal-matrix:{prefix}",
    )
    handoff = await _finish_job(url, job_id, lease.owner, lease.fencing_token)
    expected_outcome = "completed" if execution_status == "succeeded" else "domain_terminal"
    assert handoff["outcome"] == expected_outcome
    row = await _repair_terminal(url, factory, job_id=job_id, approval_id=approval_id)
    return row, approval_id, str(original_marker_id)


async def _approval_resume_actionful(
    url: str,
    prefix: str,
) -> tuple[dict[str, Any], str, str]:
    (
        admin,
        admin_factory,
        approval_id,
        _resource_id,
        _idempotency_key,
        lease,
        context,
    ) = await _prepare("refund", prefix)
    worker = create_async_engine(_role_url(url, "supportguard_worker"))
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session:
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.tenant_id == lease.tenant_id,
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.segment_kind == "approval_resume",
                )
            )
            approval = await session.get(ApprovalRequest, approval_id)
            assert marker is not None and approval is not None
            original_marker_id = str(approval.marker_id)
        execution_context = replace(context, segment_id=marker.id)
        with worker_execution_context.bind(execution_context):
            async with worker_factory.worker(execution_context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker.id,
                )
                await session.commit()
        async with admin_factory() as session:
            effect_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id,
                        BusinessAction.status == "succeeded",
                    )
                )
                or 0
            )
        assert effect_count == 1
        handoff = await _finish_job(
            url,
            lease.job_id,
            lease.owner,
            lease.fencing_token,
        )
        assert handoff["outcome"] == "completed"
        row = await _repair_terminal(
            url,
            admin_factory,
            job_id=lease.job_id,
            approval_id=approval_id,
        )
        return row, approval_id, original_marker_id
    finally:
        await worker.dispose()
        await admin.dispose()


async def _approval_resume_actionful_stale(
    url: str,
    prefix: str,
) -> tuple[dict[str, Any], str, str]:
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _idempotency_key,
        lease,
        context,
    ) = await _prepare("refund", prefix)
    worker = create_async_engine(_role_url(url, "supportguard_worker"))
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session, session.begin():
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.tenant_id == lease.tenant_id,
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.segment_kind == "approval_resume",
                )
            )
            approval = await session.get(ApprovalRequest, approval_id)
            billing = await session.get(BillingRecord, resource_id, with_for_update=True)
            assert marker is not None and approval is not None and billing is not None
            original_marker_id = str(approval.marker_id)
            billing.version += 1
        execution_context = replace(context, segment_id=marker.id)
        with worker_execution_context.bind(execution_context):
            async with worker_factory.worker(execution_context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker.id,
                )
                await session.commit()
        async with admin_factory() as session:
            effect_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status == "stale"
        assert effect_count == 0
        handoff = await _finish_job(
            url,
            lease.job_id,
            lease.owner,
            lease.fencing_token,
        )
        assert handoff["outcome"] == "domain_terminal"
        row = await _repair_terminal(
            url,
            admin_factory,
            job_id=lease.job_id,
            approval_id=approval_id,
        )
        return row, approval_id, original_marker_id
    finally:
        await worker.dispose()
        await admin.dispose()


async def _unknown_zero_write(
    url: str, factory: async_sessionmaker[AsyncSession], prefix: str
) -> dict[str, Any]:
    run_id = await _seed_run(factory, prefix)
    async with factory() as session, session.begin():
        job = await RuntimeJobRepository(session).create(
            tenant_id="tenant_demo", run_id=run_id, kind="agent_start"
        )
        job.status = "succeeded"
        job.outcome = "completed"
        job.terminal_at = datetime.now(UTC)
        run = await session.get(AgentRun, run_id, with_for_update=True)
        assert run is not None
        ticket = await session.get(SupportTicket, run.ticket_id, with_for_update=True)
        assert ticket is not None
        run.status = "queued"
        ticket.status = "queued"
        await session.flush()
        job_id = job.id
    async with factory() as session:
        persisted_job = await session.get(RuntimeJob, job_id)
        assert persisted_job is not None
        version = persisted_job.status_version
    reconciler = create_async_engine(_role_url(url, "supportguard_reconciler"))
    try:
        async with reconciler.begin() as connection:
            disposition = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_prepare(:job_id,:version,'delivery_recovery')"
                ),
                {"job_id": job_id, "version": version},
            )
    finally:
        await reconciler.dispose()
    async with factory() as session:
        job = await session.get(RuntimeJob, job_id)
        run = await session.get(AgentRun, run_id)
        assert job is not None and run is not None
        ticket = await session.get(SupportTicket, run.ticket_id)
        assert ticket is not None and isinstance(disposition, dict)
        return {
            "disposition": disposition["result"],
            "job_status": job.status,
            "job_outcome": job.outcome,
            "job_status_version_unchanged": job.status_version == version,
            "run_status": run.status,
            "ticket_status": ticket.status,
            "marker_count": int(
                await session.scalar(
                    select(func.count(CheckpointCommitMarker.id)).where(
                        CheckpointCommitMarker.job_id == job_id
                    )
                )
                or 0
            ),
        }


async def test_terminal_outcome_mapping_matrix_is_exact_and_unknown_is_zero_write() -> None:
    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    try:
        interrupted = await _interrupted_agent_start(url, factory, f"matrix_int_{suffix}")
        completed = await _completed_agent_start(url, factory, f"matrix_done_{suffix}")
        actionful, _, _ = await _approval_resume(
            url,
            factory,
            f"matrix_action_{suffix}",
            decision_name="approve",
            execution_status="succeeded",
        )
        binding_stale, _, _ = await _approval_resume(
            url,
            factory,
            f"matrix_stale_{suffix}",
            decision_name="approve",
            execution_status="stale",
        )
        logical_degradation, _, _ = await _approval_resume(
            url,
            factory,
            f"matrix_degrade_{suffix}",
            decision_name="approve",
            execution_status="logical_degradation",
        )
        rows = [
            interrupted,
            completed,
            actionful,
            binding_stale,
            logical_degradation,
        ]
        expected_rows = [
            ("interrupted", "awaiting_approval", "pending", 0),
            ("completed", "resolved", None, 0),
            ("completed", "resolved", "executed", 1),
            ("completed", "failed", "stale", 0),
            ("completed", "failed", "failed", 0),
        ]
        observed_rows = [
            (
                row["run_status"],
                row["ticket_status"],
                row["approval_status"],
                row["action_count_after"],
            )
            for row in rows
        ]
        assert observed_rows == expected_rows, {
            "logical_degradation": logical_degradation,
        }
        assert all(row["disposition"] == "terminal_reconciled" for row in rows)
        assert all(row["lease_cleared"] and row["run_active_job_cleared"] for row in rows)
        assert all(row["action_count_before"] == row["action_count_after"] for row in rows)
        assert all(row["marker_statuses"] == ["finalized"] for row in rows)

        unknown = await _unknown_zero_write(url, factory, f"matrix_unknown_{suffix}")
        assert unknown == {
            "disposition": "terminal_outcome_unrecognized",
            "job_status": "succeeded",
            "job_outcome": "completed",
            "job_status_version_unchanged": True,
            "run_status": "queued",
            "ticket_status": "queued",
            "marker_count": 0,
        }
        record_predicate_operands(
            requirement_id="C6-P0-04",
            predicate_id="terminal_outcome_mapping_exact",
            subject_kind="postgres_terminal_outcome_matrix",
            operands={
                "recognized_row_count": len(rows),
                "recognized_rows": [list(row) for row in observed_rows],
                "all_recognized_dispositions_exact": all(
                    row["disposition"] == "terminal_reconciled" for row in rows
                ),
                "all_terminal_leases_and_pointers_cleared": all(
                    row["lease_cleared"] and row["run_active_job_cleared"] for row in rows
                ),
                "all_action_counts_unchanged": all(
                    row["action_count_before"] == row["action_count_after"] for row in rows
                ),
                "unknown_disposition": unknown["disposition"],
                "unknown_job_status_version_unchanged": unknown["job_status_version_unchanged"],
                "unknown_run_status": unknown["run_status"],
                "unknown_ticket_status": unknown["ticket_status"],
                "unknown_marker_count": unknown["marker_count"],
            },
        )
    finally:
        await engine.dispose()
