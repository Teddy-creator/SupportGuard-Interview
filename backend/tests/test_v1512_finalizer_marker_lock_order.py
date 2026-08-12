from __future__ import annotations

import asyncio
import importlib
import inspect
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.memory.service import MemoryService
from supportguard.services.runtime_jobs import RuntimeConflict
from supportguard.services.segments import SegmentRepository


def test_finalizer_and_takeover_share_domain_then_marker_lock_order() -> None:
    finalize = inspect.getsource(SegmentRepository._finalize_in_transaction)
    action_execute = finalize.index(
        "action_result = await RuntimeActionExecutor(self.session).execute"
    )
    action_marker_lock = finalize.index(
        "marker = await self._lock_marker_after_domain",
        action_execute,
    )
    assert action_execute < action_marker_lock
    assert "get(CheckpointCommitMarker, marker_id, with_for_update=True)" not in finalize

    for operation in (
        SegmentRepository.takeover_prepared_tool_turn,
        SegmentRepository.takeover_finalizer,
    ):
        source = inspect.getsource(operation)
        assert source.index("assert_fence") < source.index("with_for_update=True")

    interrupt = inspect.getsource(SegmentRepository.finalize_interrupt)
    interrupt_domain = interrupt.index("_lock_interrupt_finalize_domain")
    interrupt_marker = interrupt.index("_lock_marker_after_domain")
    assert interrupt_domain < interrupt_marker
    assert "get(CheckpointCommitMarker, marker_id, with_for_update=True)" not in interrupt

    interrupt_locks = inspect.getsource(SegmentRepository._lock_interrupt_finalize_domain)
    ticket_lock = interrupt_locks.index("ticket_lock =")
    approval_lock = interrupt_locks.index("active_rows =")
    proposal_lock = interrupt_locks.index("proposal_rows =")
    run_lock = interrupt_locks.index("run_lock =")
    turn_lock = interrupt_locks.index("select(ConversationTurn)", run_lock)
    job_lock = interrupt_locks.index("job_lock =")
    assert ticket_lock < approval_lock < proposal_lock < run_lock < turn_lock < job_lock

    abort = inspect.getsource(SegmentRepository._abort_finalizer)
    assert "assert_fence" not in abort


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


async def _wait_until_lock_wait(
    engine: AsyncEngine,
    *,
    backend_pid: int,
    timeout_seconds: float = 4.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as connection:
            wait_event = await connection.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:backend_pid"),
                {"backend_pid": backend_pid},
            )
        if wait_event == "Lock":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("finalizer did not reach the Ticket barrier")


