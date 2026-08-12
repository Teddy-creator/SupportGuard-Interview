from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.db.models import SupportTicket, TicketMessage

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_conversation_tables_preserve_control_capabilities_and_scope_message_trigger() -> (
    None
):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_rls_cohesion_{suffix}"
    message_id = f"message_rls_cohesion_{suffix}"
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
                        "WHERE oid IN (to_regclass('support_tickets'),"
                        "to_regclass('ticket_messages'),to_regclass('agent_runs'),"
                        "to_regclass('conversation_turns')) ORDER BY relname"
                    )
                )
            ).all()
        assert rows == [
            ("agent_runs", True, False),
            ("conversation_turns", True, False),
            ("support_tickets", True, False),
            ("ticket_messages", True, False),
        ]

        async with factory() as session:
            transaction = await session.begin()
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add(
                SupportTicket(
                    id=ticket_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    status="queued",
                )
            )
            await session.flush()
            await session.execute(
                text("GRANT SELECT,INSERT ON ticket_messages TO supportguard_worker")
            )
            await session.execute(text("SET SESSION AUTHORIZATION supportguard_worker"))
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_other',true)"))
            with pytest.raises(DBAPIError, match="tenant_scope_mismatch"):
                async with session.begin_nested():
                    session.add(
                        TicketMessage(
                            id=message_id,
                            tenant_id="tenant_demo",
                            ticket_id=ticket_id,
                            role="user",
                            content="must not cross tenant scope",
                        )
                    )
                    await session.flush()
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            session.add(
                TicketMessage(
                    id=message_id,
                    tenant_id="tenant_demo",
                    ticket_id=ticket_id,
                    role="user",
                    content="matching tenant scope",
                )
            )
            await session.flush()
            stored = await session.scalar(
                select(TicketMessage).where(TicketMessage.id == message_id)
            )
            assert stored is not None and stored.conversation_sequence == 1
            await session.execute(text("RESET SESSION AUTHORIZATION"))
            await session.execute(
                text("REVOKE SELECT,INSERT ON ticket_messages FROM supportguard_worker")
            )
            await transaction.rollback()
    finally:
        await engine.dispose()
