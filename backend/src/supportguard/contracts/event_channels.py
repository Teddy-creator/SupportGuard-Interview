from __future__ import annotations

import hashlib


def ticket_event_channel(tenant_id: str, ticket_id: str) -> str:
    """Return a non-enumerable wake-up channel scoped to one tenant ticket."""

    if not tenant_id or not ticket_id:
        raise ValueError("ticket_event_channel_scope_required")
    scope = hashlib.sha256(f"{tenant_id}\0{ticket_id}".encode()).hexdigest()
    return f"supportguard:ticket-events:v2:{scope}"
