from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import Session

Operation = Literal["dispatcher", "worker", "reconciler", "maintenance"]

_BARRIER_KEY_SQL = "hashtextextended('supportguard.writer-barrier.v1',0)"
_LOCK_COUNT_SQL = """
WITH barrier AS (
  SELECT hashtextextended('supportguard.writer-barrier.v1',0) AS key
)
SELECT count(*)
FROM pg_locks, barrier
WHERE locktype='advisory'
  AND database=(
    SELECT oid FROM pg_database WHERE datname=current_database()
  )
  AND classid=((barrier.key >> 32) & 4294967295)::oid
  AND objid=(barrier.key & 4294967295)::oid
  AND objsubid=1
  AND mode='ShareLock'
  AND granted
"""


class UpgradeInProgress(RuntimeError):
    """Raised before a cross-store workflow mutates Redis during an upgrade fence."""


@dataclass(frozen=True, slots=True)
class DrainIdentity:
    job_id: str
    run_id: str
    owner: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class WriterBarrierReceipt:
    operation: Operation
    session_nonce: str
    backend_pid: int
    upgrade_run_id: str | None
    fence_phase: str | None
    drain_identity: DrainIdentity | None = None


_current_drain_receipt: ContextVar[WriterBarrierReceipt | None] = ContextVar(
    "supportguard_current_drain_receipt",
    default=None,
)


@event.listens_for(Session, "after_begin")
def _bind_drain_transaction(session: Session, transaction: Any, connection: Any) -> None:
    if bool(getattr(transaction, "nested", False)):
        return
    receipt = _current_drain_receipt.get()
    if receipt is None or receipt.drain_identity is None or receipt.fence_phase is None:
        return
    drain = receipt.drain_identity
    payload = json.dumps(
        {
            "schema_version": "writer-drain-bind.v1",
            "session_nonce": receipt.session_nonce,
            "job_id": drain.job_id,
            "run_id": drain.run_id,
            "owner": drain.owner,
            "fencing_token": drain.fencing_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    result = connection.execute(
        text("SELECT supportguard_runtime_bind_drain(CAST(:payload AS jsonb))"),
        {"payload": payload},
    ).scalar_one()
    if not isinstance(result, dict) or result.get("result") != "bound":
        raise RuntimeError("writer_drain_transaction_binding_invalid")


def _factory_engine(factory: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    bind = factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("writer barrier requires an AsyncEngine-bound session factory")
    return bind


async def _unlock(connection: AsyncConnection) -> None:
    released = await connection.scalar(
        text(f"SELECT pg_advisory_unlock_shared({_BARRIER_KEY_SQL})")
    )
    if released is not True:
        # Invalidate instead of returning a connection with an ambiguous lock state
        # to the pool. PostgreSQL releases session locks when that backend closes.
        await connection.invalidate()
        raise RuntimeError("writer_barrier_release_not_owned")


@asynccontextmanager
async def cross_store_writer_barrier(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation: Operation,
    allow_quiescing_drain: bool = False,
    drain_identity: DrainIdentity | None = None,
) -> AsyncIterator[WriterBarrierReceipt]:
    """Pin one PG backend and hold the shared barrier for a whole cross-store workflow.

    The connection never returns to the pool while the session advisory lock is held.
    Fence state is re-read only after lock acquisition, closing the race with the
    migrator's exclusive barrier and persistent Maintenance Fence.
    """

    engine = _factory_engine(factory)
    if engine.dialect.name != "postgresql":
        yield WriterBarrierReceipt(
            operation,
            uuid4().hex,
            0,
            None,
            None,
            drain_identity,
        )
        return

    connection = await engine.connect()
    locked = False
    context_token: Token[WriterBarrierReceipt | None] | None = None
    session_nonce = uuid4().hex
    try:
        result = await connection.scalar(
            text(
                "SELECT supportguard_runtime_acquire_writer_barrier("
                "CAST(:payload AS jsonb))"
            ),
            {
                "payload": json.dumps(
                    {
                        "schema_version": "writer-barrier-acquire.v1",
                        "operation": operation,
                        "session_nonce": session_nonce,
                        "drain": (
                            {
                                "job_id": drain_identity.job_id,
                                "run_id": drain_identity.run_id,
                                "owner": drain_identity.owner,
                                "fencing_token": drain_identity.fencing_token,
                            }
                            if drain_identity is not None
                            else None
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        locked = True
        if not isinstance(result, dict):
            raise RuntimeError("writer_barrier_capability_invalid")
        backend_pid = int(result["backend_pid"])
        run_id_raw = result.get("upgrade_run_id")
        phase_raw = result.get("fence_phase")
        run_id = str(run_id_raw) if run_id_raw is not None else None
        phase = str(phase_raw) if phase_raw is not None else None
        await connection.commit()
        permitted = bool(result.get("permitted", False))
        if not permitted:
            raise UpgradeInProgress("upgrade_in_progress")
        if phase is not None and not (
            allow_quiescing_drain
            and phase == "quiescing"
            and drain_identity is not None
        ):
            raise UpgradeInProgress("upgrade_in_progress")
        receipt = WriterBarrierReceipt(
            operation=operation,
            session_nonce=session_nonce,
            backend_pid=backend_pid,
            upgrade_run_id=run_id,
            fence_phase=phase,
            drain_identity=drain_identity,
        )
        context_token = _current_drain_receipt.set(receipt)
        yield receipt
    finally:
        try:
            if context_token is not None:
                _current_drain_receipt.reset(context_token)
            if locked and not connection.invalidated:
                await connection.scalar(
                    text("SELECT supportguard_runtime_release_writer_barrier(:nonce)"),
                    {"nonce": session_nonce},
                )
                await connection.commit()
                await _unlock(connection)
        finally:
            await connection.close()


async def owned_writer_barrier_lock_count(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Return matching session-level shared locks, used by gates and health checks."""

    engine = _factory_engine(factory)
    if engine.dialect.name != "postgresql":
        return 0
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(_LOCK_COUNT_SQL)
            )
            or 0
        )
