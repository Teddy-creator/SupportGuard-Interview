from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from supportguard.agent.persistence import (
    AgentRunStore,
    CanonicalEventHeadConflict,
)
from supportguard.contracts.context import worker_execution_context
from supportguard.contracts.finalizer import (
    ActionfulApprovalResumeDelta,
    canonical_hash,
)
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    ConversationTurn,
    FinalizerPayload,
    HumanDecision,
    OutboxEvent,
    ProposalRecord,
    RuntimeJob,
    Subscription,
    SupportTicket,
    TicketMessage,
    TicketSummary,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.memory.service import MemoryService
from supportguard.runtime.worker import AgentJobHandler
from supportguard.services.action_effect_reconciliation import (
    ActionEffectReconciliationRunner,
)
from supportguard.services.actions import RuntimeActionExecutor
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_postgres_finalizer_faults import _seed_run
from test_v1512_runtime_action_binding_postgres import _prepare

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def _resource_snapshot(
    session: AsyncSession,
    *,
    action_type: str,
    resource_id: str,
) -> dict[str, Any]:
    if action_type == "refund":
        resource = await session.get(BillingRecord, resource_id)
        assert resource is not None
        return {"status": resource.status, "version": resource.version}
    if action_type == "api_key_revocation":
        resource = await session.scalar(
            select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == resource_id)
        )
        assert resource is not None
        return {"status": resource.status, "version": resource.version}
    resource = await session.get(Subscription, resource_id)
    assert resource is not None
    return {
        "concurrency_limit": resource.concurrency_limit,
        "version": resource.version,
    }


async def _publication_counts(
    session: AsyncSession,
    *,
    run_id: str,
    ticket_id: str,
    job_id: str,
) -> dict[str, int]:
    return {
        "events": int(
            await session.scalar(
                select(func.count(AgentEvent.id)).where(AgentEvent.run_id == run_id)
            )
            or 0
        ),
        "messages": int(
            await session.scalar(
                select(func.count(TicketMessage.id)).where(
                    TicketMessage.ticket_id == ticket_id
                )
            )
            or 0
        ),
        "memory": int(
            await session.scalar(
                select(func.count(TicketSummary.id)).where(
                    TicketSummary.ticket_id == ticket_id
                )
            )
            or 0
        ),
        "outbox": int(
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(OutboxEvent.job_id == job_id)
            )
            or 0
        ),
    }


