from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.contracts import (
    AGENT_SCHEMA_VERSION,
    CONTEXT_VERSION,
    PROMPT_VERSION,
)
from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.timestamps import (
    format_canonical_utc_timestamp,
    parse_canonical_utc_timestamp,
    parse_database_utc_timestamp,
)
from supportguard.conversation_text import conversation_title, is_standalone_greeting
from supportguard.db.models import (
    AuditEvent,
    ConversationTurn,
    Customer,
    IdempotencyRequest,
    OutboxEvent,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.policies.pii import redact_pii
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.runtime_jobs import (
    IdempotencyRepository,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.tickets import TicketService
from supportguard.services.turn_activation import activate_next_turn as activate_next_turn


@dataclass(frozen=True)
class CommandAccepted:
    ticket_id: str
    run_id: str | None
    job_id: str | None
    accepted_at: datetime
    reused: bool
    status: str = "queued"

    def response(self) -> dict[str, object]:
        return {
            "schema_version": "command-accepted.v1",
            "ticket_id": self.ticket_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "accepted_at": format_canonical_utc_timestamp(self.accepted_at),
            "status_url": f"/api/runs/{self.run_id}" if self.run_id else None,
            "events_url": f"/api/tickets/{self.ticket_id}/events/stream",
            "status": self.status,
            "reused": self.reused,
        }


class CommandCoordinator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        provider_identity: tuple[str, str, str] | None = None,
    ) -> None:
        self.session = session
        self._accepted_provider_identity = provider_identity

    def _provider_identity(self) -> tuple[str, str, str]:
        if self._accepted_provider_identity is not None:
            return self._accepted_provider_identity
        # Product API entry points must inject the lifecycle-owned provider
        # identity. A missing identity is only valid for deterministic
        # service-level fixtures and must never consult process-global config.
        return "deterministic-fake", "fake", "native_fixture"

    async def accept_new_ticket(
        self,
        *,
        customer_id: str,
        principal_id: str,
        idempotency_key: str,
        message: str,
        trace_id: str,
    ) -> CommandAccepted:
        redaction = redact_pii(message)
        message = redaction.text
        redaction_receipt = self._redaction_receipt(redaction)
        if self.session.get_bind().dialect.name == "postgresql":
            await self._set_ingress_redaction_receipt(redaction_receipt)
            model, provider_mode, tool_call_mode = self._provider_identity()
            request = {
                "schema_version": "api-accept-ticket.v1",
                "customer_id": customer_id,
                "principal_id": principal_id,
                "idempotency_key": idempotency_key,
                "message": message,
                "trace_id": trace_id,
                "idempotency_id": new_id("idem"),
                "ticket_id": new_id("ticket"),
                "message_id": new_id("msg"),
                "run_id": new_id("run"),
                "job_id": new_id("job"),
                "outbox_id": new_id("outbox"),
                "delivery_id": new_id("delivery"),
                "audit_id": new_id("audit"),
                "model": model,
                "provider_mode": provider_mode,
                "tool_call_mode": tool_call_mode,
                "prompt_version": PROMPT_VERSION,
                "agent_schema_version": AGENT_SCHEMA_VERSION,
                "context_version": CONTEXT_VERSION,
            }
            try:
                value = await self.session.scalar(
                    text("SELECT supportguard_api_accept_ticket(CAST(:request AS jsonb))"),
                    {"request": json.dumps(request, sort_keys=True, separators=(",", ":"))},
                )
            except sqlalchemy_exc.DBAPIError as exc:
                if "upgrade_in_progress" in str(exc.orig):
                    raise RuntimeConflict("upgrade_in_progress") from exc
                raise
            if not isinstance(value, dict):
                raise RuntimeError("api_accept_ticket_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            return CommandAccepted(
                ticket_id=str(value["ticket_id"]),
                run_id=str(value["run_id"]),
                job_id=str(value["job_id"]),
                accepted_at=parse_database_utc_timestamp(value["accepted_at"]),
                reused=bool(value["reused"]),
            )
        customer = await self.session.get(Customer, customer_id)
        if customer is None:
            raise RuntimeConflict("customer_not_found")
        ticket_id = new_id("ticket")
        message_id = new_id("msg")
        run_id = new_id("run")
        job_id = new_id("job")
        accepted_at = datetime.now(UTC)
        snapshot = {
            "schema_version": "command-accepted.v1",
            "ticket_id": ticket_id,
            "run_id": run_id,
            "job_id": job_id,
            "accepted_at": format_canonical_utc_timestamp(accepted_at),
            "status": "queued",
            "status_url": f"/api/runs/{run_id}",
            "events_url": f"/api/tickets/{ticket_id}/events/stream",
            "reused": False,
        }
        accepted = await IdempotencyRepository(self.session).accept(
            tenant_id=customer.tenant_id,
            principal_id=principal_id,
            route="POST /api/tickets",
            key=idempotency_key,
            payload={"message": message},
            resource_ids={"ticket_id": ticket_id, "run_id": run_id, "job_id": job_id},
            response_snapshot=snapshot,
            expires_at=accepted_at + timedelta(hours=24),
        )
        if accepted.reused:
            return self._accepted_from_record(accepted.record)
        ticket, message_record = await TicketService(self.session).create(
            customer_id=customer_id,
            message=message,
            ticket_id=ticket_id,
            message_id=message_id,
        )
        ticket.tenant_id = customer.tenant_id
        ticket.status = "queued"
        ticket.lifecycle = "active"
        ticket.automation_mode = "agent"
        ticket.title = " ".join(message.split())[:80]
        ticket.next_message_sequence = 1
        message_record.tenant_id = customer.tenant_id
        message_record.source_refs = redaction_receipt
        message_record.message_kind = "customer"
        message_record.conversation_sequence = 1
        model, provider_mode, tool_call_mode = self._provider_identity()
        turn = ConversationTurn(
            id=new_id("turn"),
            tenant_id=customer.tenant_id,
            ticket_id=ticket.id,
            customer_message_id=message_record.id,
            ordinal=1,
            activity_state="queued",
            automation_mode="agent",
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=CONTEXT_VERSION,
        )
        self.session.add(turn)
        await self.session.flush()
        message_record.turn_id = turn.id
        run = await AgentRunStore(self.session).create(
            ticket_id=ticket.id,
            customer_id=customer_id,
            message_id=message_record.id,
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=CONTEXT_VERSION,
            run_id=run_id,
        )
        run.tenant_id = customer.tenant_id
        run.status = "queued"
        run.turn_id = turn.id
        turn.run_id = run.id
        message_record.run_id = run.id
        turn.model = model
        turn.provider_mode = provider_mode
        turn.tool_call_mode = tool_call_mode
        turn.context_version = CONTEXT_VERSION
        job = await RuntimeJobRepository(self.session).create(
            job_id=job_id,
            tenant_id=customer.tenant_id,
            ticket_id=ticket.id,
            run_id=run.id,
            kind="agent_start",
        )
        outbox = OutboxEvent(
            id=new_id("outbox"),
            delivery_id=new_id("delivery"),
            tenant_id=customer.tenant_id,
            job_id=job.id,
            run_id=run.id,
            event_type="runtime_job_available",
            payload={"traceparent": trace_id},
        )
        self.session.add(outbox)
        self.session.add(
            AuditEvent(
                tenant_id=customer.tenant_id,
                ticket_id=ticket.id,
                customer_id=customer_id,
                event_type="command_accepted",
                actor_type="customer",
                actor_id=principal_id,
                run_id=run.id,
                trace_id=trace_id,
                payload={"job_id": job.id, "route": "POST /api/tickets"},
            )
        )
        await self.session.flush()
        return CommandAccepted(ticket.id, run.id, job.id, accepted_at, reused=False)

    async def _existing(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        route: str,
        key: str,
        payload: dict[str, str],
    ) -> CommandAccepted | None:
        existing = await self.session.scalar(
            select(IdempotencyRequest).where(
                IdempotencyRequest.tenant_id == tenant_id,
                IdempotencyRequest.principal_id == principal_id,
                IdempotencyRequest.route == route,
                IdempotencyRequest.idempotency_key == key,
            )
        )
        if existing is None:
            return None
        accepted = await IdempotencyRepository(self.session).accept(
            tenant_id=tenant_id,
            principal_id=principal_id,
            route=route,
            key=key,
            payload=payload,
            resource_ids={},
            response_snapshot={},
            expires_at=None,
        )
        snapshot = accepted.record.response_snapshot
        return CommandAccepted(
            ticket_id=str(snapshot["ticket_id"]),
            run_id=str(snapshot["run_id"]) if snapshot.get("run_id") else None,
            job_id=str(snapshot["job_id"]) if snapshot.get("job_id") else None,
            accepted_at=parse_canonical_utc_timestamp(snapshot["accepted_at"]),
            reused=True,
            status=str(snapshot.get("status", "queued")),
        )

    @staticmethod
    def _accepted_from_record(record: IdempotencyRequest) -> CommandAccepted:
        snapshot = record.response_snapshot
        return CommandAccepted(
            ticket_id=str(snapshot["ticket_id"]),
            run_id=str(snapshot["run_id"]) if snapshot.get("run_id") else None,
            job_id=str(snapshot["job_id"]) if snapshot.get("job_id") else None,
            accepted_at=parse_canonical_utc_timestamp(snapshot["accepted_at"]),
            reused=True,
            status=str(snapshot.get("status", "queued")),
        )

    async def accept_message(
        self,
        *,
        ticket_id: str,
        customer_id: str,
        principal_id: str,
        idempotency_key: str,
        message: str,
        trace_id: str,
    ) -> CommandAccepted:
        redaction = redact_pii(message)
        message = redaction.text
        redaction_receipt = self._redaction_receipt(redaction)
        if self.session.get_bind().dialect.name == "postgresql":
            await self._set_ingress_redaction_receipt(redaction_receipt)
            model, provider_mode, tool_call_mode = self._provider_identity()
            request = {
                "schema_version": "api-accept-message.v1",
                "customer_id": customer_id,
                "principal_id": principal_id,
                "idempotency_key": idempotency_key,
                "message": message,
                "trace_id": trace_id,
                "idempotency_id": new_id("idem"),
                "message_id": new_id("msg"),
                "run_id": new_id("run"),
                "job_id": new_id("job"),
                "outbox_id": new_id("outbox"),
                "delivery_id": new_id("delivery"),
                "audit_id": new_id("audit"),
                "model": model,
                "provider_mode": provider_mode,
                "tool_call_mode": tool_call_mode,
                "prompt_version": PROMPT_VERSION,
                "agent_schema_version": AGENT_SCHEMA_VERSION,
                "context_version": CONTEXT_VERSION,
                "conversation_title": (
                    None if is_standalone_greeting(message) else conversation_title(message)
                ),
            }
            try:
                value = await self.session.scalar(
                    text(
                        "SELECT supportguard_api_accept_conversation_message("
                        ":ticket_id,CAST(:request AS jsonb))"
                    ),
                    {
                        "ticket_id": ticket_id,
                        "request": json.dumps(request, sort_keys=True, separators=(",", ":")),
                    },
                )
            except sqlalchemy_exc.DBAPIError as exc:
                if "upgrade_in_progress" in str(exc.orig):
                    raise RuntimeConflict("upgrade_in_progress") from exc
                raise
            if not isinstance(value, dict):
                raise RuntimeError("api_accept_message_capability_invalid")
            if value.get("error_code"):
                raise RuntimeConflict(str(value["error_code"]))
            return CommandAccepted(
                ticket_id=str(value["ticket_id"]),
                run_id=str(value["run_id"]) if value.get("run_id") else None,
                job_id=str(value["job_id"]) if value.get("job_id") else None,
                accepted_at=parse_database_utc_timestamp(value["accepted_at"]),
                reused=bool(value["reused"]),
                status=str(value.get("status", "queued")),
            )
        ticket = await self.session.scalar(
            select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
        )
        if ticket is None or ticket.customer_id != customer_id:
            raise RuntimeConflict("ticket_not_found")
        route = f"POST /api/tickets/{ticket_id}/messages"
        message_id = new_id("msg")
        run_id = new_id("run")
        job_id = new_id("job")
        accepted_at = datetime.now(UTC)
        human_queue = ticket.automation_mode == "human_queue"
        scheduled = ticket.automation_mode == "agent"
        snapshot = {
            "schema_version": "command-accepted.v1",
            "ticket_id": ticket.id,
            "run_id": run_id if scheduled else None,
            "job_id": job_id if scheduled else None,
            "accepted_at": format_canonical_utc_timestamp(accepted_at),
            "status": "queued" if scheduled else "accepted",
            "status_url": f"/api/runs/{run_id}" if scheduled else None,
            "events_url": f"/api/tickets/{ticket.id}/events/stream",
            "reused": False,
        }
        accepted = await IdempotencyRepository(self.session).accept(
            tenant_id=ticket.tenant_id,
            principal_id=principal_id,
            route=route,
            key=idempotency_key,
            payload={"message": message},
            resource_ids={
                "ticket_id": ticket.id,
                **({"run_id": run_id, "job_id": job_id} if scheduled else {}),
            },
            response_snapshot=snapshot,
            expires_at=accepted_at + timedelta(hours=24),
        )
        if accepted.reused:
            return self._accepted_from_record(accepted.record)
        if ticket.lifecycle != "active":
            raise RuntimeConflict("conversation_archived")
        if is_standalone_greeting(ticket.title or "") and not is_standalone_greeting(message):
            ticket.title = conversation_title(message)
        ticket.next_message_sequence += 1
        message_record = TicketMessage(
            id=message_id,
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            role="user",
            message_kind="customer",
            content=message,
            source_refs=redaction_receipt,
            conversation_sequence=ticket.next_message_sequence,
        )
        self.session.add(message_record)
        advance_conversation_activity(ticket, occurred_at=accepted_at)
        model, provider_mode, tool_call_mode = self._provider_identity()
        turn = ConversationTurn(
            id=new_id("turn"),
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            customer_message_id=message_record.id,
            ordinal=ticket.next_message_sequence,
            activity_state="queued" if scheduled else ("completed" if human_queue else "accepted"),
            result_state="human_queue" if human_queue else None,
            automation_mode=ticket.automation_mode,
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=CONTEXT_VERSION,
        )
        self.session.add(turn)
        await self.session.flush()
        message_record.turn_id = turn.id
        if scheduled:
            ticket.status = "queued"
        ticket.version += 1
        await self.session.flush()
        if not scheduled:
            self.session.add(
                AuditEvent(
                    tenant_id=ticket.tenant_id,
                    ticket_id=ticket.id,
                    customer_id=customer_id,
                    event_type="conversation_message_accepted",
                    actor_type="customer",
                    actor_id=principal_id,
                    trace_id=trace_id,
                    payload={"turn_id": turn.id, "automation_mode": ticket.automation_mode},
                )
            )
            await self.session.flush()
            return CommandAccepted(
                ticket.id, None, None, accepted_at, reused=False, status="accepted"
            )
        run = await AgentRunStore(self.session).create(
            ticket_id=ticket.id,
            customer_id=customer_id,
            message_id=message_record.id,
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=CONTEXT_VERSION,
            run_id=run_id,
        )
        run.tenant_id = ticket.tenant_id
        run.status = "queued"
        run.turn_id = turn.id
        turn.run_id = run.id
        message_record.run_id = run.id
        turn.model = model
        turn.provider_mode = provider_mode
        turn.tool_call_mode = tool_call_mode
        turn.context_version = CONTEXT_VERSION
        job = await RuntimeJobRepository(self.session).create(
            job_id=job_id,
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            run_id=run.id,
            kind="agent_start",
        )
        self.session.add(
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
        self.session.add(
            AuditEvent(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                customer_id=customer_id,
                event_type="command_accepted",
                actor_type="customer",
                actor_id=principal_id,
                run_id=run.id,
                trace_id=trace_id,
                payload={"job_id": job.id, "route": route},
            )
        )
        await self.session.flush()
        return CommandAccepted(ticket.id, run.id, job.id, accepted_at, reused=False)

    @staticmethod
    def _redaction_receipt(result: Any) -> list[dict[str, Any]]:
        if not result.redaction_count:
            return []
        return [
            {
                "kind": "ingress_redaction_receipt",
                "count": result.redaction_count,
                "rule_ids": list(result.applied_rule_ids),
                "secret_fingerprints": list(result.secret_fingerprints),
            }
        ]

    async def _set_ingress_redaction_receipt(self, receipt: list[dict[str, Any]]) -> None:
        await self.session.scalar(
            text("SELECT set_config('app.ingress_redaction_receipt', :receipt, true)"),
            {"receipt": json.dumps(receipt, sort_keys=True, separators=(",", ":"))},
        )
