from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text

from supportguard.api.auth import Principal, customer_principal
from supportguard.contracts.context import RequestContext
from supportguard.contracts.event_channels import ticket_event_channel
from supportguard.db.models import AgentEvent, SupportTicket
from supportguard.db.session import ScopedSessionFactory
from supportguard.observability.metrics import SSE_CONNECTIONS, SSE_REPLAYED_EVENTS

router = APIRouter()
CustomerIdentity = Annotated[Principal, Depends(customer_principal)]

_PUBLIC_EVENT_STRING_FIELDS = {
    "action_type",
    "freshness_status",
    "route",
    "tool_name",
}
_PUBLIC_EVENT_OUTCOMES = {
    "answered",
    "completed",
    "executed",
    "failed",
    "needs_clarification",
    "proposed",
    "refused",
    "rejected",
    "resolved",
    "stopped",
    "withdrawn",
}
_PUBLIC_PROJECTION_STATUSES = {
    "approved",
    "executed",
    "executing",
    "failed",
    "manual_takeover_legacy",
    "pending",
    "rejected",
    "stale",
    "verification_pending",
    "withdrawn",
}


def _factory(request: Request) -> ScopedSessionFactory:
    if hasattr(request.app.state, "scoped_factory"):
        return cast(ScopedSessionFactory, request.app.state.scoped_factory)
    return cast(ScopedSessionFactory, request.app.state.runtime.scoped_factory)


async def visible_events(
    request: Request,
    *,
    ticket_id: str,
    identity: Principal,
    after: int,
    limit: int = 256,
) -> list[AgentEvent | dict[str, Any]]:
    context = RequestContext(
        tenant_id=identity.tenant_id,
        authenticated_actor_id=identity.subject_id,
        authenticated_actor_role=identity.membership_role or identity.role,
        request_id=str(request.headers.get("x-request-id", "sse-replay")),
        trace_id=str(request.headers.get("traceparent", "sse-replay")),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        subject_customer_id=identity.customer_id,
    )
    async with _factory(request).request(context) as session:
        if session.get_bind().dialect.name == "postgresql":
            customer_id = cast(str, identity.customer_id)
            payload = await session.scalar(
                text(
                    "SELECT supportguard_api_list_ticket_events("
                    ":customer_id,:ticket_id,:after_sequence,:limit)"
                ),
                {
                    "customer_id": customer_id,
                    "ticket_id": ticket_id,
                    "after_sequence": after,
                    "limit": limit,
                },
            )
            if payload is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise RuntimeError("api_list_ticket_events_capability_invalid")
            return cast(list[AgentEvent | dict[str, Any]], payload)
        ticket = await session.get(SupportTicket, ticket_id)
        if ticket is None or ticket.customer_id != identity.customer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
        return list(
            (
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.ticket_id == ticket_id,
                        AgentEvent.ticket_sequence > after,
                        AgentEvent.visibility == "customer",
                    )
                    .order_by(AgentEvent.ticket_sequence)
                    .limit(limit)
                )
            ).all()
        )


def _safe_public_event_payload(value: object) -> dict[str, Any]:
    """Project wake-up events to bounded customer-safe metadata.

    SSE is a refresh signal, not a second trace or business-data API.  Durable
    conversation and action state are fetched through their scoped read models,
    so raw AgentEvent payloads must never cross this boundary.
    """

    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for key in _PUBLIC_EVENT_STRING_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 64:
            projected[key] = item
    outcome = value.get("outcome")
    if isinstance(outcome, str) and outcome in _PUBLIC_EVENT_OUTCOMES:
        projected["outcome"] = outcome
    projection_status = value.get("projection_status")
    if isinstance(projection_status, str) and projection_status in _PUBLIC_PROJECTION_STATUSES:
        projected["projection_status"] = projection_status
    source_count = value.get("source_count")
    if (
        isinstance(source_count, int)
        and not isinstance(source_count, bool)
        and 0 <= source_count <= 100
    ):
        projected["source_count"] = source_count
    return projected


def encode_event(event: AgentEvent | dict[str, Any]) -> str:
    def value(name: str) -> Any:
        return event[name] if isinstance(event, dict) else getattr(event, name)

    created_at = value("created_at")
    payload: dict[str, Any] = {
        "ticket_sequence": value("ticket_sequence"),
        "run_id": value("run_id"),
        "run_sequence": value("run_sequence"),
        "event_type": value("event_type"),
        "status": value("status"),
        "payload": _safe_public_event_payload(value("payload")),
        "created_at": (
            created_at.isoformat() if isinstance(created_at, datetime) else str(created_at)
        ),
    }
    return (
        f"id: {value('ticket_sequence')}\n"
        f"event: {value('event_type')}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def event_stream(
    request: Request,
    *,
    ticket_id: str,
    identity: Principal,
    cursor: int,
) -> AsyncIterator[str]:
    heartbeat_at = datetime.now(UTC)
    pubsub = None
    redis = getattr(request.app.state, "redis", None)
    wakeup_channel = ticket_event_channel(identity.tenant_id, ticket_id)
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(wakeup_channel)
        except Exception:
            if pubsub is not None:
                await pubsub.aclose()
            pubsub = None
    SSE_CONNECTIONS.inc()
    try:
        while not await request.is_disconnected():
            events = await visible_events(
                request,
                ticket_id=ticket_id,
                identity=identity,
                after=cursor,
            )
            for event in events:
                cursor = int(
                    event["ticket_sequence"] if isinstance(event, dict) else event.ticket_sequence
                )
                SSE_REPLAYED_EVENTS.inc()
                yield encode_event(event)
            now = datetime.now(UTC)
            if (now - heartbeat_at).total_seconds() >= 15:
                yield ": heartbeat\n\n"
                heartbeat_at = now
            if pubsub is None:
                await asyncio.sleep(1)
            else:
                try:
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception:
                    await pubsub.aclose()
                    pubsub = None
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(wakeup_channel)
            finally:
                await pubsub.aclose()
        SSE_CONNECTIONS.dec()


@router.get("/tickets/{ticket_id}/events/stream")
async def stream_ticket_events(
    ticket_id: str,
    request: Request,
    identity: CustomerIdentity,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        cursor = max(0, int(last_event_id or "0"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid Last-Event-ID") from exc
    await visible_events(
        request,
        ticket_id=ticket_id,
        identity=identity,
        after=cursor,
        limit=1,
    )
    return StreamingResponse(
        event_stream(
            request,
            ticket_id=ticket_id,
            identity=identity,
            cursor=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
