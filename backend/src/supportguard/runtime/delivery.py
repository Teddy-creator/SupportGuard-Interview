from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from redis.asyncio import Redis
from redis.typing import FieldT
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard.contracts.event_channels import ticket_event_channel
from supportguard.contracts.queue import RuntimeJobMessage
from supportguard.db.models import (
    AgentRun,
    InboxDelivery,
    OutboxEvent,
    QueueDeliveryAudit,
    RuntimeJob,
)
from supportguard.db.scope import set_local_scope
from supportguard.observability.metrics import JOB_OUTCOMES, OUTBOX_PUBLISH
from supportguard.observability.tracing import CONSUMER, PRODUCER, extracted_context, tracer
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.runtime_jobs import (
    FinalizerCommitUnknown,
    FinalizerRestartRequired,
    JobLease,
    RuntimeConflict,
    RuntimeJobRepository,
)
from supportguard.services.runtime_timing import RuntimeTiming
from supportguard.services.writer_barrier import cross_store_writer_barrier

if TYPE_CHECKING:
    from supportguard.services.heartbeats import ServiceHeartbeatSnapshot

logger = logging.getLogger(__name__)

CONTROL_LOOP_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class ServiceLoopProgress:
    """Bounded, non-secret progress state for one long-running control loop."""

    service: str
    last_completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    active_since: datetime | None = None
    completed_iterations: int = 0

    def started(self) -> None:
        self.active_since = datetime.now(UTC)

    def completed(self) -> None:
        self.last_completed_at = datetime.now(UTC)
        self.active_since = None
        self.completed_iterations += 1

    def progress_age_seconds(self, *, now: datetime | None = None) -> float:
        observed_at = now or datetime.now(UTC)
        reference = self.active_since or self.last_completed_at
        return max(0.0, (observed_at - reference).total_seconds())


def _validate_worker_finish_result(
    finished: object,
    *,
    requested_outcome: str,
) -> dict[str, Any]:
    if not isinstance(finished, dict):
        raise RuntimeError("worker finish capability returned an invalid result")
    technical_failure = requested_outcome == "failed" or requested_outcome.startswith(
        ("failed:", "domain_terminal:", "terminal_failed:")
    )
    finish_created_human_queue = (
        finished.get("outcome") == "manual_takeover"
        or finished.get("ticket_status") == "manual_takeover"
        or finished.get("automation_mode") == "human_queue"
    )
    if technical_failure and finish_created_human_queue:
        raise RuntimeError("worker finish converted failure to manual_takeover")
    activated_turn_id = finished.get("activated_turn_id")
    if activated_turn_id is not None and (
        finished.get("activated_run_id") is None or finished.get("activated_job_id") is None
    ):
        raise RuntimeError("worker finish returned a partial Turn activation")
    return finished


async def bounded_stream_add(
    redis: Redis,
    *,
    stream: str,
    fields: dict[FieldT, FieldT],
    maxlen: int,
) -> Any:
    """Compatibility wrapper that deliberately never trims a durable delivery stream."""
    del maxlen
    return await redis.xadd(stream, fields)


@dataclass(frozen=True, slots=True)
class _DeliveryEnvelope:
    redis_id: str | bytes
    redis_id_text: str
    payload_hash: str
    message: RuntimeJobMessage


@dataclass(frozen=True, slots=True)
class _DeliveryAdmission:
    message: RuntimeJobMessage
    lease: JobLease | None
    should_ack_terminal: bool