async def _row_is_nowait_lockable(
    engine: AsyncEngine,
    *,
    table: str,
    row_id: str,
) -> bool:
    statements = {
        "support_tickets": text(
            "SELECT id FROM support_tickets WHERE id=:row_id FOR UPDATE NOWAIT"
        ),
        "approval_requests": text(
            "SELECT id FROM approval_requests WHERE id=:row_id FOR UPDATE NOWAIT"
        ),
        "proposal_records": text(
            "SELECT id FROM proposal_records WHERE id=:row_id FOR UPDATE NOWAIT"
        ),
        "agent_runs": text("SELECT id FROM agent_runs WHERE id=:row_id FOR UPDATE NOWAIT"),
        "conversation_turns": text(
            "SELECT id FROM conversation_turns WHERE id=:row_id FOR UPDATE NOWAIT"
        ),
        "runtime_jobs": text("SELECT id FROM runtime_jobs WHERE id=:row_id FOR UPDATE NOWAIT"),
        "checkpoint_commit_markers": text(
            "SELECT id FROM checkpoint_commit_markers WHERE id=:row_id FOR UPDATE NOWAIT"
        ),
    }
    statement = statements.get(table)
    if statement is None:
        raise AssertionError("unexpected barrier table")
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                statement,
                {"row_id": row_id},
            )
        except DBAPIError as exc:
            await transaction.rollback()
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            return False
        await transaction.rollback()
        return True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_interrupt_finalizer_locks_ticket_then_waits_on_proposal_before_run_job_marker() -> (
    None
):
    """A real Worker must not hold Run/Job/Marker while waiting on Proposal."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")

    prefix = f"interrupt_lock_{uuid4().hex[:10]}"
    resource_id = f"bill_{prefix}"
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    prepare_interrupt = importlib.import_module("test_v1512_active_approval_race").__dict__[
        "_prepare_interrupt"
    ]
    try:
        async with admin_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add(
                BillingRecord(
                    id=resource_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    version=2,
                )
            )
            fixture = await prepare_interrupt(
                session,
                prefix=prefix,
                ordinal=1,
                resource_id=resource_id,
            )

        pid_ready: asyncio.Queue[int] = asyncio.Queue()

        async def finalize_interrupt() -> str:
            async with worker_factory() as session, session.begin():
                await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
                await pid_ready.put(int(await session.scalar(select(func.pg_backend_pid())) or 0))
                approval = await SegmentRepository(session).finalize_interrupt(
                    fixture.lease,
                    marker_id=fixture.marker_id,
                    proposal_id=fixture.proposal_id,
                    test_capability=issue_test_runtime_capability(testing=True),
                )
                return approval.id

        async with admin.connect() as proposal_barrier:
            barrier_transaction = await proposal_barrier.begin()
            await proposal_barrier.execute(
                text("SELECT id FROM proposal_records WHERE id=:proposal_id FOR UPDATE"),
                {"proposal_id": fixture.proposal_id},
            )
            finalizer_task = asyncio.create_task(finalize_interrupt())
            backend_pid = await asyncio.wait_for(pid_ready.get(), timeout=2)
            await _wait_until_lock_wait(admin, backend_pid=backend_pid)

            # The Worker owns Ticket before reaching Proposal, but no later row
            # in the frozen prefix (and no Marker) may be locked yet.
            assert not await _row_is_nowait_lockable(
                admin,
                table="support_tickets",
                row_id=fixture.ticket_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="agent_runs",
                row_id=fixture.run_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="conversation_turns",
                row_id=fixture.turn_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="runtime_jobs",
                row_id=fixture.lease.job_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="checkpoint_commit_markers",
                row_id=fixture.marker_id,
            )

            await barrier_transaction.rollback()
            approval_id = await asyncio.wait_for(finalizer_task, timeout=8)

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            marker = await session.get(CheckpointCommitMarker, fixture.marker_id)
            job = await session.get(RuntimeJob, fixture.lease.job_id)
            assert approval is not None and approval.status == "pending"
            assert marker is not None and marker.status == "finalized"
            assert job is not None and job.status == "succeeded"
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_execute_active_approval_and_interrupt_sibling_do_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute A1 and interrupt P2 share Approval->Proposal lock ordering."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")

    async def skip_fixture_summary(_service: object, _state: object) -> None:
        return None

    monkeypatch.setattr(MemoryService, "persist_summary", skip_fixture_summary)
    prepare_action = importlib.import_module("test_v1512_runtime_action_binding_postgres").__dict__[
        "_prepare"
    ]
    prepare_interrupt = importlib.import_module("test_v1512_active_approval_race").__dict__[
        "_prepare_interrupt"
    ]
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _idempotency_key,
        action_lease,
        action_context,
    ) = await prepare_action("refund", "a1_p2_order")
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False),
        pool_size=2,
        max_overflow=0,
    )
    interrupt_factory = async_sessionmaker(worker, expire_on_commit=False)
    action_factory = create_scoped_session_factory(worker)
    try:
        prefix = f"a1p2_{uuid4().hex[:10]}"
        async with admin_factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            action_marker_id = await session.scalar(
                select(CheckpointCommitMarker.id).where(
                    CheckpointCommitMarker.job_id == action_lease.job_id,
                    CheckpointCommitMarker.fencing_token == action_lease.fencing_token,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            assert action_marker_id is not None
            action_proposal_id = approval.proposal_id
            action_run_id = approval.run_id
            action_ticket_id = approval.ticket_id
            interrupt = await prepare_interrupt(
                session,
                prefix=prefix,
                ordinal=2,
                resource_id=resource_id,
            )

        interrupt_pid_ready: asyncio.Queue[int] = asyncio.Queue()
        action_pid_ready: asyncio.Queue[int] = asyncio.Queue()

        async def finalize_interrupt() -> str:
            async with interrupt_factory() as session, session.begin():
                await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
                await interrupt_pid_ready.put(
                    int(await session.scalar(select(func.pg_backend_pid())) or 0)
                )
                approval = await SegmentRepository(session).finalize_interrupt(
                    interrupt.lease,
                    marker_id=interrupt.marker_id,
                    proposal_id=interrupt.proposal_id,
                    test_capability=issue_test_runtime_capability(testing=True),
                )
                return approval.id

        async def finalize_action() -> str:
            async with action_factory.worker(action_context) as session:
                await action_pid_ready.put(
                    int(await session.scalar(select(func.pg_backend_pid())) or 0)
                )
                marker = await SegmentRepository(session).finalize(
                    action_lease,
                    marker_id=action_marker_id,
                )
                await session.commit()
                return marker.status

        async with admin.connect() as proposal_barrier:
            barrier_transaction = await proposal_barrier.begin()
            await proposal_barrier.execute(
                text("SELECT id FROM proposal_records WHERE id=:proposal_id FOR UPDATE"),
                {"proposal_id": interrupt.proposal_id},
            )
            interrupt_task = asyncio.create_task(finalize_interrupt())
            interrupt_pid = await asyncio.wait_for(
                interrupt_pid_ready.get(),
                timeout=2,
            )
            await _wait_until_lock_wait(admin, backend_pid=interrupt_pid)

            # The interrupt owns the canonical active Approval before it waits
            # on P2, but it has not yet reached its Run/Job/Marker suffix.
            assert not await _row_is_nowait_lockable(
                admin,
                table="approval_requests",
                row_id=approval_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="agent_runs",
                row_id=interrupt.run_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="runtime_jobs",
                row_id=interrupt.lease.job_id,
            )
            assert await _row_is_nowait_lockable(
                admin,
                table="checkpoint_commit_markers",
                row_id=interrupt.marker_id,
            )

            action_task = asyncio.create_task(finalize_action())
            action_pid = await asyncio.wait_for(action_pid_ready.get(), timeout=2)
            await _wait_until_lock_wait(admin, backend_pid=action_pid)

            await barrier_transaction.rollback()
            reused_id, action_status = await asyncio.wait_for(
                asyncio.gather(interrupt_task, action_task),
                timeout=12,
            )
            assert reused_id == approval_id
            assert action_status == "finalized"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            billing = await session.get(BillingRecord, resource_id)
            proposals = list(
                await session.scalars(
                    select(ProposalRecord).where(
                        ProposalRecord.id.in_([action_proposal_id, interrupt.proposal_id])
                    )
                )
            )
            runs = list(
                await session.scalars(
                    select(AgentRun).where(AgentRun.id.in_([action_run_id, interrupt.run_id]))
                )
            )
            jobs = list(
                await session.scalars(
                    select(RuntimeJob).where(
                        RuntimeJob.id.in_([action_lease.job_id, interrupt.lease.job_id])
                    )
                )
            )
            markers = list(
                await session.scalars(
                    select(CheckpointCommitMarker).where(
                        CheckpointCommitMarker.id.in_([action_marker_id, interrupt.marker_id])
                    )
                )
            )
            tickets = list(
                await session.scalars(
                    select(SupportTicket).where(
                        SupportTicket.id.in_([action_ticket_id, interrupt.ticket_id])
                    )
                )
            )
            assert approval is not None and approval.status == "executed"
            assert billing is not None
            assert billing.status == "refunded"
            assert billing.version == 3
            assert {proposal.status for proposal in proposals} == {"stale"}
            assert {run.status for run in runs} == {"completed"}
            assert {job.status for job in jobs} == {"succeeded"}
            assert {marker.status for marker in markers} == {"finalized"}
            assert {ticket.status for ticket in tickets} == {"resolved"}
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == approval_id
                        )
                    )
                    or 0
                )
                == 1
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(ApprovalRequest.id)).where(
                            ApprovalRequest.tenant_id == "tenant_demo",
                            ApprovalRequest.customer_id == "cust_demo",
                            ApprovalRequest.action_type == "refund",
                            ApprovalRequest.resource_id == resource_id,
                            ApprovalRequest.status.in_(("pending", "approved")),
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_interrupt_finalizer_revalidates_lease_after_proposal_barrier() -> None:
    """An identity pre-read cannot authorize a lease expired while blocked."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")

    prefix = f"interrupt_revalidate_{uuid4().hex[:10]}"
    resource_id = f"bill_{prefix}"
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    prepare_interrupt = importlib.import_module("test_v1512_active_approval_race").__dict__[
        "_prepare_interrupt"
    ]
    try:
        async with admin_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add(
                BillingRecord(
                    id=resource_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    version=2,
                )
            )
            fixture = await prepare_interrupt(
                session,
                prefix=prefix,
                ordinal=1,
                resource_id=resource_id,
            )

        pid_ready: asyncio.Queue[int] = asyncio.Queue()

        async def finalize_interrupt() -> str:
            try:
                async with worker_factory() as session, session.begin():
                    await session.execute(
                        text("SELECT set_config('app.tenant_id','tenant_demo',true)")
                    )
                    await pid_ready.put(
                        int(await session.scalar(select(func.pg_backend_pid())) or 0)
                    )
                    await SegmentRepository(session).finalize_interrupt(
                        fixture.lease,
                        marker_id=fixture.marker_id,
                        proposal_id=fixture.proposal_id,
                        test_capability=issue_test_runtime_capability(testing=True),
                    )
            except RuntimeConflict as exc:
                return exc.code
            raise AssertionError("expired lease unexpectedly finalized")

        async with admin.connect() as proposal_barrier:
            barrier_transaction = await proposal_barrier.begin()
            await proposal_barrier.execute(
                text("SELECT id FROM proposal_records WHERE id=:proposal_id FOR UPDATE"),
                {"proposal_id": fixture.proposal_id},
            )
            finalizer_task = asyncio.create_task(finalize_interrupt())
            backend_pid = await asyncio.wait_for(pid_ready.get(), timeout=2)
            await _wait_until_lock_wait(admin, backend_pid=backend_pid)

            # RuntimeJob is deliberately still unlocked at this point, so a
            # competing lease authority can expire it before the final lock.
            async with admin.begin() as expiration:
                await expiration.execute(
                    text(
                        "UPDATE runtime_jobs "
                        "SET lease_expires_at=clock_timestamp()-interval '1 second' "
                        "WHERE id=:job_id"
                    ),
                    {"job_id": fixture.lease.job_id},
                )
            await barrier_transaction.rollback()
            assert await asyncio.wait_for(finalizer_task, timeout=8) == "stale_fencing_token"

        async with admin_factory() as session:
            marker = await session.get(CheckpointCommitMarker, fixture.marker_id)
            job = await session.get(RuntimeJob, fixture.lease.job_id)
            assert marker is not None and marker.status == "checkpoint_written"
            assert job is not None and job.status == "leased"
            assert (
                int(
                    await session.scalar(
                        select(func.count(ApprovalRequest.id)).where(
                            ApprovalRequest.proposal_id == fixture.proposal_id
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_action_finalizer_waits_on_ticket_before_marker_and_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Marker stays lockable while Finalizer is blocked on its Ticket."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")

    async def skip_fixture_summary(_service: object, _state: object) -> None:
        # The shared action-capability fixture intentionally contains only the
        # publication/action fields.  Memory is independently covered; this
        # barrier owns only lock ordering and the atomic action aggregate.
        return None

    monkeypatch.setattr(MemoryService, "persist_summary", skip_fixture_summary)
    prepare = importlib.import_module("test_v1512_runtime_action_binding_postgres").__dict__[
        "_prepare"
    ]
    (
        admin,
        admin_factory,
        approval_id,
        _resource_id,
        _idempotency_key,
        lease,
        context,
    ) = await prepare("refund", "marker_barrier")
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
                    CheckpointCommitMarker.fencing_token == lease.fencing_token,
                    CheckpointCommitMarker.status == "checkpoint_written",
                )
            )
            approval = await session.get(ApprovalRequest, approval_id)
            assert marker_id is not None
            assert approval is not None
            ticket_id = approval.ticket_id

        pid_ready: asyncio.Queue[int] = asyncio.Queue()

        async def finalize() -> str:
            async with worker_factory.worker(context) as session:
                backend_pid = int(await session.scalar(select(func.pg_backend_pid())) or 0)
                await pid_ready.put(backend_pid)
                marker = await SegmentRepository(session).finalize(
                    lease,
                    marker_id=marker_id,
                )
                await session.commit()
                return marker.status

        async with admin.connect() as ticket_barrier:
            barrier_transaction = await ticket_barrier.begin()
            await ticket_barrier.execute(
                text(
                    "SELECT id FROM support_tickets "
                    "WHERE tenant_id=:tenant_id AND id=:ticket_id FOR UPDATE"
                ),
                {"tenant_id": lease.tenant_id, "ticket_id": ticket_id},
            )
            finalizer_task = asyncio.create_task(finalize())
            backend_pid = await asyncio.wait_for(pid_ready.get(), timeout=2)
            await _wait_until_lock_wait(admin, backend_pid=backend_pid)

            # A Marker-first implementation would already hold this row while
            # waiting on Ticket.  NOWAIT proves the authoritative Marker lock
            # has not been taken ahead of the aggregate barrier.
            async with admin.begin() as marker_probe:
                await marker_probe.execute(
                    text(
                        "SELECT id FROM checkpoint_commit_markers "
                        "WHERE id=:marker_id FOR UPDATE NOWAIT"
                    ),
                    {"marker_id": marker_id},
                )

            await barrier_transaction.rollback()
            assert await asyncio.wait_for(finalizer_task, timeout=8) == "finalized"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
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
            assert approval is not None and approval.status == "executed"
            assert marker is not None and marker.status == "finalized"
            assert job is not None and job.status == "succeeded"
            assert action_count == 1
    finally:
        await worker.dispose()
        await admin.dispose()