async def test_pre_effect_payload_conflict_converges_complete_safe_terminal() -> None:
    """Confirmed zero-effect corruption cannot leave an active zombie Approval."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        context,
    ) = await _prepare("refund", "prefix_abort")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session, session.begin():
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            approval = await session.get(ApprovalRequest, approval_id)
            assert marker is not None
            assert approval is not None
            payload = await session.scalar(
                select(FinalizerPayload).where(
                    FinalizerPayload.marker_id == marker.id,
                )
            )
            assert payload is not None
            payload.full_payload = {
                **payload.full_payload,
                "payload_hash": "0" * 64,
            }
            marker_id = marker.id
            proposal_id = approval.proposal_id
            ticket_id = approval.ticket_id
            run_id = approval.run_id
            initial_resource = await _resource_snapshot(
                session,
                action_type="refund",
                resource_id=resource_id,
            )

        with pytest.raises(
            RuntimeConflict,
            match="finalizer_payload_hash_mismatch",
        ), worker_execution_context.bind(context):
            async with worker_factory.worker(context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            marker = await session.get(CheckpointCommitMarker, marker_id)
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            turn = (
                await session.get(ConversationTurn, run.turn_id)
                if run is not None and run.turn_id
                else None
            )
            action_count = await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == approval_id
                )
            )
            failure_events = await session.scalar(
                select(func.count(AgentEvent.id)).where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.event_type == "runtime_failed",
                )
            )
            action_updates = await session.scalar(
                select(func.count(TicketMessage.id)).where(
                    TicketMessage.ticket_id == ticket_id,
                    TicketMessage.approval_id == approval_id,
                    TicketMessage.message_kind == "action_update",
                )
            )
            assert approval is not None and approval.status == "failed"
            assert proposal is not None and proposal.status == "stale"
            assert marker is not None and marker.status == "aborted"
            assert job is not None and job.status == "dead"
            assert run is not None and run.status == "failed"
            assert turn is not None and turn.activity_state == "failed"
            assert ticket is not None and ticket.status == "failed"
            assert action_count == 0
            assert failure_events == 1
            assert action_updates == 1
            assert (
                await _resource_snapshot(
                    session,
                    action_type="refund",
                    resource_id=resource_id,
                )
                == initial_resource
            )
    finally:
        await worker.dispose()
        await admin.dispose()


@pytest.mark.parametrize(
    "action_type",
    ("refund", "api_key_revocation", "entitlement_change"),
)
async def test_action_finalizer_commit_unknown_enters_bounded_reconciliation(
    action_type: str,
) -> None:
    """A lost COMMIT ACK preserves Active Identity until joint verification."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        _,
    ) = await _prepare(action_type, "commit_unknown")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    reconciler = create_async_engine(
        make_url(database_url)
        .set(
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
        .render_as_string(hide_password=False)
    )
    reconciler_factory = async_sessionmaker(reconciler, expire_on_commit=False)
    try:
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal_id = approval.proposal_id
            initial_resource = await _resource_snapshot(
                session,
                action_type=action_type,
                resource_id=resource_id,
            )

        async with worker.begin() as connection:
            unknown = await connection.scalar(
                select(
                    func.supportguard_worker_finish_job(
                        lease.job_id,
                        lease.owner,
                        lease.fencing_token,
                        "finalizer_commit_unknown:action_effect",
                    )
                )
            )
        assert isinstance(unknown, dict)
        assert unknown["status"] == "succeeded"
        assert unknown["outcome"] == "verification_pending"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                )
            )
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            ticket = await session.get(SupportTicket, lease.ticket_id)
            assert approval is not None and approval.status == "approved"
            assert proposal is not None and proposal.status == "bound"
            assert marker is not None and marker.status == "finalized"
            assert job is not None and job.status == "succeeded"
            assert job.outcome == "verification_pending"
            assert run is not None and run.status == "interrupted"
            assert ticket is not None and ticket.status == "verification_pending"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 0
            )
            assert (
                await _resource_snapshot(
                    session,
                    action_type=action_type,
                    resource_id=resource_id,
                )
                == initial_resource
            )
            status_version = job.status_version

        report = await ActionEffectReconciliationRunner(
            reconciler_factory,
        ).reconcile_candidates(
            [
                {
                    "job_id": lease.job_id,
                    "job_status": "succeeded",
                    "status_version": status_version,
                }
            ]
        )
        assert report.resolved_zero_effect == 1

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            job = await session.get(RuntimeJob, lease.job_id)
            assert approval is not None and approval.status == "failed"
            assert proposal is not None and proposal.status == "stale"
            assert job is not None and job.outcome == "verified_zero_effect"
            assert (
                await _resource_snapshot(
                    session,
                    action_type=action_type,
                    resource_id=resource_id,
                )
                == initial_resource
            )
    finally:
        await reconciler.dispose()
        await worker.dispose()
        await admin.dispose()


