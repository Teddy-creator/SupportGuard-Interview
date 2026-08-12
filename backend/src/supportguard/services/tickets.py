from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import Customer, SupportTicket, TicketMessage, new_id
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.errors import DomainError, ErrorCode


class TicketService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        customer_id: str,
        message: str,
        ticket_id: str | None = None,
        message_id: str | None = None,
    ) -> tuple[SupportTicket, TicketMessage]:
        customer = await self.session.get(Customer, customer_id)
        if customer is None:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "Customer is out of scope")
        ticket = SupportTicket(
            id=ticket_id or new_id("ticket"),
            tenant_id=customer.tenant_id,
            customer_id=customer_id,
            status="open",
        )
        self.session.add(ticket)
        await self.session.flush()
        message_record = TicketMessage(
            id=message_id or new_id("msg"),
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            role="user",
            content=message,
        )
        self.session.add(message_record)
        advance_conversation_activity(ticket)
        await self.session.flush()
        return ticket, message_record

    async def append_message(
        self, *, ticket_id: str, customer_id: str, message: str
    ) -> TicketMessage:
        ticket = await self.session.scalar(
            select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
        )
        if ticket is None:
            raise DomainError(ErrorCode.TICKET_NOT_FOUND, "Ticket was not found")
        if ticket.customer_id != customer_id:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "Ticket is out of scope")
        if ticket.status not in {"open", "needs_clarification"}:
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "Ticket cannot accept a message in its current state",
                details={"status": ticket.status},
            )
        message_record = TicketMessage(
            id=new_id("msg"),
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            role="user",
            content=message,
        )
        self.session.add(message_record)
        advance_conversation_activity(ticket)
        ticket.status = "running"
        ticket.version += 1
        await self.session.flush()
        return message_record
