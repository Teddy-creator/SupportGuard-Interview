from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.contracts import CONTEXT_VERSION
from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.errors import RuntimeConflict
from supportguard.db.models import (
    AgentRun,
    ConversationTurn,
    OutboxEvent,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)


async def activate_next_turn(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    trace_id: str,
) -> tuple[AgentRun, RuntimeJob] | None:
    """Promote the oldest accepted ordinary Turn while holding the Ticket lock."""

    active = await session.scalar(
        select(ConversationTurn.id)
        .where(
            ConversationTurn.ticket_id == ticket.id,
            ConversationTurn.activity_state.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active is not None or ticket.automation_mode != "agent" or ticket.lifecycle != "active":
        return None
    turn = await session.scalar(
        select(ConversationTurn)
        .where(
            ConversationTurn.ticket_id == ticket.id,
            ConversationTurn.activity_state == "accepted",
        )
        .order_by(ConversationTurn.ordinal, ConversationTurn.id)
        .limit(1)
        .with_for_update()
    )
    if turn is None:
        return None
    if not turn.model or not turn.provider_mode or not turn.tool_call_mode:
        raise RuntimeConflict("accepted_turn_runtime_identity_missing")
    run = await AgentRunStore(session).create(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=turn.customer_message_id,
        model=turn.model,
        provider_mode=turn.provider_mode,
        tool_call_mode=turn.tool_call_mode,
        context_version=turn.context_version or CONTEXT_VERSION,
    )
    run.status = "queued"
    run.turn_id = turn.id
    turn.run_id = run.id
    message = await session.get(TicketMessage, turn.customer_message_id)
    if message is None:
        raise RuntimeConflict("accepted_turn_customer_message_missing")
    message.run_id = run.id
    turn.activity_state = "queued"
    job = RuntimeJob(
        id=new_id("job"),
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        kind="agent_start",
    )
    if session.get_bind().dialect.name != "postgresql":
        ticket.next_dispatch_sequence += 1
        job.ticket_id = ticket.id
        job.dispatch_sequence = ticket.next_dispatch_sequence
    session.add(job)
    await session.flush()
    session.add(
        OutboxEvent(
            id=new_id("outbox"),
            delivery_id=new_id("delivery"),
            tenant_id=ticket.tenant_id,
            job_id=job.id,
            run_id=run.id,
            event_type="runtime_job_available",
            payload={"traceparent": trace_id},
        )
    )
    ticket.status = "queued"
    ticket.version += 1
    await session.flush()
    return run, job


__all__ = ["activate_next_turn"]