class OutboxDispatcher:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        stream: str,
        stream_maxlen: int = 10_000,
    ) -> None:
        self.factory = factory
        self.redis = redis
        self.stream = stream
        self.stream_maxlen = stream_maxlen

    async def dispatch_once(
        self,
        *,
        batch_size: int = 50,
        _after_claim: Callable[[str], Awaitable[None]] | None = None,
    ) -> int:
        async with self.factory() as probe:
            is_postgresql = probe.get_bind().dialect.name == "postgresql"
        if is_postgresql:
            async with cross_store_writer_barrier(
                self.factory,
                operation="dispatcher",
            ):
                return await self._dispatch_postgresql(
                    batch_size=batch_size,
                    _after_claim=_after_claim,
                )
        claimed_ids: list[str] = []
        async with self.factory() as session, session.begin():
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.available_at <= datetime.now(UTC),
                    )
                    .order_by(OutboxEvent.created_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for event in events:
                event.publish_attempts += 1
                event.available_at = datetime.now(UTC) + timedelta(seconds=30)
                claimed_ids.append(event.id)
        published = 0
        for event_id in claimed_ids:
            async with self.factory() as session:
                loaded_event = await session.get(OutboxEvent, event_id)
                if loaded_event is None or loaded_event.published_at is not None:
                    continue
                message = RuntimeJobMessage(
                    event_id=loaded_event.id,
                    delivery_id=loaded_event.delivery_id,
                    job_id=loaded_event.job_id,
                    run_id=loaded_event.run_id,
                    tenant_id=loaded_event.tenant_id,
                    delivery_generation=loaded_event.delivery_generation,
                    traceparent=loaded_event.payload.get("traceparent"),
                )
            try:
                with tracer().start_as_current_span(
                    "redis.xadd",
                    context=extracted_context(message.traceparent),
                    kind=PRODUCER,
                    attributes={"messaging.system": "redis"},
                ):
                    redis_message_id = await bounded_stream_add(
                        self.redis,
                        stream=self.stream,
                        fields=message.redis_fields(),
                        maxlen=self.stream_maxlen,
                    )
            except Exception:
                OUTBOX_PUBLISH.labels("failed").inc()
                logger.exception("outbox_publish_failed", extra={"event_id": event_id})
                continue
            async with self.factory() as session, session.begin():
                locked = await session.scalar(
                    select(OutboxEvent)
                    .where(OutboxEvent.id == event_id, OutboxEvent.published_at.is_(None))
                    .with_for_update()
                )
                if locked is not None:
                    locked.redis_message_id = (
                        redis_message_id.decode()
                        if isinstance(redis_message_id, bytes)
                        else str(redis_message_id)
                    )
                    locked.published_at = datetime.now(UTC)
                    locked.last_delivery_at = locked.published_at
                    published += 1
                    OUTBOX_PUBLISH.labels("succeeded").inc()
        return published

    async def _dispatch_postgresql(
        self,
        *,
        batch_size: int,
        _after_claim: Callable[[str], Awaitable[None]] | None = None,
    ) -> int:
        async with self.factory() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        text("SELECT * FROM supportguard_dispatcher_claim_outbox(:batch_size)"),
                        {"batch_size": batch_size},
                    )
                )
                .mappings()
                .all()
            )
        published = 0
        for row in rows:
            if _after_claim is not None:
                await _after_claim(str(row["event_id"]))
            message = RuntimeJobMessage(
                event_id=str(row["event_id"]),
                delivery_id=str(row["delivery_id"]),
                job_id=str(row["job_id"]),
                run_id=str(row["run_id"]),
                tenant_id=str(row["tenant_id"]),
                delivery_generation=int(row["delivery_generation"]),
                traceparent=str(row["traceparent"]) if row["traceparent"] else None,
            )
            try:
                with tracer().start_as_current_span(
                    "redis.xadd",
                    context=extracted_context(message.traceparent),
                    kind=PRODUCER,
                    attributes={"messaging.system": "redis"},
                ):
                    redis_message_id = await bounded_stream_add(
                        self.redis,
                        stream=self.stream,
                        fields=message.redis_fields(),
                        maxlen=self.stream_maxlen,
                    )
            except Exception:
                OUTBOX_PUBLISH.labels("failed").inc()
                logger.exception("outbox_publish_failed", extra={"event_id": message.event_id})
                continue
            redis_id = (
                redis_message_id.decode()
                if isinstance(redis_message_id, bytes)
                else str(redis_message_id)
            )
            async with self.factory() as session, session.begin():
                marked = await session.scalar(
                    text(
                        "SELECT supportguard_dispatcher_mark_published(:event_id,:redis_message_id)"
                    ),
                    {"event_id": message.event_id, "redis_message_id": redis_id},
                )
            if bool(marked):
                published += 1
                OUTBOX_PUBLISH.labels("succeeded").inc()
        return published


