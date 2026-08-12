from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select, text

from supportguard.api.contracts import (
    TICKET_EVENT_LIMIT,
    TICKET_LIST_LIMIT,
    AgentEventResponse,
    RunInspectorResponse,
    RunProjectionResponse,
    TicketDetailResponse,
    TicketListItemResponse,
)
from supportguard.api.dependencies import (
    CustomerIdentity,
    request_session,
)
from supportguard.api.projections import (
    _bounded_ticket_payload,
    _public_event_projection,
    _public_run_projection,
    _run_projection,
    _sqlite_run_inspector,
    _sqlite_ticket_projection,
)
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    SupportTicket,
    TicketMessage,
)

router = APIRouter()


@router.get("/tickets", response_model=list[TicketListItemResponse])
async def list_tickets(request: Request, identity: CustomerIdentity) -> list[dict[str, Any]]:
    customer_id = cast(str, identity.customer_id)
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            rows = await session.scalar(
                text("SELECT supportguard_api_list_tickets(:customer_id,:limit)"),
                {"customer_id": customer_id, "limit": TICKET_LIST_LIMIT},
            )
            if not isinstance(rows, list):
                raise RuntimeError("api_list_tickets_capability_invalid")
            return cast(list[dict[str, Any]], rows)
        tickets = (
            await session.scalars(
                select(SupportTicket)
                .where(SupportTicket.customer_id == identity.customer_id)
                .order_by(SupportTicket.updated_at.desc())
                .limit(TICKET_LIST_LIMIT)
            )
        ).all()
        messages = (
            (
                await session.scalars(
                    select(TicketMessage)
                    .where(
                        TicketMessage.ticket_id.in_([item.id for item in tickets]),
                        TicketMessage.role.in_(("user", "customer")),
                    )
                    .order_by(TicketMessage.created_at, TicketMessage.id)
                )
            ).all()
            if tickets
            else []
        )
        titles: dict[str, str] = {}
        for message in messages:
            titles.setdefault(message.ticket_id, " ".join(message.content.split())[:80])
        return [
            {
                "id": item.id,
                "status": item.status,
                "issue_type": item.issue_type,
                "risk": item.risk,
                "title": titles.get(item.id, "未命名工单"),
                "appendable": item.status in {"open", "needs_clarification"},
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in tickets
        ]


@router.get("/runs/{run_id}", response_model=RunProjectionResponse)
async def run_detail(
    run_id: str,
    request: Request,
    identity: CustomerIdentity,
) -> dict[str, Any]:
    customer_id = cast(str, identity.customer_id)
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text("SELECT supportguard_api_get_run(:customer_id,:run_id)"),
                {"customer_id": customer_id, "run_id": run_id},
            )
            if not isinstance(payload, dict):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
            return _public_run_projection(cast(dict[str, Any], payload))
        run = await session.get(AgentRun, run_id)
        if run is None or run.customer_id != identity.customer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        return await _run_projection(session, run)


@router.get("/runs/{run_id}/inspector", response_model=RunInspectorResponse)
async def run_inspector(
    run_id: str,
    request: Request,
    identity: CustomerIdentity,
    conversation_id: str = Query(min_length=1, max_length=64),
    turn_id: str = Query(min_length=1, max_length=64),
    message_id: str = Query(min_length=1, max_length=64),
) -> dict[str, Any]:
    customer_id = cast(str, identity.customer_id)
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text(
                    "SELECT supportguard_api_get_run_inspector("
                    ":customer_id,:conversation_id,:turn_id,:message_id,:run_id)"
                ),
                {
                    "customer_id": customer_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "run_id": run_id,
                },
            )
        else:
            payload = await _sqlite_run_inspector(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                message_id=message_id,
                run_id=run_id,
            )
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run inspector not found")
        projected = dict(payload)
        raw_run = projected.get("run")
        if not isinstance(raw_run, dict):
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "projection_invalid")
        projected["run"] = _public_run_projection(raw_run)
        raw_timeline = projected.get("timeline")
        projected["timeline"] = (
            [
                _public_event_projection(item, inspector=True)
                for item in raw_timeline
                if isinstance(item, dict)
            ]
            if isinstance(raw_timeline, list)
            else []
        )
        return projected


@router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
async def ticket_detail(
    ticket_id: str,
    request: Request,
    identity: CustomerIdentity,
) -> dict[str, Any]:
    customer_id = cast(str, identity.customer_id)
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text("SELECT supportguard_api_get_ticket(:customer_id,:ticket_id)"),
                {"customer_id": customer_id, "ticket_id": ticket_id},
            )
            if not isinstance(payload, dict):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
            return _bounded_ticket_payload(cast(dict[str, Any], payload))
        ticket = await session.get(SupportTicket, ticket_id)
        if ticket is None or ticket.customer_id != identity.customer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
        return _bounded_ticket_payload(await _sqlite_ticket_projection(session, ticket))


@router.get("/tickets/{ticket_id}/events", response_model=list[AgentEventResponse])
async def ticket_events(
    ticket_id: str,
    request: Request,
    identity: CustomerIdentity,
) -> list[dict[str, Any]]:
    customer_id = cast(str, identity.customer_id)
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text(
                    "SELECT supportguard_api_list_ticket_events("
                    ":customer_id,:ticket_id,:after_sequence,:limit)"
                ),
                {
                    "customer_id": customer_id,
                    "ticket_id": ticket_id,
                    "after_sequence": 0,
                    "limit": TICKET_EVENT_LIMIT,
                },
            )
            if payload is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
            if not isinstance(payload, list):
                raise RuntimeError("api_list_ticket_events_capability_invalid")
            return [_public_event_projection(item) for item in payload if isinstance(item, dict)]
        ticket = await session.get(SupportTicket, ticket_id)
        if ticket is None or ticket.customer_id != identity.customer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
        events = (
            await session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.ticket_id == ticket_id,
                    AgentEvent.visibility == "customer",
                )
                .order_by(AgentEvent.ticket_sequence)
                .limit(TICKET_EVENT_LIMIT)
            )
        ).all()
        return [_public_event_projection(item) for item in events]
