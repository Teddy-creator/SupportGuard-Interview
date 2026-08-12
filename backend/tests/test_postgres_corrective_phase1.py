from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.contracts.context import WorkerExecutionContext
from supportguard.db.models import Customer
from supportguard.db.session import create_scoped_session_factory


def _worker_url() -> str | None:
    # A restricted-role integration test must never guess credentials from an
    # administrator URL.  The environment that bootstraps the role owns this
    # contract and must provide the matching login explicitly.
    return os.getenv("TEST_WORKER_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_worker_login_rebinds_rls_scope_after_transaction_boundaries() -> None:
    database_url = _worker_url()
    if database_url is None:
        pytest.skip("worker service login is required")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    factory = create_scoped_session_factory(engine)
    context = WorkerExecutionContext(
        tenant_id="tenant_demo",
        actor_principal_id="cust_demo",
        executor_service_principal="supportguard_worker",
        customer_id="cust_demo",
        ticket_id="scope_probe",
        run_id="scope_probe",
        job_id="scope_probe",
        segment_id="scope_probe",
        delivery_generation=1,
        fencing_token=1,
        trace_id="scope_probe",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    try:
        async with factory.worker(context) as session:
            for boundary in ("initial", "commit", "rollback"):
                if boundary == "commit":
                    await session.commit()
                elif boundary == "rollback":
                    await session.rollback()
                values = (
                    await session.execute(
                        text(
                            "SELECT current_setting('app.tenant_id', true), "
                            "current_setting('app.principal_id', true), "
                            "current_setting('app.principal_role', true)"
                        )
                    )
                ).one()
                assert tuple(values) == ("tenant_demo", "cust_demo", "system_worker")
            visible = list((await session.scalars(select(Customer.id))).all())
            assert visible
            assert all(value.startswith("cust_") for value in visible)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_worker_login_cannot_reuse_tenant_a_scope_for_tenant_b() -> None:
    database_url = _worker_url()
    if database_url is None:
        pytest.skip("worker service login is required")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    factory = create_scoped_session_factory(engine)
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    contexts = [
        WorkerExecutionContext(
            tenant_id=tenant,
            actor_principal_id="scope_probe",
            executor_service_principal="supportguard_worker",
            customer_id="scope_probe",
            ticket_id="scope_probe",
            run_id="scope_probe",
            job_id="scope_probe",
            segment_id="scope_probe",
            delivery_generation=1,
            fencing_token=1,
            trace_id=f"scope:{tenant}",
            deadline=deadline,
        )
        for tenant in ("tenant_demo", "tenant_other")
    ]
    try:
        observed: list[tuple[str, list[str]]] = []
        for context in contexts:
            async with factory.worker(context) as session:
                ids = list((await session.scalars(select(Customer.id))).all())
                observed.append((context.tenant_id, ids))
        demo_ids = set(observed[0][1])
        other_ids = set(observed[1][1])
        assert demo_ids
        assert demo_ids.isdisjoint(other_ids)
    finally:
        await engine.dispose()
