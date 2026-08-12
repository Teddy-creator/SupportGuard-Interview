from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import AgentRunStore
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ConversationTurn,
    Customer,
    SupportTicket,
    Tenant,
    TicketMessage,
)

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_postgres_ticket_and_run_sequences_are_concurrency_safe() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    tenant_id = f"tenant_it_{suffix}"
    customer_id = f"cust_it_{suffix}"
    ticket_id = f"ticket_it_{suffix}"
    message_id = f"msg_it_{suffix}"
    run_id: str
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id',:tenant_id,true)"),
            {"tenant_id": tenant_id},
        )
        tenant = Tenant(id=tenant_id, name="Integration Tenant", status="active")
        customer = Customer(
            id=customer_id,
            tenant_id=tenant_id,
            display_name="Integration Customer",
            email=f"{suffix}@integration.test",
            status="active",
            security_status="normal",
            region="test-region",
            version=1,
        )
        ticket = SupportTicket(id=ticket_id, tenant_id=tenant_id, customer_id=customer_id)
        message = TicketMessage(
            id=message_id,
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            role="user",
            content="concurrent event allocation",
        )
        session.add(tenant)
        await session.flush()
        session.add(customer)
        await session.flush()
        session.add(ticket)
        await session.flush()
        session.add(message)
        await session.flush()
        run = await AgentRunStore(session).create(
            ticket_id=ticket_id,
            customer_id=customer_id,
            message_id=message_id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run_id = run.id
        await session.commit()

    async def append(event_type: str) -> None:
        async with factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            await AgentRunStore(session).append_event(run, event_type=event_type, payload={})
            await session.commit()

    await asyncio.gather(append("concurrent_a"), append("concurrent_b"))
    async with factory() as session:
        events = (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.run_sequence)
            )
        ).all()
        assert [event.run_sequence for event in events] == [1, 2, 3]
        assert [event.ticket_sequence for event in events] == [1, 2, 3]
        await session.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(turn_id=None)
        )
        await session.execute(
            update(TicketMessage)
            .where(TicketMessage.id == message_id)
            .values(turn_id=None, run_id=None)
        )
        await session.execute(
            delete(ConversationTurn).where(ConversationTurn.customer_message_id == message_id)
        )
        await session.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await session.execute(delete(TicketMessage).where(TicketMessage.id == message_id))
        await session.execute(delete(SupportTicket).where(SupportTicket.id == ticket_id))
        await session.execute(delete(Customer).where(Customer.id == customer_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_append_refreshes_a_stale_identity_map_after_lock() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    tenant_id = f"tenant_stale_{suffix}"
    customer_id = f"cust_stale_{suffix}"
    ticket_id = f"ticket_stale_{suffix}"
    message_id = f"msg_stale_{suffix}"

    async with factory() as setup_session:
        await setup_session.execute(
            text("SELECT set_config('app.tenant_id',:tenant_id,true)"),
            {"tenant_id": tenant_id},
        )
        setup_session.add(Tenant(id=tenant_id, name="Stale Map Tenant", status="active"))
        await setup_session.flush()
        setup_session.add(
            Customer(
                id=customer_id,
                tenant_id=tenant_id,
                display_name="Stale Map Customer",
                email=f"{suffix}@stale.test",
                status="active",
                security_status="normal",
                region="test-region",
                version=1,
            )
        )
        await setup_session.flush()
        setup_session.add(
            SupportTicket(id=ticket_id, tenant_id=tenant_id, customer_id=customer_id)
        )
        await setup_session.flush()
        setup_session.add(
            TicketMessage(
                id=message_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                role="user",
                content="stale identity map regression",
            )
        )
        await setup_session.flush()
        run = await AgentRunStore(setup_session).create(
            ticket_id=ticket_id,
            customer_id=customer_id,
            message_id=message_id,
            model="fake",
            provider_mode="fake",
            tool_call_mode="native_fixture",
            context_version="context.v1.2",
        )
        run_id = run.id
        await setup_session.commit()

    async with factory() as stale_session:
        stale_run = await stale_session.get(AgentRun, run_id)
        assert stale_run is not None
        assert stale_run.next_run_sequence == 1

        async with factory() as winning_session:
            winning_run = await winning_session.get(AgentRun, run_id)
            assert winning_run is not None
            await AgentRunStore(winning_session).append_event(
                winning_run,
                event_type="winning_writer",
                payload={},
            )
            await winning_session.commit()

        await AgentRunStore(stale_session).append_event(
            stale_run,
            event_type="stale_writer",
            payload={},
        )
        await stale_session.commit()

    async with factory() as cleanup_session:
        events = (
            await cleanup_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.run_sequence)
            )
        ).all()
        assert [event.run_sequence for event in events] == [1, 2, 3]
        assert [event.event_type for event in events] == [
            "run_started",
            "winning_writer",
            "stale_writer",
        ]
        await cleanup_session.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await cleanup_session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(turn_id=None)
        )
        await cleanup_session.execute(
            update(TicketMessage)
            .where(TicketMessage.id == message_id)
            .values(turn_id=None, run_id=None)
        )
        await cleanup_session.execute(
            delete(ConversationTurn).where(ConversationTurn.customer_message_id == message_id)
        )
        await cleanup_session.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await cleanup_session.execute(delete(TicketMessage).where(TicketMessage.id == message_id))
        await cleanup_session.execute(delete(SupportTicket).where(SupportTicket.id == ticket_id))
        await cleanup_session.execute(delete(Customer).where(Customer.id == customer_id))
        await cleanup_session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await cleanup_session.commit()
    await engine.dispose()
