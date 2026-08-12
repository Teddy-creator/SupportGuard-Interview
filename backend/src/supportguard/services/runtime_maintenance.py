from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import and_, exc, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard.contracts.queue import RuntimeJobMessage
from supportguard.db.models import (
    AgentRun,
    InboxDelivery,
    OutboxEvent,
    QueueDeliveryAudit,
    RuntimeJob,
    new_id,
)
from supportguard.observability.metrics import RECONCILE
from supportguard.services.action_effect_reconciliation import ActionEffectReconciliationRunner
from supportguard.services.runtime_jobs import (
    converge_dead_aggregates,
    transition_runtime_job_status,
)
from supportguard.services.runtime_timing import RuntimeTiming
from supportguard.services.writer_barrier import cross_store_writer_barrier

logger = logging.getLogger(__name__)
MAX_DELIVERY_GENERATION = 5

RECONCILE_OBSERVATION_SCRIPT = """
local stream = KEYS[1]
local message_id = ARGV[1]
local observation_nonce = ARGV[2]
if not string.match(message_id, '^%d+%-%d+$') then
  return cjson.encode({status='known', delivery_present=false, pending_groups={},
    registered_groups={}, observation_nonce=observation_nonce})
end
local row = redis.pcall('XRANGE', stream, message_id, message_id, 'COUNT', 1)
if type(row) == 'table' and row.err then
  return cjson.encode({status='unknown', error_code='xrange_failed'})
end
local groups = redis.pcall('XINFO', 'GROUPS', stream)
if type(groups) == 'table' and groups.err then
  if string.find(groups.err, 'no such key') then
    groups = {}
  else
    return cjson.encode({status='unknown', error_code='xinfo_failed'})
  end
end
local pending_groups = {}
local registered_groups = {}
for _, group in ipairs(groups) do
  local name = nil
  for index=1,#group,2 do
    if group[index] == 'name' then name = group[index+1] end
  end
  if name then
    table.insert(registered_groups, name)
    local pending = redis.pcall('XPENDING', stream, name, message_id, message_id, 1)
    if type(pending) == 'table' and pending.err then
      return cjson.encode({status='unknown', error_code='xpending_failed'})
    end
    if #pending > 0 then table.insert(pending_groups, name) end
  end
end
local payload = cjson.null
if #row > 0 then
  for index=1,#row[1][2],2 do
    if row[1][2][index] == 'payload' then payload = row[1][2][index+1] end
  end
end
return cjson.encode({status='known', delivery_present=(#row > 0),
  pending_groups=pending_groups, registered_groups=registered_groups,
  observation_nonce=observation_nonce, stream_payload=payload})
"""

RETENTION_TRIM_SCRIPT = """
local stream = KEYS[1]
local tombstone = KEYS[2]
local intent_id = ARGV[1]
local message_id = ARGV[2]
local payload = ARGV[3]
local payload_hash = ARGV[4]
local group_set_hash = ARGV[5]
local expected_group = ARGV[6]
local existing = redis.call('HGETALL', tombstone)
if #existing > 0 then
  local values = {}
  for index=1,#existing,2 do values[existing[index]] = existing[index+1] end
  if values['intent_id'] == intent_id and values['message_id'] == message_id
     and values['payload_hash'] == payload_hash
     and values['group_set_hash'] == group_set_hash
     and values['state'] == 'redis_trimmed' then
    return cjson.encode({status='redis_trimmed', reused=true})
  end
  return cjson.encode({status='denied', reason='tombstone_conflict'})
end
local groups = redis.pcall('XINFO', 'GROUPS', stream)
if type(groups) == 'table' and groups.err then
  return cjson.encode({status='denied', reason='group_registry_unknown'})
end
if #groups ~= 1 then
  return cjson.encode({status='denied', reason='group_registry_mismatch'})
end
local group_name = nil
for index=1,#groups[1],2 do
  if groups[1][index] == 'name' then group_name = groups[1][index+1] end
end
if group_name ~= expected_group then
  return cjson.encode({status='denied', reason='group_registry_mismatch'})
end
local pending = redis.pcall('XPENDING', stream, expected_group, message_id, message_id, 1)
if type(pending) == 'table' and pending.err then
  return cjson.encode({status='denied', reason='pel_unknown'})
end
if #pending > 0 then
  return cjson.encode({status='denied', reason='pel_active'})
end
local rows = redis.call('XRANGE', stream, message_id, message_id, 'COUNT', 1)
if #rows ~= 1 then
  return cjson.encode({status='denied', reason='message_absent_before_xdel'})
end
local stored_payload = nil
for index=1,#rows[1][2],2 do
  if rows[1][2][index] == 'payload' then stored_payload = rows[1][2][index+1] end
end
if stored_payload ~= payload then
  return cjson.encode({status='denied', reason='payload_mismatch'})
end
local removed = redis.call('XDEL', stream, message_id)
if removed ~= 1 then
  return cjson.encode({status='denied', reason='xdel_not_applied'})
end
redis.call('HSET', tombstone,
  'state', 'redis_trimmed', 'intent_id', intent_id, 'stream', stream,
  'message_id', message_id, 'payload_hash', payload_hash,
  'group_set_hash', group_set_hash)
return cjson.encode({status='redis_trimmed', reused=false})
"""