async def ensure_consumer_group(redis: Redis, *, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def record_delivery(
    session: AsyncSession,
    *,
    message: RuntimeJobMessage,
    redis_message_id: str,
    consumer_group: str,
) -> tuple[InboxDelivery, bool]:
    existing = await session.scalar(
        select(InboxDelivery).where(
            InboxDelivery.consumer_group == consumer_group,
            InboxDelivery.delivery_id == message.delivery_id,
        )
    )
    if existing is not None:
        return existing, True
    delivery = InboxDelivery(
        tenant_id=message.tenant_id,
        job_id=message.job_id,
        delivery_id=message.delivery_id,
        redis_message_id=redis_message_id,
        consumer_group=consumer_group,
        status="received",
    )
    session.add(delivery)
    await session.flush()
    return delivery, False


JobHandler = Callable[[RuntimeJobMessage, JobLease], Awaitable[str]]


class RuntimeWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        stream: str,
        group: str,
        consumer: str,
        handler: JobHandler,
        timing: RuntimeTiming | None = None,
    ) -> None:
        self.factory = factory
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.handler = handler
        self.timing = timing
        self._reclaim_cursor = "0-0"
        self._prefer_reclaim = False
        self.last_progress_at = datetime.now(UTC)
        self.last_lease_heartbeat_at: datetime | None = None
        self.heartbeat_snapshot_provider: Callable[[], ServiceHeartbeatSnapshot] | None = None

    async def _run_handler_with_heartbeat(
        self,
        message: RuntimeJobMessage,
        lease: JobLease,
    ) -> tuple[str, Exception | None]:
        """Run one lease while guaranteeing both owned child tasks are drained."""

        handler_task: asyncio.Future[str] = asyncio.ensure_future(self.handler(message, lease))
        heartbeat_task = asyncio.create_task(self._heartbeat(lease))
        owned_tasks: tuple[asyncio.Future[Any], ...] = (handler_task, heartbeat_task)
        try:
            done, _ = await asyncio.wait(
                set(owned_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handler_task in done:
                try:
                    return handler_task.result(), None
                except Exception as exc:
                    return "failed", exc
            heartbeat_error = heartbeat_task.exception()
            failure = (
                heartbeat_error
                if isinstance(heartbeat_error, Exception)
                else RuntimeConflict("heartbeat_stopped")
            )
            return "failed", failure
        finally:
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)

    async def _read_new(self, block_ms: int) -> list[Any]:
        batches = await self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=1,
            block=block_ms,
        )
        return list(batches)

    async def _reclaim_old(self) -> list[Any]:
        reclaimed = await self.redis.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=(self.timing.pel_min_idle_ms if self.timing else 35_000),
            start_id=self._reclaim_cursor,
            count=1,
        )
        if reclaimed:
            cursor = reclaimed[0]
            self._reclaim_cursor = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
        entries = reclaimed[1] if len(reclaimed) > 1 else []
        return [(self.stream, entries)] if entries else []

    async def consume_once(
        self,
        *,
        block_ms: int = 1000,
        _after_accept: Callable[[JobLease], Awaitable[None]] | None = None,
    ) -> int:
        async with self.factory() as probe:
            is_postgresql = probe.get_bind().dialect.name == "postgresql"
        if is_postgresql:
            async with cross_store_writer_barrier(
                self.factory,
                operation="worker",
            ):
                result = await self._consume_once(
                    block_ms=block_ms,
                    _after_accept=_after_accept,
                )
        else:
            result = await self._consume_once(
                block_ms=block_ms,
                _after_accept=_after_accept,
            )
        self.last_progress_at = datetime.now(UTC)
        return result

    async def _next_delivery_entry(
        self,
        *,
        block_ms: int,
    ) -> tuple[str | bytes, dict[bytes | str, bytes | str]] | None:
        await ensure_consumer_group(self.redis, stream=self.stream, group=self.group)
        first_reclaim = self._prefer_reclaim
        batches = await self._reclaim_old() if first_reclaim else await self._read_new(block_ms)
        self._prefer_reclaim = not self._prefer_reclaim
        if not batches:
            batches = await self._read_new(block_ms) if first_reclaim else await self._reclaim_old()
        if not batches:
            return None
        _, entries = batches[0]
        return cast(tuple[str | bytes, dict[bytes | str, bytes | str]], entries[0])

    async def _decode_delivery(
        self,
        redis_id: str | bytes,
        fields: dict[bytes | str, bytes | str],
    ) -> _DeliveryEnvelope | None:
        redis_id_text = redis_id.decode() if isinstance(redis_id, bytes) else str(redis_id)
        raw_payload = fields.get(b"payload", fields.get("payload", b""))
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode(errors="replace")
        payload_hash = hashlib.sha256(str(raw_payload).encode()).hexdigest()
        try:
            message = RuntimeJobMessage.from_redis(fields)
        except Exception as exc:
            async with self.factory() as session, session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    await session.execute(
                        text("SELECT supportguard_worker_record_poison(CAST(:payload AS jsonb))"),
                        {
                            "payload": json.dumps(
                                {
                                    "schema_version": "worker-poison.v1",
                                    "redis_message_id": redis_id_text,
                                    "consumer_group": self.group,
                                    "payload_hash": payload_hash,
                                    "error_type": type(exc).__name__,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        },
                    )
                else:
                    session.add(
                        QueueDeliveryAudit(
                            redis_message_id=redis_id_text,
                            consumer_group=self.group,
                            outcome="poison_invalid_schema",
                            payload_hash=payload_hash,
                            details={"error_type": type(exc).__name__},
                        )
                    )
            await self.redis.xack(self.stream, self.group, redis_id)
            return None
        return _DeliveryEnvelope(redis_id, redis_id_text, payload_hash, message)

    async def _accept_postgresql_delivery(
        self,
        session: AsyncSession,
        envelope: _DeliveryEnvelope,
    ) -> _DeliveryAdmission:
        message = envelope.message
        accepted = await session.scalar(
            text("SELECT supportguard_worker_accept_delivery(CAST(:payload AS jsonb))"),
            {
                "payload": json.dumps(
                    {
                        "schema_version": "worker-delivery.v1",
                        "event_id": message.event_id,
                        "delivery_id": message.delivery_id,
                        "job_id": message.job_id,
                        "run_id": message.run_id,
                        "tenant_id": message.tenant_id,
                        "generation": message.delivery_generation,
                        "redis_message_id": envelope.redis_id_text,
                        "consumer_group": self.group,
                        "owner": self.consumer,
                        "payload_hash": envelope.payload_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        if not isinstance(accepted, dict):
            raise RuntimeError("worker control plane returned an invalid result")
        lease: JobLease | None = None
        if accepted.get("result") == "claimed":
            if accepted.get("ticket_id") is None or accepted.get("dispatch_sequence") is None:
                raise RuntimeError("worker claim omitted Ticket dispatch identity")
            lease = JobLease(
                job_id=str(accepted["job_id"]),
                run_id=str(accepted["run_id"]),
                tenant_id=str(accepted["tenant_id"]),
                owner=str(accepted["owner"]),
                fencing_token=int(accepted["fencing_token"]),
                expires_at=datetime.fromisoformat(str(accepted["expires_at"])),
                kind=str(accepted["kind"]),
                approval_id=(
                    str(accepted["approval_id"])
                    if accepted.get("approval_id") is not None
                    else None
                ),
                attempt=int(accepted["attempt"]),
                ticket_id=str(accepted["ticket_id"]),
                dispatch_sequence=int(accepted["dispatch_sequence"]),
            )
            message = message.model_copy(
                update={"tenant_id": lease.tenant_id, "run_id": lease.run_id}
            )
        return _DeliveryAdmission(message, lease, bool(accepted.get("ack", False)))

    async def _accept_sqlite_delivery(
        self,
        session: AsyncSession,
        envelope: _DeliveryEnvelope,
    ) -> _DeliveryAdmission:
        message = envelope.message
        trusted_job = await session.scalar(
            select(RuntimeJob).where(RuntimeJob.id == message.job_id).with_for_update()
        )
        mismatch = (
            trusted_job is None
            or trusted_job.tenant_id != message.tenant_id
            or trusted_job.run_id != message.run_id
            or trusted_job.kind not in {"agent_start", "approval_resume"}
        )
        if mismatch:
            session.add(
                QueueDeliveryAudit(
                    tenant_id=trusted_job.tenant_id if trusted_job else None,
                    job_id=message.job_id,
                    delivery_id=message.delivery_id,
                    redis_message_id=envelope.redis_id_text,
                    consumer_group=self.group,
                    outcome="rejected_untrusted_envelope",
                    payload_hash=envelope.payload_hash,
                    details={
                        "job_exists": trusted_job is not None,
                        "tenant_match": bool(
                            trusted_job and trusted_job.tenant_id == message.tenant_id
                        ),
                        "run_match": bool(trusted_job and trusted_job.run_id == message.run_id),
                    },
                )
            )
            return _DeliveryAdmission(message, None, True)
        if trusted_job is None:
            raise RuntimeError("trusted job validation invariant failed")
        message = message.model_copy(
            update={"tenant_id": trusted_job.tenant_id, "run_id": trusted_job.run_id}
        )
        delivery, reused = await record_delivery(
            session,
            message=message,
            redis_message_id=envelope.redis_id_text,
            consumer_group=self.group,
        )
        if reused and delivery.status == "acked":
            return _DeliveryAdmission(message, None, True)
        try:
            lease = await RuntimeJobRepository(session).claim(
                job_id=message.job_id,
                owner=self.consumer,
                lease_seconds=int(self.timing.job_lease.total_seconds() if self.timing else 30),
            )
        except RuntimeConflict:
            job = await session.get(RuntimeJob, message.job_id)
            if job is not None and job.status in {"succeeded", "dead"}:
                delivery.status = "acked"
                delivery.outcome = "already_terminal"
                should_ack_terminal = True
            else:
                delivery.status = "rejected"
                delivery.outcome = "not_claimable"
                should_ack_terminal = False
            lease = None
        else:
            delivery.status = "claimed"
            should_ack_terminal = False
        return _DeliveryAdmission(message, lease, should_ack_terminal)

    async def _accept_delivery(self, envelope: _DeliveryEnvelope) -> _DeliveryAdmission:
        async with self.factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                return await self._accept_postgresql_delivery(session, envelope)
            return await self._accept_sqlite_delivery(session, envelope)

    async def _run_delivery_handler(
        self,
        admission: _DeliveryAdmission,
    ) -> tuple[str, Exception | None]:
        lease = admission.lease
        if lease is None:
            raise RuntimeError("delivery admission omitted a lease")
        message = admission.message
        with tracer().start_as_current_span(
            "runtime.job.consume",
            context=extracted_context(message.traceparent),
            kind=CONSUMER,
            attributes={"messaging.system": "redis", "runtime.job.kind": "agent"},
        ):
            outcome, failure = await self._run_handler_with_heartbeat(message, lease)
            if failure is not None:
                logger.error(
                    "runtime_job_handler_failed",
                    extra={
                        "event": "runtime_job_handler_failed",
                        "run_id": message.run_id,
                        "job_id": message.job_id,
                        "attempt": lease.fencing_token,
                        "safe_error_code": type(failure).__name__,
                    },
                    exc_info=(type(failure), failure, failure.__traceback__),
                )
        return outcome, failure

    @staticmethod
    def _postgresql_finish_outcome(outcome: str, failure: Exception | None) -> str:
        if failure is None:
            return outcome
        if isinstance(failure, FinalizerCommitUnknown):
            return f"finalizer_commit_unknown:{failure.recovery_mode}"
        if isinstance(failure, FinalizerRestartRequired):
            return "failed:pre_effect_finalizer_restart_required"
        if isinstance(failure, DomainError):
            domain_reason = (
                "binding_stale"
                if failure.code
                in {
                    ErrorCode.APPROVAL_BINDING_INVALID,
                    ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
                    ErrorCode.APPROVAL_STALE,
                }
                else "logical_degradation"
            )
            return f"domain_terminal:{domain_reason}:{failure.code.value}"
        return f"failed:{type(failure).__name__}"

    async def _finish_postgresql_delivery(
        self,
        session: AsyncSession,
        lease: JobLease,
        outcome: str,
        failure: Exception | None,
    ) -> tuple[str, str | None]:
        finish_outcome = self._postgresql_finish_outcome(outcome, failure)
        finished = await session.scalar(
            text("SELECT supportguard_worker_finish_job(:job_id,:owner,:fencing_token,:outcome)"),
            {
                "job_id": lease.job_id,
                "owner": lease.owner,
                "fencing_token": lease.fencing_token,
                "outcome": finish_outcome,
            },
        )
        finished = _validate_worker_finish_result(finished, requested_outcome=finish_outcome)
        wakeup_ticket_id = (
            str(finished["ticket_id"]) if finished.get("ticket_id") is not None else None
        )
        return str(finished.get("outcome", finish_outcome)), wakeup_ticket_id

    async def _finish_sqlite_delivery(
        self,
        session: AsyncSession,
        admission: _DeliveryAdmission,
        outcome: str,
        failure: Exception | None,
    ) -> tuple[str, str | None]:
        lease = admission.lease
        if lease is None:
            raise RuntimeError("delivery admission omitted a lease")
        await set_local_scope(
            session,
            tenant_id=lease.tenant_id,
            principal_id=lease.owner,
            principal_role="system_worker",
        )
        current_job = await session.get(RuntimeJob, lease.job_id)
        if failure is not None and current_job is not None and current_job.status == "leased":
            repository = RuntimeJobRepository(session)
            if isinstance(failure, DomainError):
                outcome = await repository.terminal_fail(
                    lease, error_code=f"domain:{failure.code.value}"
                )
            else:
                outcome = await repository.fail(lease, error_code=type(failure).__name__)
        elif (
            outcome != "interrupted" and current_job is not None and current_job.status == "leased"
        ):
            await RuntimeJobRepository(session).complete(lease, outcome=outcome)
        persisted_delivery = await session.scalar(
            select(InboxDelivery).where(
                InboxDelivery.consumer_group == self.group,
                InboxDelivery.delivery_id == admission.message.delivery_id,
            )
        )
        if persisted_delivery is None:
            raise RuntimeError("inbox delivery disappeared")
        persisted_delivery.status = "acked"
        persisted_delivery.outcome = outcome
        persisted_run = await session.get(AgentRun, admission.message.run_id)
        return outcome, persisted_run.ticket_id if persisted_run is not None else None

    async def _finish_delivery(
        self,
        admission: _DeliveryAdmission,
        outcome: str,
        failure: Exception | None,
    ) -> tuple[str, str | None]:
        lease = admission.lease
        if lease is None:
            raise RuntimeError("delivery admission omitted a lease")
        async with self.factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                return await self._finish_postgresql_delivery(session, lease, outcome, failure)
            return await self._finish_sqlite_delivery(session, admission, outcome, failure)

    async def _publish_ticket_wakeup(
        self,
        message: RuntimeJobMessage,
        wakeup_ticket_id: str | None,
    ) -> None:
        publisher = getattr(self.redis, "publish", None)
        if publisher is None or wakeup_ticket_id is None:
            return
        try:
            published = publisher(
                ticket_event_channel(message.tenant_id, wakeup_ticket_id),
                message.run_id,
            )
            if inspect.isawaitable(published):
                await published
        except Exception as exc:
            logger.warning(
                "ticket_event_wakeup_failed",
                extra={"safe_error_code": type(exc).__name__},
            )

    async def _consume_once(
        self,
        *,
        block_ms: int,
        _after_accept: Callable[[JobLease], Awaitable[None]] | None = None,
    ) -> int:
        entry = await self._next_delivery_entry(block_ms=block_ms)
        if entry is None:
            return 0
        envelope = await self._decode_delivery(*entry)
        if envelope is None:
            return 1
        admission = await self._accept_delivery(envelope)
        if admission.should_ack_terminal:
            await self.redis.xack(self.stream, self.group, envelope.redis_id)
            return 1
        if admission.lease is None:
            return 1
        if _after_accept is not None:
            await _after_accept(admission.lease)
        outcome, failure = await self._run_delivery_handler(admission)
        outcome, wakeup_ticket_id = await self._finish_delivery(admission, outcome, failure)
        await self._publish_ticket_wakeup(admission.message, wakeup_ticket_id)
        await self.redis.xack(self.stream, self.group, envelope.redis_id)
        JOB_OUTCOMES.labels(outcome).inc()
        return 1

    async def _heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(
                self.timing.heartbeat_interval.total_seconds() if self.timing else 10
            )
            async with self.factory() as session, session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    heartbeat = await session.scalar(
                        text(
                            "SELECT supportguard_worker_heartbeat_job("
                            ":job_id,:owner,:fencing_token)"
                        ),
                        {
                            "job_id": lease.job_id,
                            "owner": lease.owner,
                            "fencing_token": lease.fencing_token,
                        },
                    )
                    if not isinstance(heartbeat, dict):
                        raise RuntimeError("worker heartbeat capability returned an invalid result")
                else:
                    await set_local_scope(
                        session,
                        tenant_id=lease.tenant_id,
                        principal_id=lease.owner,
                        principal_role="system_worker",
                    )
                    await RuntimeJobRepository(session).heartbeat(
                        lease,
                        lease_seconds=int(
                            self.timing.job_lease.total_seconds() if self.timing else 30
                        ),
                    )
            # This assignment occurs only after the heartbeat transaction has
            # committed. A long-running Provider/MCP segment is therefore live
            # progress, while a failed heartbeat still cancels the handler.
            self.last_lease_heartbeat_at = datetime.now(UTC)


async def bounded_service_loop(
    operation: Any,
    *,
    interval_seconds: float,
    operation_timeout_seconds: float | None = None,
    progress: ServiceLoopProgress | None = None,
) -> None:
    """Run one control loop without allowing a silent, indefinitely hung iteration.

    Worker handler execution is deliberately not assigned a loop-wide timeout:
    its Provider, MCP, lease and attempt bounds own that lifecycle. Dispatcher
    and Reconciler callers use this timeout so an unresponsive cross-store
    operation exits the process and lets the container restart from durable
    PostgreSQL/Redis state.
    """

    if operation_timeout_seconds is not None and operation_timeout_seconds <= 0:
        raise ValueError("service loop timeout must be positive")
    while True:
        if progress is not None:
            progress.started()
        try:
            if operation_timeout_seconds is None:
                await operation()
            else:
                async with asyncio.timeout(operation_timeout_seconds):
                    await operation()
        except TimeoutError as exc:
            service = progress.service if progress is not None else "runtime"
            logger.error(
                "runtime_service_operation_timeout",
                extra={
                    "event": "runtime_service_operation_timeout",
                    "service": service,
                    "timeout_seconds": operation_timeout_seconds,
                },
            )
            raise RuntimeError(f"{service}_operation_timeout") from exc
        else:
            if progress is not None:
                progress.completed()
        await asyncio.sleep(interval_seconds)
