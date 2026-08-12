from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import and_, exists, or_, select, text

from supportguard.api.contracts import (
    ConversationDetailResponse,
    ConversationListResponse,
)
from supportguard.api.conversation_presentation import (
    apply_conversation_detail_presentation,
)
from supportguard.api.dependencies import (
    CustomerIdentity,
    request_session,
)
from supportguard.api.projections import (
    _apply_conversation_action_projection,
    _conversation_activity_label,
    _project_action_source,
    _public_run_projection,
    _sqlite_conversation_detail,
)
from supportguard.db.models import (
    ApprovalRequest,
    ConversationTurn,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.conversation_action_state import (
    ConversationActionStateProjectionError,
)

router = APIRouter()


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    identity: CustomerIdentity,
    query: str | None = Query(default=None, max_length=200),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=30, ge=1, le=50),
) -> dict[str, Any]:
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            value = await session.scalar(
                text(
                    "SELECT supportguard_api_list_conversations(:customer_id,:query,:cursor,:limit)"
                ),
                {
                    "customer_id": identity.customer_id,
                    "query": query,
                    "cursor": cursor,
                    "limit": limit,
                },
            )
            if not isinstance(value, dict):
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "projection_invalid")
            return value
        statement = select(SupportTicket).where(SupportTicket.customer_id == identity.customer_id)
        if query:
            pattern = f"%{query.strip()}%"
            statement = statement.where(
                or_(
                    SupportTicket.title.ilike(pattern),
                    exists(
                        select(TicketMessage.id).where(
                            TicketMessage.ticket_id == SupportTicket.id,
                            TicketMessage.content.ilike(pattern),
                        )
                    ),
                )
            )
        if cursor:
            cursor_ticket = await session.scalar(
                select(SupportTicket).where(
                    SupportTicket.id == cursor,
                    SupportTicket.customer_id == identity.customer_id,
                )
            )
            if cursor_ticket is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "cursor_invalid")
            statement = statement.where(
                or_(
                    SupportTicket.last_message_at < cursor_ticket.last_message_at,
                    and_(
                        SupportTicket.last_message_at == cursor_ticket.last_message_at,
                        SupportTicket.id < cursor_ticket.id,
                    ),
                )
            )
        tickets = list(
            (
                await session.scalars(
                    statement.order_by(
                        SupportTicket.last_message_at.desc(), SupportTicket.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
        selected = tickets[:limit]
        items: list[dict[str, Any]] = []
        for ticket in selected:
            turns = (
                await session.scalars(
                    select(ConversationTurn).where(ConversationTurn.ticket_id == ticket.id)
                )
            ).all()
            approvals = (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.ticket_id == ticket.id)
                )
            ).all()
            latest_message = await session.scalar(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == ticket.id)
                .order_by(TicketMessage.conversation_sequence.desc(), TicketMessage.id.desc())
                .limit(1)
            )
            items.append(
                {
                    "id": ticket.id,
                    "title": ticket.title or "未命名对话",
                    "lifecycle": ticket.lifecycle,
                    "automation_mode": ticket.automation_mode,
                    "activity_label": _conversation_activity_label(
                        list(turns),
                        list(approvals),
                        lifecycle=ticket.lifecycle,
                        automation_mode=ticket.automation_mode,
                    ),
                    "pending_action_count": sum(
                        item.status in {"pending", "approved"} for item in approvals
                    ),
                    "latest_summary": (
                        " ".join(latest_message.content.split())[:120]
                        if latest_message is not None
                        else None
                    ),
                    "updated_at": ticket.last_message_at,
                }
            )
        return {
            "items": items,
            "next_cursor": selected[-1].id if len(tickets) > limit else None,
        }


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str,
    request: Request,
    identity: CustomerIdentity,
    before_turn: int | None = Query(default=None, ge=1),
    turn_limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            value = await session.scalar(
                text(
                    "SELECT supportguard_api_get_conversation_page("
                    ":customer_id,:conversation_id,:before_turn,:turn_limit)"
                ),
                {
                    "customer_id": identity.customer_id,
                    "conversation_id": conversation_id,
                    "before_turn": before_turn,
                    "turn_limit": turn_limit,
                },
            )
            if value is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
            if not isinstance(value, dict):
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "projection_invalid")
            turns = value.get("turns", [])
            if isinstance(turns, list):
                for turn in turns:
                    if not isinstance(turn, dict):
                        continue
                    raw_run = turn.get("run")
                    if isinstance(raw_run, dict):
                        turn["run"] = _public_run_projection(raw_run)
            source_payloads = value.pop("conversation_action_sources", None)
            if not isinstance(source_payloads, list):
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "projection_invalid",
                )
            try:
                action_states = tuple(
                    _project_action_source(
                        item,
                        tenant_id=identity.tenant_id,
                        customer_id=cast(str, identity.customer_id),
                    )
                    for item in source_payloads
                )
            except ConversationActionStateProjectionError as exc:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "projection_invalid",
                ) from exc
            projected = _apply_conversation_action_projection(value, action_states)
            return apply_conversation_detail_presentation(projected)
        ticket = await session.scalar(
            select(SupportTicket).where(
                SupportTicket.id == conversation_id,
                SupportTicket.customer_id == identity.customer_id,
            )
        )
        if ticket is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        value = await _sqlite_conversation_detail(session, ticket)
        eligible = [
            turn for turn in value["turns"] if before_turn is None or turn["ordinal"] < before_turn
        ]
        selected = eligible[-turn_limit:]
        value["turns"] = selected
        value["turn_pagination"] = {
            "limit": turn_limit,
            "returned": len(selected),
            "has_more": len(eligible) > len(selected),
            "next_before_ordinal": selected[0]["ordinal"]
            if len(eligible) > len(selected)
            else None,
        }
        return apply_conversation_detail_presentation(value)