RETENTION_FINALIZE_TOMBSTONE_SCRIPT = """
local tombstone = KEYS[1]
local intent_id = ARGV[1]
local receipt_hash = ARGV[2]
local values = redis.call('HGETALL', tombstone)
if #values == 0 then return 0 end
local parsed = {}
for index=1,#values,2 do parsed[values[index]] = values[index+1] end
if parsed['intent_id'] ~= intent_id then return 0 end
if parsed['state'] == 'pg_finalized' then
  if parsed['pg_receipt_hash'] ~= receipt_hash then return 0 end
  redis.call('PEXPIRE', tombstone, 604800000)
  return 1
end
if parsed['state'] ~= 'redis_trimmed' then return 0 end
redis.call('HSET', tombstone, 'state', 'pg_finalized',
  'pg_receipt_hash', receipt_hash)
redis.call('PEXPIRE', tombstone, 604800000)
return 1
"""

RETENTION_GROUP = "supportguard-workers-v1"
RETENTION_GROUP_SET_HASH = "3a0fde6ba7d90c905877897d4ddad78298e4e7b587248f8520de9393cb60148d"


@dataclass(frozen=True, slots=True)
class RedisTrimReport:
    stream: str
    schema_version: str
    scanned: int
    eligible: int
    deleted: int
    pending_ids: int
    skipped: dict[str, int] = field(default_factory=dict)


async def _pending_message_ids(redis: Redis, *, stream: str) -> set[str]:
    pending: set[str] = set()
    try:
        groups = await redis.xinfo_groups(stream)
    except ResponseError as exc:
        if "no such key" in str(exc).lower():
            return pending
        raise
    group_names = {
        (
            raw.decode()
            if isinstance((raw := group.get(b"name", group.get("name"))), bytes)
            else str(raw)
        )
        for group in groups
    }
    if group_names != {RETENTION_GROUP}:
        raise RuntimeError("retention_consumer_group_registry_mismatch")
    for group in groups:
        raw_name = group.get(b"name", group.get("name"))
        name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
        summary = await redis.xpending(stream, name)
        total = int(summary.get("pending", 0)) if isinstance(summary, dict) else 0
        if total == 0:
            continue
        rows = await redis.xpending_range(stream, name, min="-", max="+", count=total)
        for row in rows:
            raw_id = row.get("message_id", row.get(b"message_id"))
            pending.add(raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id))
    return pending