async def test_action_finalizer_commit_ack_loss_reads_committed_terminal_truth() -> None:
    """If COMMIT landed, the unknown classifier never regresses the Action."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        _,
        _,
        lease,
        context,
    ) = await _prepare("refund", "commit_landed")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_scoped = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session:
            marker_id = await session.scalar(
                select(CheckpointCommitMarker.id).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
        assert marker_id is not None
        with worker_execution_context.bind(context):
            async with worker_scoped.worker(context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )
                await session.commit()

        async with worker.begin() as connection:
            unknown = await connection.scalar(
                select(
                    func.supportguard_worker_finish_job(
                        lease.job_id,
                        lease.owner,
                        lease.fencing_token,
                        "finalizer_commit_unknown:action_effect",
                    )
                )
            )
        assert isinstance(unknown, dict)
        assert unknown["status"] == "succeeded"
        assert unknown["outcome"] == "completed"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, lease.job_id)
            assert approval is not None and approval.status == "executed"
            assert job is not None and job.status == "succeeded"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 1
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_non_action_commit_unknown_recovers_finalizer_without_provider_replay() -> None:
    """Ordinary finalizers retry only the committed Marker, never the Provider."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    prefix = f"commit_plain_{os.urandom(4).hex()}"
    run_id = await _seed_run(admin_factory, prefix)
    async with admin_factory() as session, session.begin():
        run = await session.get(AgentRun, run_id)
        assert run is not None
        job = await RuntimeJobRepository(session).create(
            tenant_id=run.tenant_id,
            run_id=run.id,
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
            segment_input={"message_id": run.message_id, "kind": "agent_start"},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint-{prefix}",
            checkpoint_hash="f" * 64,
            outcome="completed",
            state={
                "ticket_id": run.ticket_id,
                "customer_id": run.customer_id,
                "run_id": run.id,
                "trace_id": f"trace-{prefix}",
                "classification": {"issue_type": "product", "risk": "low"},
                "agent_finish_reason": "answered",
                "tool_observations": [],
                "evidence": [],
                "segment_events": [],
                "final": {
                    "answer": "这是一个可由现有证据回答的普通产品问题。",
                    "terminal_state": "resolved",
                    "knowledge_chunk_ids": [],
                    "business_source_ids": [],
                    "material_claims": [],
                    "policy_route": "answer",
                },
            },
        )
        marker_id = marker.id

    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        async with worker.begin() as connection:
            unknown = await connection.scalar(
                select(
                    func.supportguard_worker_finish_job(
                        lease.job_id,
                        lease.owner,
                        lease.fencing_token,
                        "finalizer_commit_unknown:finalizer_only",
                    )
                )
            )
        assert isinstance(unknown, dict)
        assert unknown["status"] == "retry_wait"

        async with admin_factory() as session, session.begin():
            job = await session.get(RuntimeJob, lease.job_id, with_for_update=True)
            run = await session.get(AgentRun, lease.run_id, with_for_update=True)
            marker = await session.get(CheckpointCommitMarker, marker_id)
            assert job is not None and job.status == "retry_wait"
            assert run is not None and run.status == "queued"
            assert marker is not None and marker.status == "checkpoint_written"
            await session.execute(
                text(
                    "UPDATE runtime_jobs "
                    "SET available_at=clock_timestamp()-interval '1 second' "
                    "WHERE id=:job_id"
                ),
                {"job_id": lease.job_id},
            )

        async with admin_factory() as session, session.begin():
            retry_lease = await RuntimeJobRepository(session).claim(
                job_id=lease.job_id,
                owner=f"worker-retry-{prefix}",
            )

        handler = AgentJobHandler(
            worker_factory,
            object(),  # type: ignore[arg-type]
        )
        outcome = await handler._recover_finalizer_only(
            retry_lease,
            marker_id,
        )
        assert outcome == "completed"

        async with admin_factory() as session:
            marker_count = int(
                await session.scalar(
                    select(func.count(CheckpointCommitMarker.id)).where(
                        CheckpointCommitMarker.job_id == lease.job_id,
                        CheckpointCommitMarker.status == "finalized",
                    )
                )
                or 0
            )
            job = await session.get(RuntimeJob, lease.job_id)
            provider_attempts = int(
                await session.scalar(
                    select(func.count(AgentCallAttempt.id)).where(
                        AgentCallAttempt.job_id == lease.job_id,
                    )
                )
                or 0
            )
            assert marker_count == 1
            assert job is not None and job.status == "succeeded"
            assert provider_attempts == 0
    finally:
        await worker.dispose()
        await admin.dispose()


