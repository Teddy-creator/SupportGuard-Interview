from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import seed_business_facts
from supportguard.db.models import AgentRun, RuntimeJob
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.runtime_queue import RuntimeReconciler
from supportguard.services.segments import SegmentRepository


async def _create_queued_job(
    session: AsyncSession,
) -> tuple[RuntimeJobRepository, AgentRun, RuntimeJob]:
    await seed_business_facts(session)
    run = await session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(session)
    job = await jobs.create(
        tenant_id="tenant_demo",
        run_id=run.id,
        kind="agent_start",
    )
    assert job.status == "queued"
    assert job.status_version == 1
    return jobs, run, job


@pytest.mark.asyncio
async def test_claim_advances_job_status_version_once_and_rejected_replay_does_not(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)

    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    assert job.status == "leased"
    assert job.status_version == 2
    with pytest.raises(RuntimeConflict, match="job_not_claimable"):
        await jobs.claim(job_id=job.id, owner="worker-a")
    assert job.status == "leased"
    assert job.status_version == 2
    await jobs.assert_fence(lease)


@pytest.mark.asyncio
async def test_retryable_fail_advances_job_status_version_once_and_stale_replay_does_not(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    status = await jobs.fail(lease, error_code="failed:transient")

    assert status == "retry_wait"
    assert job.status_version == 3
    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await jobs.fail(lease, error_code="failed:transient")
    assert job.status == "retry_wait"
    assert job.status_version == 3


@pytest.mark.asyncio
async def test_terminal_fail_advances_job_status_version_once_and_stale_replay_does_not(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    status = await jobs.terminal_fail(lease, error_code="domain:invalid_scope")

    assert status == "dead"
    assert job.status_version == 3
    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await jobs.terminal_fail(lease, error_code="domain:invalid_scope")
    assert job.status == "dead"
    assert job.status_version == 3


@pytest.mark.asyncio
async def test_finalize_control_advances_only_for_a_real_status_change(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    await jobs.finalize_control(lease, status="dead", outcome="failed:worker")

    assert job.status == "dead"
    assert job.status_version == 3
    await jobs.finalize_control(lease, status="dead", outcome="failed:worker")
    assert job.status == "dead"
    assert job.status_version == 3


@pytest.mark.asyncio
async def test_complete_advances_job_status_version_once_and_stale_replay_does_not(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    lease = await jobs.claim(job_id=job.id, owner="worker-a")

    await jobs.complete(lease, outcome="completed")

    assert job.status == "succeeded"
    assert job.status_version == 3
    with pytest.raises(RuntimeConflict, match="stale_fencing_token"):
        await jobs.complete(lease, outcome="completed")
    assert job.status == "succeeded"
    assert job.status_version == 3


@pytest.mark.asyncio
async def test_reconciler_retry_wait_to_queued_advances_once(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    assert await jobs.fail(lease, error_code="failed:transient") == "retry_wait"
    job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    before = job.status_version
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 1
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status == "queued"
        assert stored.status_version == before + 1

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 0
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status_version == before + 1


@pytest.mark.asyncio
async def test_reconciler_expired_lease_to_queued_advances_once(
    db_session: AsyncSession,
) -> None:
    jobs, _, job = await _create_queued_job(db_session)
    await jobs.claim(job_id=job.id, owner="worker-a")
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    before = job.status_version
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 1
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status == "queued"
        assert stored.status_version == before + 1

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 0
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status_version == before + 1


@pytest.mark.asyncio
async def test_reconciler_dead_letter_advances_once(
    db_session: AsyncSession,
) -> None:
    _, _, job = await _create_queued_job(db_session)
    job.created_at = datetime.now(UTC) - timedelta(hours=1)
    before = job.status_version
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 0
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status == "dead"
        assert stored.status_version == before + 1

    assert await RuntimeReconciler(factory).reconcile_once(redelivery_grace_seconds=0) == 0
    async with factory() as verification:
        stored = await verification.get(RuntimeJob, job.id)
        assert stored is not None
        assert stored.status_version == before + 1


@pytest.mark.asyncio
async def test_finalizer_takeover_versions_come_from_fail_and_successor_claim(
    db_session: AsyncSession,
) -> None:
    jobs, run, job = await _create_queued_job(db_session)
    first_lease = await jobs.claim(job_id=job.id, owner="worker-old")
    segments = SegmentRepository(db_session)
    marker = await segments.prepare(
        first_lease,
        delivery_generation=1,
        segment_kind="agent_start",
        segment_input={"kind": "agent_start"},
    )
    state = {
        "ticket_id": "ticket_demo",
        "customer_id": "cust_demo",
        "run_id": run.id,
        "trace_id": "trace_status_version",
        "classification": {"issue_type": "product_knowledge", "risk": "low"},
        "agent_finish_reason": "answered",
        "tool_observations": [],
        "evidence": [],
        "segment_events": [],
        "final": {
            "answer": "Recovered without external calls.",
            "terminal_state": "resolved",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "policy_route": "answer",
        },
    }
    await segments.checkpoint_written(
        first_lease,
        marker_id=marker.id,
        checkpoint_id="checkpoint-status-version",
        checkpoint_hash="e" * 64,
        outcome="completed",
        state=state,
    )
    expected_job_version = job.status_version
    expected_run_version = run.status_version

    assert (
        await jobs.fail(
            first_lease,
            error_code="failed:checkpoint_written_finalizer_interrupted",
        )
        == "retry_wait"
    )
    assert job.status_version == expected_job_version + 1
    assert run.status_version == expected_run_version + 1
    job.available_at = datetime(2000, 1, 1, tzinfo=UTC)

    successor_lease = await jobs.claim(job_id=job.id, owner="worker-new")

    assert job.status_version == expected_job_version + 2
    assert run.status_version == expected_run_version + 2
    replacement = await segments.takeover_finalizer(
        successor_lease,
        source_marker_id=marker.id,
    )
    assert replacement.status == "checkpoint_written"