async def trim_terminal_deliveries(
    session: AsyncSession,
    redis: Redis,
    *,
    stream: str,
    timing: RuntimeTiming,
    apply: bool,
    now: datetime | None = None,
    batch_size: int = 1_000,
    audit_factory: async_sessionmaker[AsyncSession] | None = None,
    _barrier_held: bool = False,
    _after_authorize: Callable[[str], Awaitable[None]] | None = None,
    _after_redis_trim: Callable[[str], Awaitable[None]] | None = None,
    _after_pg_finalize: Callable[[str], Awaitable[None]] | None = None,
) -> RedisTrimReport:
    """Delete only aged, terminal PostgreSQL deliveries absent from every Redis PEL."""
    is_postgresql = session.get_bind().dialect.name == "postgresql"
    if is_postgresql and apply and not _barrier_held:
        if audit_factory is None:
            raise RuntimeError("postgres_trim_requires_independent_audit_factory")
        async with cross_store_writer_barrier(
            audit_factory,
            operation="maintenance",
        ):
            return await trim_terminal_deliveries(
                session,
                redis,
                stream=stream,
                timing=timing,
                apply=apply,
                now=now,
                batch_size=batch_size,
                audit_factory=audit_factory,
                _barrier_held=True,
                _after_authorize=_after_authorize,
                _after_redis_trim=_after_redis_trim,
                _after_pg_finalize=_after_pg_finalize,
            )
    effective_now = now or datetime.now(UTC)
    recovered = 0
    if is_postgresql and apply:
        if audit_factory is None:
            raise RuntimeError("postgres_trim_requires_independent_audit_factory")
        async with audit_factory() as recovery_session, recovery_session.begin():
            pending_ttl_report = await recovery_session.scalar(
                text("SELECT supportguard_maintenance_retention_report('pending_ttl')")
            )
        if not isinstance(pending_ttl_report, dict) or not isinstance(
            pending_ttl_report.get("pending_ttl"), list
        ):
            raise RuntimeError("maintenance_trim_pending_ttl_report_invalid")
        for raw_intent in pending_ttl_report["pending_ttl"]:
            if not isinstance(raw_intent, dict) or raw_intent.get("stream") != stream:
                continue
            recovered_intent_id = str(raw_intent["intent_id"])
            redis_message_id = str(raw_intent["redis_message_id"])
            tombstone_key = (
                "supportguard:retention:v1:"
                + hashlib.sha256(f"{stream}|{redis_message_id}".encode()).hexdigest()
            )
            tombstone_finalized = await cast(
                Awaitable[Any],
                redis.eval(
                    RETENTION_FINALIZE_TOMBSTONE_SCRIPT,
                    1,
                    tombstone_key,
                    recovered_intent_id,
                    str(raw_intent["receipt_hash"]),
                ),
            )
            if int(tombstone_finalized) != 1:
                # A missing or conflicting Redis receipt cannot be inferred safe.
                # Leave the durable PostgreSQL record pending for operator recovery.
                continue
            async with audit_factory() as recovery_session, recovery_session.begin():
                confirmed = await recovery_session.scalar(
                    text(
                        "SELECT supportguard_maintenance_finalize_trim("
                        ":intent_id,:message_id,:payload_hash,:group_hash,:receipt_hash)"
                    ),
                    {
                        "intent_id": recovered_intent_id,
                        "message_id": redis_message_id,
                        "payload_hash": str(raw_intent["payload_hash"]),
                        "group_hash": str(raw_intent["group_set_hash"]),
                        "receipt_hash": str(raw_intent["redis_receipt_hash"]),
                    },
                )
            if not isinstance(confirmed, dict) or not bool(confirmed.get("ttl_confirmed")):
                raise RuntimeError("maintenance_trim_ttl_confirmation_failed")
        async with audit_factory() as recovery_session, recovery_session.begin():
            active_report = await recovery_session.scalar(
                text("SELECT supportguard_maintenance_retention_report('active')")
            )
        if not isinstance(active_report, dict) or not isinstance(active_report.get("active"), list):
            raise RuntimeError("maintenance_trim_active_report_invalid")
        for raw_intent in active_report["active"]:
            if not isinstance(raw_intent, dict) or raw_intent.get("stream") != stream:
                continue
            recovered_intent_id = str(raw_intent["intent_id"])
            redis_message_id = str(raw_intent["redis_message_id"])
            tombstone_key = (
                "supportguard:retention:v1:"
                + hashlib.sha256(f"{stream}|{redis_message_id}".encode()).hexdigest()
            )
            raw_tombstone = await cast(Awaitable[Any], redis.hgetall(tombstone_key))
            tombstone = {
                (key.decode() if isinstance(key, bytes) else str(key)): (
                    value.decode() if isinstance(value, bytes) else str(value)
                )
                for key, value in raw_tombstone.items()
            }
            if (
                tombstone.get("state") != "redis_trimmed"
                or tombstone.get("intent_id") != recovered_intent_id
                or tombstone.get("message_id") != redis_message_id
                or tombstone.get("payload_hash") != raw_intent.get("payload_hash")
                or tombstone.get("group_set_hash") != raw_intent.get("group_set_hash")
            ):
                unknown_hash = hashlib.sha256(
                    "|".join(
                        (
                            "unknown-xdel.v1",
                            recovered_intent_id,
                            redis_message_id,
                            str(raw_intent["payload_hash"]),
                            str(raw_intent["group_set_hash"]),
                        )
                    ).encode()
                ).hexdigest()
                async with audit_factory() as recovery_session, recovery_session.begin():
                    unknown = await recovery_session.scalar(
                        text("SELECT supportguard_maintenance_abort_trim(:id,:receipt)"),
                        {"id": recovered_intent_id, "receipt": unknown_hash},
                    )
                if not isinstance(unknown, dict) or unknown.get("status") != "unknown_trim_state":
                    raise RuntimeError("maintenance_trim_unknown_state_failed")
                continue
            redis_receipt_hash = hashlib.sha256(
                json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            async with audit_factory() as recovery_session, recovery_session.begin():
                finalized = await recovery_session.scalar(
                    text(
                        "SELECT supportguard_maintenance_finalize_trim("
                        ":intent_id,:message_id,:payload_hash,:group_hash,:receipt_hash)"
                    ),
                    {
                        "intent_id": recovered_intent_id,
                        "message_id": redis_message_id,
                        "payload_hash": str(raw_intent["payload_hash"]),
                        "group_hash": str(raw_intent["group_set_hash"]),
                        "receipt_hash": redis_receipt_hash,
                    },
                )
            if not isinstance(finalized, dict) or finalized.get("status") != "finalized":
                raise RuntimeError("maintenance_trim_recovery_finalize_failed")
            tombstone_finalized = await cast(
                Awaitable[Any],
                redis.eval(
                    RETENTION_FINALIZE_TOMBSTONE_SCRIPT,
                    1,
                    tombstone_key,
                    recovered_intent_id,
                    str(finalized["receipt_hash"]),
                ),
            )
            if int(tombstone_finalized) != 1:
                raise RuntimeError("maintenance_trim_recovery_tombstone_failed")
            async with audit_factory() as recovery_session, recovery_session.begin():
                confirmed = await recovery_session.scalar(
                    text(
                        "SELECT supportguard_maintenance_finalize_trim("
                        ":intent_id,:message_id,:payload_hash,:group_hash,:receipt_hash)"
                    ),
                    {
                        "intent_id": recovered_intent_id,
                        "message_id": redis_message_id,
                        "payload_hash": str(raw_intent["payload_hash"]),
                        "group_hash": str(raw_intent["group_set_hash"]),
                        "receipt_hash": redis_receipt_hash,
                    },
                )
            if not isinstance(confirmed, dict) or not bool(confirmed.get("ttl_confirmed")):
                raise RuntimeError("maintenance_trim_recovery_ttl_confirmation_failed")
            recovered += 1
    pending = await _pending_message_ids(redis, stream=stream)
    rows = await redis.xrange(stream, min="-", max="+", count=batch_size)
    skipped: dict[str, int] = {}
    eligible: list[tuple[str, RuntimeJobMessage, str, str]] = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for raw_id, fields in rows:
        message_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        if message_id in pending:
            skip("pel_active")
            continue
        try:
            published_at = datetime.fromtimestamp(int(message_id.split("-", 1)[0]) / 1000, UTC)
            message = RuntimeJobMessage.from_redis(fields)
        except (ValueError, TypeError):
            skip("invalid_delivery")
            continue
        if effective_now - published_at < timing.operational_horizon:
            skip("retention_not_elapsed")
            continue
        raw_payload = fields.get(b"payload", fields.get("payload"))
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode()
        if not isinstance(raw_payload, str):
            skip("invalid_delivery")
            continue
        payload_hash = hashlib.sha256(raw_payload.encode()).hexdigest()
        if is_postgresql:
            eligibility = await session.scalar(
                text(
                    "SELECT supportguard_maintenance_trim_eligibility(:job_id,:run_id,:tenant_id)"
                ),
                {
                    "job_id": message.job_id,
                    "run_id": message.run_id,
                    "tenant_id": message.tenant_id,
                },
            )
            if not isinstance(eligibility, dict):
                raise RuntimeError("maintenance_trim_eligibility_invalid")
            if not bool(eligibility.get("eligible")):
                skip(str(eligibility.get("reason", "postgres_eligibility_denied")))
                continue
        else:
            job = await session.get(RuntimeJob, message.job_id)
            if job is None:
                skip("postgres_job_missing")
                continue
            if job.status not in {"succeeded", "dead"}:
                skip("postgres_job_active")
                continue
            if job.tenant_id != message.tenant_id or job.run_id != message.run_id:
                skip("envelope_mismatch")
                continue
        eligible.append((message_id, message, payload_hash, raw_payload))

    deleted = recovered
    if apply:
        for message_id, message, payload_hash, raw_payload in eligible:
            intent_id: str | None = None
            if is_postgresql:
                if audit_factory is None:
                    raise RuntimeError("postgres_trim_requires_independent_audit_factory")
                async with audit_factory() as audit_session, audit_session.begin():
                    intent_id = new_id("trim")
                    authorization = await audit_session.scalar(
                        text(
                            "SELECT supportguard_maintenance_authorize_trim("
                            ":intent_id,:stream,:redis_message_id,:payload_hash)"
                        ),
                        {
                            "intent_id": intent_id,
                            "stream": stream,
                            "redis_message_id": message_id,
                            "payload_hash": payload_hash,
                        },
                    )
                if not isinstance(authorization, dict):
                    raise RuntimeError("maintenance_trim_authorization_invalid")
                intent_id = str(authorization["intent_id"])
                if _after_authorize is not None:
                    await _after_authorize(intent_id)
            tombstone_key = (
                "supportguard:retention:v1:"
                + hashlib.sha256(f"{stream}|{message_id}".encode()).hexdigest()
            )
            raw_result = await cast(
                Awaitable[Any],
                redis.eval(
                    RETENTION_TRIM_SCRIPT,
                    2,
                    stream,
                    tombstone_key,
                    intent_id or "sqlite",
                    message_id,
                    raw_payload,
                    payload_hash,
                    RETENTION_GROUP_SET_HASH,
                    RETENTION_GROUP,
                ),
            )
            if isinstance(raw_result, bytes):
                raw_result = raw_result.decode()
            trim_result = json.loads(str(raw_result))
            if trim_result.get("status") != "redis_trimmed":
                reason = str(trim_result.get("reason", "redis_trim_denied"))
                skip(reason)
                if is_postgresql and intent_id is not None:
                    if audit_factory is None:
                        raise RuntimeError("maintenance_trim_audit_invariant")
                    negative_hash = hashlib.sha256(
                        "|".join(
                            (
                                "negative-xdel.v1",
                                intent_id,
                                message_id,
                                payload_hash,
                                RETENTION_GROUP_SET_HASH,
                            )
                        ).encode()
                    ).hexdigest()
                    async with audit_factory() as audit_session, audit_session.begin():
                        await audit_session.scalar(
                            text("SELECT supportguard_maintenance_abort_trim(:id,:receipt)"),
                            {"id": intent_id, "receipt": negative_hash},
                        )
                continue
            if _after_redis_trim is not None and intent_id is not None:
                await _after_redis_trim(intent_id)
            if is_postgresql:
                if audit_factory is None or intent_id is None:
                    raise RuntimeError("maintenance_trim_audit_invariant")
                redis_receipt_hash = hashlib.sha256(
                    json.dumps(trim_result, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                async with audit_factory() as audit_session, audit_session.begin():
                    finalized = await audit_session.scalar(
                        text(
                            "SELECT supportguard_maintenance_finalize_trim("
                            ":intent_id,:message_id,:payload_hash,:group_hash,:receipt_hash)"
                        ),
                        {
                            "intent_id": intent_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "group_hash": RETENTION_GROUP_SET_HASH,
                            "receipt_hash": redis_receipt_hash,
                        },
                    )
                if not isinstance(finalized, dict) or finalized.get("status") != "finalized":
                    raise RuntimeError("maintenance_trim_audit_finalize_failed")
                if _after_pg_finalize is not None:
                    await _after_pg_finalize(intent_id)
                tombstone_finalized = await cast(
                    Awaitable[Any],
                    redis.eval(
                        RETENTION_FINALIZE_TOMBSTONE_SCRIPT,
                        1,
                        tombstone_key,
                        intent_id,
                        str(finalized["receipt_hash"]),
                    ),
                )
                if int(tombstone_finalized) != 1:
                    raise RuntimeError("maintenance_trim_tombstone_finalize_failed")
                async with audit_factory() as audit_session, audit_session.begin():
                    confirmed = await audit_session.scalar(
                        text(
                            "SELECT supportguard_maintenance_finalize_trim("
                            ":intent_id,:message_id,:payload_hash,:group_hash,:receipt_hash)"
                        ),
                        {
                            "intent_id": intent_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "group_hash": RETENTION_GROUP_SET_HASH,
                            "receipt_hash": redis_receipt_hash,
                        },
                    )
                if not isinstance(confirmed, dict) or not bool(confirmed.get("ttl_confirmed")):
                    raise RuntimeError("maintenance_trim_ttl_confirmation_failed")
            deleted += 1
            if not is_postgresql:
                session.add(
                    QueueDeliveryAudit(
                        tenant_id=message.tenant_id,
                        job_id=message.job_id,
                        delivery_id=message.delivery_id,
                        redis_message_id=message_id,
                        consumer_group="maintenance-trim",
                        outcome="maintenance_trimmed_terminal",
                        payload_hash=payload_hash,
                        details={
                            "stream": stream,
                            "threshold_schema_version": timing.schema_version,
                            "retention_seconds": int(timing.operational_horizon.total_seconds()),
                        },
                    )
                )
    return RedisTrimReport(
        stream=stream,
        schema_version=timing.schema_version,
        scanned=len(rows),
        eligible=len(eligible) + recovered,
        deleted=deleted,
        pending_ids=len(pending),
        skipped=skipped,
    )


class RuntimeReconciler:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        redis: Redis | None = None,
        *,
        stream: str | None = None,
        timing: RuntimeTiming | None = None,
    ) -> None:
        self.factory = factory
        self.redis = redis
        self.stream = stream
        self.timing = timing

    async def _latest_delivery_still_exists(self, redis_message_id: str | None) -> bool:
        if self.redis is None or self.stream is None or redis_message_id is None:
            return False
        try:
            rows = await self.redis.xrange(
                self.stream,
                min=redis_message_id,
                max=redis_message_id,
                count=1,
            )
        except Exception:
            # Redis uncertainty must never create a second delivery generation.
            return True
        return bool(rows)

    async def _delivery_is_recoverable(
        self,
        session: AsyncSession,
        *,
        event: OutboxEvent,
        job_status: str,
    ) -> bool:
        """A stream row alone is not a recoverable delivery after XACK."""
        inbox = await session.scalar(
            select(InboxDelivery)
            .where(InboxDelivery.delivery_id == event.delivery_id)
            .order_by(InboxDelivery.created_at.desc())
            .limit(1)
        )
        if inbox is not None and inbox.status in {"acked", "rejected"}:
            return False
        if job_status == "retry_wait":
            # A due retry_wait row is created only after the handler outcome and
            # current delivery terminal are committed.  Redis retains XACKed
            # stream entries, so XRANGE cannot be used as a liveness signal.
            return False
        return await self._latest_delivery_still_exists(event.redis_message_id)

    async def _supersede_delivery(
        self,
        session: AsyncSession,
        *,
        event: OutboxEvent,
        next_generation: int,
        next_outbox_id: str,
        superseded_at: datetime,
    ) -> None:
        event.superseded_at = superseded_at
        event.superseded_by_delivery_id = next_outbox_id
        event.delivery_state_version += 1
        inboxes = (
            await session.scalars(
                select(InboxDelivery).where(InboxDelivery.delivery_id == event.delivery_id)
            )
        ).all()
        audit_sources: list[InboxDelivery | None] = list(inboxes) or [None]
        for inbox in audit_sources:
            previous_status = inbox.status if inbox is not None else "missing"
            if inbox is not None:
                inbox.status = "rejected"
                inbox.outcome = "superseded_by_redelivery"
            session.add(
                QueueDeliveryAudit(
                    tenant_id=event.tenant_id,
                    job_id=event.job_id,
                    delivery_id=event.delivery_id,
                    redis_message_id=(
                        inbox.redis_message_id
                        if inbox is not None
                        else event.redis_message_id or f"outbox:{event.id}"
                    ),
                    consumer_group=(inbox.consumer_group if inbox is not None else "reconciler"),
                    outcome="superseded_lost_or_expired",
                    payload_hash=hashlib.sha256(event.delivery_id.encode()).hexdigest(),
                    details={
                        "outbox_id": event.id,
                        "previous_status": previous_status,
                        "delivery_generation": event.delivery_generation,
                        "next_generation": next_generation,
                    },
                )
            )

    async def reconcile_once(
        self,
        *,
        redelivery_grace_seconds: int = 15,
        _after_prepare: Callable[[str], Awaitable[None]] | None = None,
    ) -> int:
        async with self.factory() as probe:
            is_postgresql = probe.get_bind().dialect.name == "postgresql"
        if is_postgresql:
            async with cross_store_writer_barrier(
                self.factory,
                operation="reconciler",
            ):
                return await self._reconcile_postgresql(
                    redelivery_grace_seconds=redelivery_grace_seconds,
                    _after_prepare=_after_prepare,
                )
        now = datetime.now(UTC)
        grace = now - timedelta(seconds=redelivery_grace_seconds)
        repaired = 0
        async with self.factory() as session, session.begin():
            jobs = (
                await session.scalars(
                    select(RuntimeJob)
                    .where(
                        or_(
                            RuntimeJob.status == "queued",
                            and_(RuntimeJob.status == "retry_wait", RuntimeJob.available_at <= now),
                            and_(RuntimeJob.status == "leased", RuntimeJob.lease_expires_at < now),
                        )
                    )
                    .order_by(
                        RuntimeJob.tenant_id,
                        RuntimeJob.ticket_id,
                        RuntimeJob.dispatch_sequence,
                        RuntimeJob.id,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job in jobs:
                head_job_id = await session.scalar(
                    select(RuntimeJob.id)
                    .where(
                        RuntimeJob.tenant_id == job.tenant_id,
                        RuntimeJob.ticket_id == job.ticket_id,
                        RuntimeJob.status.in_(("queued", "retry_wait", "leased")),
                    )
                    .order_by(RuntimeJob.dispatch_sequence, RuntimeJob.id)
                    .limit(1)
                )
                if head_job_id != job.id:
                    # A later delivery must not overtake an earlier retry,
                    # lease, or not-yet-due queue entry on the same Ticket.
                    continue
                # An expired lease must become claimable even when the original
                # Redis delivery still exists. PEL recovery reuses that delivery;
                # leaving the PostgreSQL row as `leased` makes every XAUTOCLAIM
                # lose the claim race forever and continually reset its idle age.
                if job.status == "leased":
                    transition_runtime_job_status(job, "queued")
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                latest = await session.scalar(
                    select(OutboxEvent)
                    .where(OutboxEvent.job_id == job.id)
                    .order_by(OutboxEvent.delivery_generation.desc())
                    .limit(1)
                )
                if latest is not None and latest.published_at is None:
                    # An unpublished Outbox row is already the recoverable delivery source.
                    # Creating another generation here races the Dispatcher and amplifies load.
                    continue
                if latest is not None and latest.last_delivery_at is not None:
                    if await self._delivery_is_recoverable(
                        session, event=latest, job_status=job.status
                    ):
                        continue
                    delivered_at = latest.last_delivery_at
                    if delivered_at.tzinfo is None:
                        delivered_at = delivered_at.replace(tzinfo=UTC)
                    if delivered_at > grace:
                        continue
                if job.status == "retry_wait":
                    transition_runtime_job_status(job, "queued")
                generation = (latest.delivery_generation + 1) if latest is not None else 1
                created_at = job.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                max_age = (
                    self.timing.operational_horizon
                    if self.timing is not None
                    else timedelta(minutes=10)
                )
                if generation > MAX_DELIVERY_GENERATION or now - created_at >= max_age:
                    reason = (
                        "delivery_generation_exhausted"
                        if generation > MAX_DELIVERY_GENERATION
                        else "job_max_age_exhausted"
                    )
                    transition_runtime_job_status(job, "dead")
                    job.last_error = reason
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    run = await session.get(AgentRun, job.run_id, with_for_update=True)
                    if run is not None:
                        await converge_dead_aggregates(session, job=job, run=run, reason=reason)
                    RECONCILE.labels("bounded_dead_letter").inc()
                    continue
                if latest is not None:
                    next_outbox_id = new_id("outbox")
                    await self._supersede_delivery(
                        session,
                        event=latest,
                        next_generation=generation,
                        next_outbox_id=next_outbox_id,
                        superseded_at=now,
                    )
                else:
                    next_outbox_id = new_id("outbox")
                session.add(
                    OutboxEvent(
                        id=next_outbox_id,
                        delivery_id=new_id("delivery"),
                        tenant_id=job.tenant_id,
                        job_id=job.id,
                        run_id=job.run_id,
                        delivery_generation=generation,
                        event_type="runtime_job_available",
                        payload={},
                    )
                )
                repaired += 1
                RECONCILE.labels("eligible_non_terminal").inc()
        return repaired

    async def _reconcile_postgresql(
        self,
        *,
        redelivery_grace_seconds: int,
        _after_prepare: Callable[[str], Awaitable[None]] | None = None,
    ) -> int:
        del redelivery_grace_seconds
        async with self.factory() as session, session.begin():
            candidates = (
                (
                    await session.execute(
                        text("SELECT * FROM supportguard_reconciler_candidates(:batch_size)"),
                        {"batch_size": 500},
                    )
                )
                .mappings()
                .all()
            )
        action_effect_report = await ActionEffectReconciliationRunner(
            self.factory
        ).reconcile_candidates(candidates)
        action_effect_job_ids = set(action_effect_report.handled_job_ids)
        if action_effect_report.resolved_executed:
            RECONCILE.labels("action_effect_executed").inc(action_effect_report.resolved_executed)
        if action_effect_report.resolved_zero_effect:
            RECONCILE.labels("action_effect_zero_effect").inc(
                action_effect_report.resolved_zero_effect
            )
        if action_effect_report.pending:
            RECONCILE.labels("action_effect_pending").inc(action_effect_report.pending)
        repaired = 0
        for candidate in candidates:
            if str(candidate["job_id"]) in action_effect_job_ids:
                # Unknown Action effects are durable PostgreSQL intents. They must
                # never be inferred from or repaired through Redis delivery state.
                continue
            prepared: dict[str, Any] | None = None
            observation: dict[str, Any] | None = None
            result: Any = None
            for retry_index in range(3):
                try:
                    if prepared is None:
                        async with self.factory() as session, session.begin():
                            raw_prepared = await session.scalar(
                                text(
                                    "SELECT supportguard_reconciler_prepare("
                                    ":job_id,:expected_job_version,:reason)"
                                ),
                                {
                                    "job_id": candidate["job_id"],
                                    "expected_job_version": candidate["status_version"],
                                    "reason": "delivery_recovery",
                                },
                            )
                        if (
                            not isinstance(raw_prepared, dict)
                            or raw_prepared.get("result") != "prepared"
                        ):
                            break
                        prepared = raw_prepared
                        if _after_prepare is not None:
                            await _after_prepare(str(prepared["intent_id"]))
                    if observation is None:
                        observation = await self._observe_reconcile_intent(prepared)
                    async with self.factory() as session, session.begin():
                        result = await session.scalar(
                            text(
                                "SELECT supportguard_reconciler_repair("
                                ":job_id,:expected_job_version,:intent_id,"
                                "CAST(:observation AS jsonb))"
                            ),
                            {
                                "job_id": candidate["job_id"],
                                "expected_job_version": candidate["status_version"],
                                "intent_id": prepared["intent_id"],
                                "observation": json.dumps(
                                    observation,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                ),
                            },
                        )
                    break
                except exc.DBAPIError as error:
                    sqlstate = getattr(error.orig, "sqlstate", None)
                    if sqlstate not in {"40001", "40P01"} or retry_index == 2:
                        raise
            if result == "repaired":
                repaired += 1
                RECONCILE.labels("eligible_non_terminal").inc()
            elif result == "dead":
                RECONCILE.labels("bounded_dead_letter").inc()
            elif result == "manual_takeover":
                raise RuntimeError("reconciler_created_manual_takeover")
        return repaired + action_effect_report.resolved

    async def _observe_reconcile_intent(self, prepared: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": "redis-delivery-observation.v1",
            "intent_id": str(prepared["intent_id"]),
            "observation_nonce": str(prepared["observation_nonce"]),
            "job_id": str(prepared["job_id"]),
            "outbox_id": str(prepared["outbox_id"]),
            "delivery_generation": int(prepared["delivery_generation"]),
            "runner_nonce": uuid4().hex,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if self.redis is None or self.stream is None:
            return {**base, "status": "unknown", "error_code": "redis_not_configured"}
        try:
            async with self.redis.client() as client:
                client_id_before = int(await client.client_id())
                principal_before = await client.acl_whoami()
                connection_generation = f"redis-client:{client_id_before}"
                pool = client.connection_pool.connection_kwargs
                endpoint_fingerprint = hashlib.sha256(
                    "|".join(
                        (
                            str(pool.get("host", "")),
                            str(pool.get("port", "")),
                            str(pool.get("db", "")),
                            bool(pool.get("ssl", False)).__str__(),
                        )
                    ).encode()
                ).hexdigest()
                raw = await cast(
                    Awaitable[Any],
                    client.eval(
                        RECONCILE_OBSERVATION_SCRIPT,
                        1,
                        self.stream,
                        str(prepared.get("stream_message_id") or "0-0"),
                        str(prepared["observation_nonce"]),
                    ),
                )
                principal_after = await client.acl_whoami()
                client_id_after = int(await client.client_id())
            if isinstance(raw, bytes):
                raw = raw.decode()
            result = json.loads(str(raw))
            if not isinstance(result, dict):
                raise ValueError("redis observation is not an object")
            if result.get("pending_groups") == {}:
                # Redis Lua cjson encodes an empty Lua table as an object. The
                # transport contract is unambiguously an array, including empty.
                result["pending_groups"] = []
            if not isinstance(result.get("pending_groups"), list):
                raise ValueError("redis pending group observation is not an array")
            if result.get("registered_groups") == {}:
                result["registered_groups"] = []
            if not isinstance(result.get("registered_groups"), list):
                raise ValueError("redis registered group observation is not an array")
            stream_payload = result.pop("stream_payload", None)
            result["pending_groups"] = sorted(str(item) for item in result["pending_groups"])
            result["registered_groups"] = sorted(str(item) for item in result["registered_groups"])
            result["stream_payload_hash"] = (
                hashlib.sha256(stream_payload.encode()).hexdigest()
                if isinstance(stream_payload, str)
                else None
            )
            if result.pop("observation_nonce", None) != base["observation_nonce"]:
                raise ValueError("redis observation nonce mismatch")
            return {
                **base,
                **result,
                "redis_client_id_before": client_id_before,
                "redis_client_id_after": client_id_after,
                "redis_principal_before": (
                    principal_before.decode()
                    if isinstance(principal_before, bytes)
                    else str(principal_before)
                ),
                "redis_principal_after": (
                    principal_after.decode()
                    if isinstance(principal_after, bytes)
                    else str(principal_after)
                ),
                "connection_generation_before": connection_generation,
                "connection_generation_after": f"redis-client:{client_id_after}",
                "endpoint_fingerprint_before": endpoint_fingerprint,
                "endpoint_fingerprint_after": endpoint_fingerprint,
                "lua_observation_nonce": base["observation_nonce"],
            }
        except Exception as exc:
            return {
                **base,
                "status": "unknown",
                "error_code": f"redis_{type(exc).__name__}",
            }