@pytest.mark.parametrize(
    "action_type",
    ("refund", "api_key_revocation", "entitlement_change"),
)
@pytest.mark.parametrize("failure_kind", ("canonical_publication", "memory"))
async def test_post_effect_failure_rolls_back_every_surface_for_safe_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    action_type: str,
) -> None:
    """All three actions share one atomic effect/publication/memory boundary."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        context,
    ) = await _prepare(action_type, f"postfx_{failure_kind[:3]}")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session:
            marker_id = await session.scalar(
                select(CheckpointCommitMarker.id).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            approval = await session.get(ApprovalRequest, approval_id)
            assert marker_id is not None
            assert approval is not None
            ticket_id = approval.ticket_id
            proposal_id = approval.proposal_id
            expected_entitlement_limit = (
                int(approval.action_payload["target"]["concurrency_limit"])
                if action_type == "entitlement_change"
                else None
            )
            baseline_publications = await _publication_counts(
                session,
                run_id=lease.run_id,
                ticket_id=ticket_id,
                job_id=lease.job_id,
            )
            initial_resource = await _resource_snapshot(
                session,
                action_type=action_type,
                resource_id=resource_id,
            )

        original_append_event = AgentRunStore.append_event
        original_persist_summary = MemoryService.persist_summary

        async def fail_final_outcome(
            self: AgentRunStore,
            run: AgentRun,
            *,
            event_type: str,
            **kwargs: object,
        ):
            if event_type == "final_outcome":
                raise CanonicalEventHeadConflict(
                    "injected_post_effect_head_conflict"
                )
            return await original_append_event(
                self,
                run,
                event_type=event_type,
                **kwargs,
            )

        async def persist_then_fail(
            self: MemoryService,
            state: Any,
        ):
            await original_persist_summary(self, state)
            raise RuntimeError("injected_post_effect_memory_failure")

        expected_error = (
            pytest.raises(
                RuntimeConflict,
                match="finalizer_actual_head_conflict",
            )
            if failure_kind == "canonical_publication"
            else pytest.raises(
                RuntimeError,
                match="injected_post_effect_memory_failure",
            )
        )
        with monkeypatch.context() as patch, expected_error:
            if failure_kind == "canonical_publication":
                patch.setattr(AgentRunStore, "append_event", fail_final_outcome)
            else:
                patch.setattr(MemoryService, "persist_summary", persist_then_fail)
            with worker_execution_context.bind(context):
                async with worker_factory.worker(context) as session:
                    await SegmentRepository(session).finalize(
                        lease,
                        marker_id=str(marker_id),
                    )

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            billing = (
                await session.get(BillingRecord, resource_id)
                if action_type == "refund"
                else None
            )
            marker = await session.get(CheckpointCommitMarker, marker_id)
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            proposal = await session.get(ProposalRecord, proposal_id)
            action_count = await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == approval_id
                )
            )
            assert approval is not None and approval.status == "approved"
            assert approval.consumed_at is None
            assert proposal is not None and proposal.status == "bound"
            assert marker is not None and marker.status == "checkpoint_written"
            assert job is not None and job.status == "leased"
            assert run is not None and run.status == "running"
            assert action_count == 0
            assert await _publication_counts(
                session,
                run_id=lease.run_id,
                ticket_id=ticket_id,
                job_id=lease.job_id,
            ) == baseline_publications
            assert (
                await _resource_snapshot(
                    session,
                    action_type=action_type,
                    resource_id=resource_id,
                )
                == initial_resource
            )
            if billing is not None:
                assert billing.status == initial_resource["status"]

        with worker_execution_context.bind(context):
            async with worker_factory.worker(context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=str(marker_id),
                )
                await session.commit()

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            marker = await session.get(CheckpointCommitMarker, marker_id)
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            action_count = await session.scalar(
                select(func.count(BusinessAction.id)).where(
                    BusinessAction.approval_id == approval_id
                )
            )
            final_resource = await _resource_snapshot(
                session,
                action_type=action_type,
                resource_id=resource_id,
            )
            assert approval is not None and approval.status == "executed"
            assert marker is not None and marker.status == "finalized"
            assert job is not None and job.status == "succeeded"
            assert run is not None and run.status == "completed"
            assert action_count == 1
            assert final_resource["version"] == initial_resource["version"] + 1
            if action_type == "refund":
                assert final_resource["status"] == "refunded"
            elif action_type == "api_key_revocation":
                assert final_resource["status"] == "revoked"
            else:
                assert (
                    final_resource["concurrency_limit"]
                    == expected_entitlement_limit
                )
            final_publications = await _publication_counts(
                session,
                run_id=lease.run_id,
                ticket_id=ticket_id,
                job_id=lease.job_id,
            )
            assert final_publications["events"] > baseline_publications["events"]
            assert final_publications["messages"] > baseline_publications["messages"]
            assert final_publications["memory"] >= 1
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_real_worker_resume_commits_checkpoint_before_finalizer_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production handler preserves its only no-Provider recovery entry."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        _,
    ) = await _prepare("refund", "worker_topology")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        async with admin_factory() as session, session.begin():
            old_marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            assert old_marker is not None
            old_payload = await session.scalar(
                select(FinalizerPayload).where(
                    FinalizerPayload.marker_id == old_marker.id,
                )
            )
            assert old_payload is not None
            output = dict(old_payload.state_delta["state"])
            await session.execute(
                delete(FinalizerPayload).where(
                    FinalizerPayload.marker_id == old_marker.id,
                )
            )
            await session.delete(old_marker)
            initial_resource = await _resource_snapshot(
                session,
                action_type="refund",
                resource_id=resource_id,
            )

        checkpoint = SimpleNamespace(
            config={"configurable": {"checkpoint_id": "checkpoint_worker_topology"}},
            checkpoint={"id": "checkpoint_worker_topology", "channel_values": {}},
        )

        class RuntimeStub:
            checkpointer = SimpleNamespace(
                aget_tuple=lambda _config: None,
            )

            async def fork_checkpoint(self, **kwargs: object) -> None:
                del kwargs

            async def resume_ticket(self, **kwargs: object) -> dict[str, Any]:
                del kwargs
                return output

        runtime = RuntimeStub()

        async def read_checkpoint(_config: object) -> object:
            return checkpoint

        runtime.checkpointer.aget_tuple = read_checkpoint
        handler = AgentJobHandler(
            worker_factory,
            runtime,  # type: ignore[arg-type]
        )
        original_persist_summary = MemoryService.persist_summary

        async def persist_then_fail(
            self: MemoryService,
            state: Any,
        ) -> None:
            await original_persist_summary(self, state)
            raise RuntimeError("injected_real_worker_finalizer_fault")

        with monkeypatch.context() as patch, pytest.raises(
            RuntimeError,
            match="injected_real_worker_finalizer_fault",
        ):
            patch.setattr(MemoryService, "persist_summary", persist_then_fail)
            await handler._resume_approval(lease)

        async with admin_factory() as session:
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            payload = (
                await session.scalar(
                    select(FinalizerPayload).where(
                        FinalizerPayload.marker_id == marker.id,
                    )
                )
                if marker is not None
                else None
            )
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            assert marker is not None and payload is not None
            assert marker.final_checkpoint_id == "checkpoint_worker_topology"
            assert approval is not None and approval.status == "approved"
            assert job is not None and job.status == "leased"
            assert run is not None and run.status == "running"
            assert action_count == 0
            assert (
                await _resource_snapshot(
                    session,
                    action_type="refund",
                    resource_id=resource_id,
                )
                == initial_resource
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_legacy_actionful_fault_never_aborts_committed_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old-worker effect is immutable even if later publication rolls back."""

    database_url = os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        context,
    ) = await _prepare("refund", "legacy_effect")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with worker_factory.worker(context) as session:
            action_result = await RuntimeActionExecutor(session).execute(
                lease,
                approval_id=approval_id,
            )
            await session.commit()
        assert action_result.business_action_id is not None

        async with admin_factory() as session, session.begin():
            marker = await session.scalar(
                select(CheckpointCommitMarker).where(
                    CheckpointCommitMarker.job_id == lease.job_id,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            payload = (
                await session.scalar(
                    select(FinalizerPayload).where(
                        FinalizerPayload.marker_id == marker.id,
                    )
                )
                if marker is not None
                else None
            )
            approval = await session.get(ApprovalRequest, approval_id)
            decision = await session.scalar(
                select(HumanDecision).where(
                    HumanDecision.approval_id == approval_id,
                )
            )
            action = await session.get(
                BusinessAction,
                action_result.business_action_id,
            )
            assert marker is not None and payload is not None
            assert approval is not None and decision is not None and action is not None
            assert action.canonical_event_id and action.canonical_event_hash
            action_event = await session.get(AgentEvent, action.canonical_event_id)
            assert action_event is not None
            marker.expected_ticket_head_event_id = action_event.id
            marker.expected_ticket_sequence = action_event.ticket_sequence
            marker.expected_ticket_event_hash = action_event.event_hash
            state = dict(payload.state_delta["state"])
            state["execution_result"] = {
                "approval_id": approval.id,
                "business_action_id": action.id,
                "action_type": action.action_type,
                "resource_id": approval.resource_id,
                "status": "succeeded",
                "reused": False,
            }
            state["agent_finish_reason"] = "executed"
            state["final"] = {
                **dict(state["final"]),
                "answer": "退款操作已经安全执行完成，账单状态已更新。",
                "terminal_state": "resolved",
            }
            legacy_delta = ActionfulApprovalResumeDelta(
                approval_id=approval.id,
                human_decision_id=decision.id,
                decision=decision.decision,  # type: ignore[arg-type]
                action_hash=approval.action_hash,
                business_action_id=action.id,
                effect_hash=canonical_hash(action.result),
            )
            rebuilt = await SegmentRepository(session)._build_payload_v2(
                lease,
                marker=marker,
                checkpoint_id=str(marker.final_checkpoint_id),
                checkpoint_hash=str(marker.final_checkpoint_hash),
                outcome="completed",
                state=state,
                proposal_id=None,
                approval_id=approval.id,
                legacy_action_delta=legacy_delta,
            )
            payload.payload_hash = rebuilt.payload_hash
            payload.full_payload = rebuilt.model_dump(mode="json")
            payload.state_delta = rebuilt.state_delta.model_dump(mode="json")
            payload.domain_delta = rebuilt.domain_delta.model_dump(mode="json")
            payload.expected_heads = rebuilt.expected_heads.model_dump(mode="json")
            initial_resource = await _resource_snapshot(
                session,
                action_type="refund",
                resource_id=resource_id,
            )
            marker_id = marker.id

        original_persist_summary = MemoryService.persist_summary

        async def persist_then_fail(
            self: MemoryService,
            state: Any,
        ) -> None:
            await original_persist_summary(self, state)
            raise RuntimeError("injected_legacy_finalizer_fault")

        with monkeypatch.context() as patch, pytest.raises(
            RuntimeError,
            match="injected_legacy_finalizer_fault",
        ):
            patch.setattr(MemoryService, "persist_summary", persist_then_fail)
            with worker_execution_context.bind(context):
                async with worker_factory.worker(context) as session:
                    await SegmentRepository(session).finalize(
                        lease,
                        marker_id=marker_id,
                    )

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            marker = await session.get(CheckpointCommitMarker, marker_id)
            job = await session.get(RuntimeJob, lease.job_id)
            run = await session.get(AgentRun, lease.run_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            assert approval is not None and approval.status == "executed"
            assert marker is not None and marker.status == "checkpoint_written"
            assert job is not None and job.status == "leased"
            assert run is not None and run.status == "running"
            assert action_count == 1
            assert (
                await _resource_snapshot(
                    session,
                    action_type="refund",
                    resource_id=resource_id,
                )
                == initial_resource
            )

        with worker_execution_context.bind(context):
            async with worker_factory.worker(context) as session:
                await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )
                await session.commit()

        async with admin_factory() as session:
            marker = await session.get(CheckpointCommitMarker, marker_id)
            job = await session.get(RuntimeJob, lease.job_id)
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            assert marker is not None and marker.status == "finalized"
            assert job is not None and job.status == "succeeded"
            assert action_count == 1
    finally:
        await worker.dispose()
        await admin.dispose()
