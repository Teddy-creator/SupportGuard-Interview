import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_rls_denies_unscoped_and_isolates_tenants() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE supportguard_rls_client"))
        assert (await connection.scalar(text("SELECT count(*) FROM customers"))) == 0
        await connection.execute(text("SELECT set_config('app.tenant_id', 'tenant_demo', true)"))
        demo_ids = (await connection.execute(text("SELECT id FROM customers"))).scalars().all()
        assert demo_ids == ["cust_demo"]
        await connection.execute(text("SELECT set_config('app.tenant_id', 'tenant_other', true)"))
        other_ids = (await connection.execute(text("SELECT id FROM customers"))).scalars().all()
        assert other_ids == ["cust_other"]
    await engine.dispose()
