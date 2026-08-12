from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.timestamps import (
    format_canonical_utc_timestamp,
    parse_canonical_utc_timestamp,
    parse_database_utc_timestamp,
)
from supportguard.db.models import AuditEvent, IdempotencyRequest, SupportTicket, new_id
from supportguard.services.runtime_jobs import IdempotencyRepository, RuntimeConflict


@dataclass(frozen=True)
class LifecycleAccepted:
    conversation_id: str
    lifecycle: str
    accepted_at: datetime
    reused: bool

    def response(self) -> dict[str, object]:
        return {
            "schema_version": "conversation-lifecycle.v1",
            "conversation_id": self.conversation_id,
            "lifecycle": self.lifecycle,
            "accepted_at": format_canonical_utc_timestamp(self.accepted_at),
            "reused": self.reused,
        }


class ConversationLifecycleCoordinator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def transition(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        principal_id: str,
        conversation_id: str,
        lifecycle: str,
        idempotency_key: str,
        trace_id: str,
    ) -> LifecycleAccepted:
        if lifecycle not in {"active", "archived"}:
            raise RuntimeConflict("conversation_lifecycle_invalid")
        if self.session.get_bind().dialect.name == "postgresql":
            value = await self.session.scalar(
                text(
                    "SELECT supportguard_api_transition_conversation("
                    ":conversation_id,:lifecycle,CAST(:request AS jsonb))"
                ),
                {
                    "conversation_id": conversation_id,
                    "lifecycle": lifecycle,
                    "request": json.dumps(
                        {
                            "schema_version": "api-transition-conversation.v1",
                            "customer_id": customer_id,
                            "actor_id": principal_id,
                            "idempotency_key": idempotency_key,
                            "idempotency_id": new_id("idem"),
                            "audit_id": new_id("audit"),
                            "trace_id": trace_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            )
            if not isinstance(value, dict):
                raise RuntimeError("conversation_lifecycle_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            return LifecycleAccepted(
                conversation_id=str(value["conversation_id"]),
                lifecycle=str(value["lifecycle"]),
                accepted_at=parse_database_utc_timestamp(value["accepted_at"]),
                reused=bool(value["reused"]),
            )

        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == conversation_id,
                SupportTicket.tenant_id == tenant_id,
                SupportTicket.customer_id == customer_id,
            )
            .with_for_update()
        )
        if ticket is None:
            raise RuntimeConflict("conversation_not_found")
        action = "restore" if lifecycle == "active" else "archive"
        route = f"POST /api/conversations/{conversation_id}/{action}"
        accepted = await IdempotencyRepository(self.session).accept(
            tenant_id=tenant_id,
            principal_id=principal_id,
            route=route,
            key=idempotency_key,
            payload={"lifecycle": lifecycle},
            resource_ids={},
            response_snapshot={},
            expires_at=None,
        )
        if accepted.reused and accepted.record.response_snapshot:
            snapshot = accepted.record.response_snapshot
            return LifecycleAccepted(
                conversation_id=conversation_id,
                lifecycle=str(snapshot["lifecycle"]),
                accepted_at=parse_canonical_utc_timestamp(snapshot["accepted_at"]),
                reused=True,
            )
        if ticket.lifecycle == lifecycle:
            now = ticket.updated_at
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
        else:
            ticket.lifecycle = lifecycle
            ticket.version += 1
            now = datetime.now(UTC)
            self.session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    ticket_id=ticket.id,
                    customer_id=customer_id,
                    event_type=f"conversation_{action}d",
                    actor_type="customer",
                    actor_id=principal_id,
                    payload={"lifecycle": lifecycle},
                    trace_id=trace_id,
                )
            )
            await self.session.flush()
        result = LifecycleAccepted(ticket.id, lifecycle, now, reused=False)
        self._store_response(accepted.record, result)
        await self.session.flush()
        return result

    @staticmethod
    def _store_response(record: IdempotencyRequest, result: LifecycleAccepted) -> None:
        record.resource_ids = {"conversation_id": result.conversation_id}
        record.response_snapshot = result.response() | {"reused": False}
        record.completed_at = result.accepted_at
        record.retention_class = "low_risk_non_action"
