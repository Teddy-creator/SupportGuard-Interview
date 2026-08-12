from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.contracts import (
    AGENT_SCHEMA_VERSION,
    PROMPT_VERSION,
    runtime_provenance,
)
from supportguard.contracts.context import worker_execution_context
from supportguard.db.models import AgentEvent, AgentRun, SupportTicket, TicketMessage, new_id
from supportguard.db.session import runtime_code_version
from supportguard.policies.pii import redact_pii

Visibility = Literal["customer", "approver", "internal"]
GENESIS_EVENT_HASH = "0" * 64
EVENT_SCHEMA_VERSION = "support-ticket-event.v1"
CANONICALIZATION_VERSION = "json-sort-keys.v1"
EVENT_HASH_SCHEMA_VERSION = "event-hash.v1"


class CanonicalEventHeadConflict(RuntimeError):
    """The caller's canonical ticket head no longer matches durable state."""


_TYPED_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_.:/-]{1,511}$")
_TYPED_DIGEST = re.compile(r"^[0-9a-f]{32,128}$")


def _trusted_structural_string(*, key: str | None, value: str) -> bool:
    """Keep validated internal identities stable while free text remains redacted."""

    if key is None:
        return False
    normalized = key.lower()
    if normalized == "id" or normalized.endswith(("_id", "_ids")):
        return bool(_TYPED_IDENTIFIER.fullmatch(value))
    if normalized.endswith("_hash"):
        return bool(_TYPED_DIGEST.fullmatch(value))
    return False


