from __future__ import annotations

from datetime import UTC, datetime

from supportguard.db.models import SupportTicket


def advance_conversation_activity(
    ticket: SupportTicket,
    *,
    occurred_at: datetime | None = None,
) -> datetime:
    """Advance only the customer-visible message clock.

    PostgreSQL enforces the same rule in the TicketMessage insert trigger. This
    helper keeps the SQLite adapter and explicit ORM paths contract-equivalent.
    """

    activity_at = occurred_at or datetime.now(UTC)
    if activity_at.tzinfo is None:
        activity_at = activity_at.replace(tzinfo=UTC)
    current = ticket.last_message_at
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if current is None or activity_at > current:
        ticket.last_message_at = activity_at
    return ticket.last_message_at