def _safe_payload(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, str):
        if _trusted_structural_string(key=key, value=value):
            return value[:4000]
        return redact_pii(value).text[:4000]
    if isinstance(value, dict):
        return {
            str(item_key): _safe_payload(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).lower()
            not in {"prompt", "system_prompt", "chain_of_thought", "reasoning_content", "api_key"}
        }
    if isinstance(value, list):
        return [_safe_payload(item, key=key) for item in value[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _canonical_created_at(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat(timespec="microseconds")


def _payload_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _event_envelope(event: AgentEvent) -> dict[str, Any]:
    return {
        "aggregate_type": "SupportTicket",
        "aggregate_id": event.ticket_id,
        "tenant_id": event.tenant_id,
        "customer_id": event.customer_id,
        "event_id": event.id,
        "ticket_sequence": event.ticket_sequence,
        "run_id": event.run_id,
        "run_sequence": event.run_sequence,
        "job_id": event.job_id,
        "delivery_generation": event.delivery_generation,
        "fencing_token": event.fencing_token,
        "step_index": event.step_index,
        "tool_round": event.tool_round,
        "event_type": event.event_type,
        "status": event.status,
        "visibility": event.visibility,
        "tool_call_id": event.tool_call_id,
        "payload_hash": event.payload_hash,
        "previous_event_id": event.previous_event_id,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "idempotency_id": event.idempotency_id,
        "created_at": _canonical_created_at(event.created_at),
        "event_schema_version": event.event_schema_version,
        "canonicalization_version": event.canonicalization_version,
    }


def _event_hash(
    *,
    event: AgentEvent,
    previous_event_hash: str,
) -> str:
    return hashlib.sha256(
        event.event_hash_schema_version.encode()
        + _canonical_json(_event_envelope(event))
        + previous_event_hash.encode()
    ).hexdigest()


async def verify_ticket_event_chain(session: AsyncSession, ticket_id: str) -> str:
    events = list(
        (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.ticket_id == ticket_id)
                .order_by(AgentEvent.ticket_sequence)
            )
        ).all()
    )
    if not events:
        raise RuntimeError("ticket_event_chain_empty")
    parent_hash = GENESIS_EVENT_HASH
    parent_id: str | None = None
    parent_run_id: str | None = None
    for expected_sequence, event in enumerate(events, start=1):
        expected_hash = _event_hash(event=event, previous_event_hash=parent_hash)
        expected_parent_id = parent_id if parent_run_id == event.run_id else None
        if (
            event.ticket_sequence != expected_sequence
            or event.parent_event_hash != parent_hash
            or event.previous_event_id != expected_parent_id
            or event.payload_hash != _payload_hash(event.payload)
            or event.event_schema_version != EVENT_SCHEMA_VERSION
            or event.canonicalization_version != CANONICALIZATION_VERSION
            or event.event_hash_schema_version != EVENT_HASH_SCHEMA_VERSION
            or event.correlation_id != event.run_id
            or event.event_hash != expected_hash
        ):
            raise RuntimeError("ticket_event_chain_invalid")
        parent_hash = event.event_hash
        parent_id = event.id
        parent_run_id = event.run_id
    if parent_hash is None:
        raise RuntimeError("ticket_event_chain_empty")
    return parent_hash


class AgentRunStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        ticket_id: str,
        customer_id: str,
        message_id: str,
        model: str,
        provider_mode: str,
        tool_call_mode: str,
        context_version: str,
        run_id: str | None = None,
    ) -> AgentRun:
        message = await self.session.scalar(
            select(TicketMessage).where(
                TicketMessage.id == message_id,
                TicketMessage.ticket_id == ticket_id,
            )
        )
        ticket = await self.session.scalar(
            select(SupportTicket).where(
                SupportTicket.id == ticket_id,
                SupportTicket.customer_id == customer_id,
            )
        )
        if message is None or ticket is None or message.tenant_id != ticket.tenant_id:
            raise ValueError("Agent Run must bind an existing message from the same ticket")
        existing = await self.session.scalar(
            select(AgentRun).where(AgentRun.message_id == message_id)
        )
        if existing is not None:
            return existing
        accepted_at = datetime.now(UTC)
        run = AgentRun(
            id=run_id or new_id("run"),
            tenant_id=ticket.tenant_id,
            ticket_id=ticket_id,
            customer_id=customer_id,
            message_id=message_id,
            status="running",
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            prompt_version=PROMPT_VERSION,
            schema_version=AGENT_SCHEMA_VERSION,
            context_version=context_version,
            # SQLite's CURRENT_TIMESTAMP has only second precision. Persist the
            # causal acceptance time explicitly so two customer turns cannot be
            # ordered by their random IDs in test/demo mode. PostgreSQL command
            # capabilities already use clock_timestamp() for the same purpose.
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        self.session.add(run)
        await self.session.flush()
        provenance = runtime_provenance(
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=context_version,
            code_version=runtime_code_version(self.session),
        )
        await self.append_event(
            run,
            event_type="run_started",
            visibility="customer",
            payload={
                "provider_mode": provider_mode,
                "tool_call_mode": tool_call_mode,
                "prompt_version": provenance["prompt_version"],
                "schema_version": provenance["schema_version"],
                "runtime_manifest_hash": provenance["runtime_manifest_hash"],
                "code_commit": provenance["code_commit"],
            },
        )
        return run

    async def append_event(
        self,
        run: AgentRun,
        *,
        event_type: str,
        payload: dict[str, Any],
        visibility: Visibility = "internal",
        status: str = "completed",
        tool_call_id: str | None = None,
        step_index: int | None = None,
        tool_round: int | None = None,
        idempotency_id: str | None = None,
        expected_ticket_head_event_id: str | None | object = ...,
        expected_ticket_sequence: int | None = None,
        expected_ticket_event_hash: str | None | object = ...,
    ) -> AgentEvent:
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(SupportTicket.id == run.ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        locked_run = await self.session.scalar(
            select(AgentRun)
            .where(AgentRun.id == run.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if ticket is None or locked_run is None:
            raise RuntimeError("event owner disappeared")
        parent = await self.session.scalar(
            select(AgentEvent)
            .where(AgentEvent.ticket_id == run.ticket_id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        actual_id = parent.id if parent is not None else None
        actual_sequence = parent.ticket_sequence if parent is not None else 0
        actual_hash = parent.event_hash if parent is not None else None
        if ticket.next_event_sequence != actual_sequence:
            raise CanonicalEventHeadConflict("ticket_event_sequence_counter_conflict")
        if expected_ticket_head_event_id is not ... and actual_id != expected_ticket_head_event_id:
            raise CanonicalEventHeadConflict("ticket_event_head_id_conflict")
        if expected_ticket_sequence is not None and actual_sequence != expected_ticket_sequence:
            raise CanonicalEventHeadConflict("ticket_event_head_sequence_conflict")
        if expected_ticket_event_hash is not ... and actual_hash != expected_ticket_event_hash:
            raise CanonicalEventHeadConflict("ticket_event_head_hash_conflict")
        run_parent_sequence = await self.session.scalar(
            select(AgentEvent.run_sequence)
            .where(AgentEvent.run_id == run.id)
            .order_by(AgentEvent.run_sequence.desc())
            .limit(1)
        )
        if locked_run.next_run_sequence != (run_parent_sequence or 0):
            raise CanonicalEventHeadConflict("run_event_sequence_counter_conflict")
        ticket.next_event_sequence += 1
        locked_run.next_run_sequence += 1
        ticket_sequence = ticket.next_event_sequence
        run_sequence = locked_run.next_run_sequence
        safe_payload = _safe_payload(payload)
        parent_hash = parent.event_hash if parent is not None else GENESIS_EVENT_HASH
        try:
            worker = worker_execution_context.get()
        except RuntimeError:
            worker = None
        if worker is not None and (
            worker.tenant_id != run.tenant_id
            or worker.ticket_id != run.ticket_id
            or worker.run_id != run.id
        ):
            raise RuntimeError("worker event context does not match event aggregate")
        same_run_parent_id = parent.id if parent is not None and parent.run_id == run.id else None
        event = AgentEvent(
            id=new_id("event"),
            tenant_id=run.tenant_id,
            run_id=run.id,
            ticket_id=run.ticket_id,
            customer_id=run.customer_id,
            sequence=run_sequence,
            ticket_sequence=ticket_sequence,
            run_sequence=run_sequence,
            step_index=run.step_index if step_index is None else step_index,
            tool_round=run.tool_rounds if tool_round is None else tool_round,
            event_type=event_type,
            status=status,
            visibility=visibility,
            tool_call_id=tool_call_id,
            payload=safe_payload,
            payload_hash=_payload_hash(safe_payload),
            previous_event_id=same_run_parent_id,
            parent_event_hash=parent_hash,
            event_hash="",
            event_schema_version=EVENT_SCHEMA_VERSION,
            canonicalization_version=CANONICALIZATION_VERSION,
            event_hash_schema_version=EVENT_HASH_SCHEMA_VERSION,
            correlation_id=run.id,
            causation_id=parent.id if parent is not None else None,
            idempotency_id=idempotency_id,
            job_id=worker.job_id if worker is not None else None,
            delivery_generation=worker.delivery_generation if worker is not None else None,
            fencing_token=worker.fencing_token if worker is not None else None,
            created_at=datetime.now(UTC),
        )
        event.event_hash = _event_hash(event=event, previous_event_hash=parent_hash)
        self.session.add(event)
        run.step_index = max(run.step_index, event.step_index)
        await self.session.flush()
        return event

    async def assert_ticket_head(
        self,
        run: AgentRun,
        *,
        expected_ticket_head_event_id: str | None,
        expected_ticket_sequence: int,
        expected_ticket_event_hash: str | None,
    ) -> AgentEvent | None:
        """Lock and compare the durable aggregate head without advancing it."""
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(SupportTicket.id == run.ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        parent = await self.session.scalar(
            select(AgentEvent)
            .where(AgentEvent.ticket_id == run.ticket_id)
            .order_by(AgentEvent.ticket_sequence.desc())
            .limit(1)
        )
        if ticket is None:
            raise RuntimeError("event owner disappeared")
        actual_id = parent.id if parent is not None else None
        actual_sequence = parent.ticket_sequence if parent is not None else 0
        actual_hash = parent.event_hash if parent is not None else None
        if (
            ticket.next_event_sequence != actual_sequence
            or actual_id != expected_ticket_head_event_id
            or actual_sequence != expected_ticket_sequence
            or actual_hash != expected_ticket_event_hash
        ):
            raise CanonicalEventHeadConflict("ticket_event_head_conflict")
        return parent

    async def transition(
        self,
        run: AgentRun,
        *,
        status: Literal["running", "interrupted", "completed", "failed"],
        checkpoint_stage: str,
        checkpoint_id: str | None = None,
        agent_finish_reason: str | None = None,
        error_code: str | None = None,
        tool_rounds: int | None = None,
        tool_attempts: int | None = None,
        llm_calls: int | None = None,
    ) -> None:
        run.status = status
        run.checkpoint_stage = checkpoint_stage
        run.checkpoint_id = checkpoint_id
        run.agent_finish_reason = agent_finish_reason
        run.error_code = error_code
        if tool_rounds is not None:
            run.tool_rounds = max(run.tool_rounds, tool_rounds)
        if tool_attempts is not None:
            run.tool_attempts = max(run.tool_attempts, tool_attempts)
        if llm_calls is not None:
            run.llm_calls = max(run.llm_calls, llm_calls)
        if status in {"completed", "failed"}:
            run.completed_at = datetime.now(UTC)
        await self.session.flush()
