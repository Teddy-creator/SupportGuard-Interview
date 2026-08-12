from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard import __version__
from supportguard.db.models import ServiceInstanceHeartbeat
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.services.schema_rollout import (
    READER_CONTRACT,
    DatabaseIdentity,
    SchemaRolloutSnapshot,
    WriterService,
    classify_schema_probe_failure,
    inspect_schema_rollout,
    inspect_writer_schema_rollout,
    schema_rollout_for_head,
)

WRITER_BINARY_CONTRACT = "v1512-double-write.v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceHeartbeatSnapshot:
    status: Literal["ready", "degraded", "stopped"]
    capabilities: tuple[str, ...]
    migration_head: str = CURRENT_PRODUCT_DATABASE_HEAD
    database_identity: DatabaseIdentity = "interview_baseline"
    reader_contract: str = READER_CONTRACT
    writer_contract: str | None = None
    writer_contract_generation: int | None = None


def bind_heartbeat_to_rollout(
    snapshot: ServiceHeartbeatSnapshot,
    rollout: SchemaRolloutSnapshot,
    *,
    service: str,
) -> ServiceHeartbeatSnapshot:
    capabilities = tuple(
        value
        for value in snapshot.capabilities
        if not value.startswith(
            (
                "migration_head:",
                "database_head:",
                "database_identity:",
                "reader_contract:",
                "writer_contract:",
                "writer_binary:",
            )
        )
    )
    capabilities += (
        f"migration_head:{rollout.database_head}",
        f"database_head:{rollout.database_head}",
        f"database_identity:{rollout.database_identity}",
        f"reader_contract:{rollout.reader_contract}",
        (f"writer_contract:{rollout.writer_contract_generation}:{rollout.writer_contract}"),
    )
    writer_service = service in {
        "worker",
        "dispatcher",
        "reconciler",
        "action_mcp",
    }
    if writer_service:
        capabilities += (f"writer_binary:{WRITER_BINARY_CONTRACT}",)
    status = snapshot.status
    if writer_service and not rollout.current_writer_compatible and status == "ready":
        status = "degraded"
    return ServiceHeartbeatSnapshot(
        status=status,
        capabilities=capabilities,
        migration_head=rollout.database_head,
        database_identity=rollout.database_identity,
        reader_contract=rollout.reader_contract,
        writer_contract=rollout.writer_contract,
        writer_contract_generation=rollout.writer_contract_generation,
    )


def heartbeat_wire_payload(snapshot: ServiceHeartbeatSnapshot) -> str:
    capabilities = tuple(dict.fromkeys(snapshot.capabilities))
    if len(capabilities) > 24 or any(not value or len(value) > 256 for value in capabilities):
        raise ValueError("service heartbeat capabilities are outside the bounded contract")
    rollout = schema_rollout_for_head(snapshot.migration_head)
    if snapshot.database_identity != rollout.database_identity:
        raise ValueError("service heartbeat database identity is inconsistent")
    if snapshot.writer_contract is None or snapshot.writer_contract_generation is None:
        raise ValueError("service heartbeat must be bound to a database rollout")
    if (
        snapshot.writer_contract != rollout.writer_contract
        or snapshot.writer_contract_generation != rollout.writer_contract_generation
    ):
        raise ValueError("service heartbeat rollout binding is inconsistent")
    return json.dumps(
        {
            "schema_version": "service-heartbeat.v2",
            "version": __version__,
            "status": snapshot.status,
            "capabilities": capabilities,
            "migration_head": snapshot.migration_head,
            "database_head": snapshot.migration_head,
            "database_identity": snapshot.database_identity,
            "reader_contract": snapshot.reader_contract,
            "writer_contract": snapshot.writer_contract,
            "writer_contract_generation": snapshot.writer_contract_generation,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@asynccontextmanager
async def service_heartbeat(
    factory: async_sessionmaker[AsyncSession],
    *,
    instance_id: str,
    service: WriterService,
    capabilities: list[str],
    interval_seconds: float = 10,
    snapshot_provider: Callable[[], ServiceHeartbeatSnapshot] | None = None,
) -> AsyncIterator[None]:
    last_snapshot: ServiceHeartbeatSnapshot | None = None

    def current_snapshot() -> ServiceHeartbeatSnapshot:
        nonlocal last_snapshot
        if snapshot_provider is None:
            snapshot = ServiceHeartbeatSnapshot(
                status="ready",
                capabilities=tuple(capabilities),
            )
        else:
            snapshot = snapshot_provider()
        last_snapshot = snapshot
        return snapshot

    async def publish_once(snapshot: ServiceHeartbeatSnapshot) -> None:
        async with factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                rollout = await inspect_writer_schema_rollout(
                    session,
                    service=service,
                )
                effective_snapshot = bind_heartbeat_to_rollout(
                    snapshot,
                    rollout,
                    service=service,
                )
                await session.execute(
                    text(
                        "SELECT supportguard_record_service_heartbeat("
                        ":instance_id,:service,:version)"
                    ),
                    {
                        "instance_id": instance_id,
                        "service": service,
                        "version": heartbeat_wire_payload(effective_snapshot),
                    },
                )
            else:
                rollout = await inspect_schema_rollout(session)
                effective_snapshot = bind_heartbeat_to_rollout(
                    snapshot,
                    rollout,
                    service=service,
                )
                heartbeat = await session.get(ServiceInstanceHeartbeat, instance_id)
                now = datetime.now(UTC)
                if heartbeat is None:
                    session.add(
                        ServiceInstanceHeartbeat(
                            id=instance_id,
                            service=service,
                            capabilities=list(effective_snapshot.capabilities),
                            version=__version__,
                            status=effective_snapshot.status,
                            last_heartbeat_at=now,
                            timing_version=1,
                            runtime_config_hash="settings-fixture",
                        )
                    )
                else:
                    heartbeat.capabilities = list(effective_snapshot.capabilities)
                    heartbeat.version = __version__
                    heartbeat.status = effective_snapshot.status
                    heartbeat.last_heartbeat_at = now
                    heartbeat.timing_version = 1
                    heartbeat.runtime_config_hash = "settings-fixture"

    await publish_once(current_snapshot())
    owner_task = asyncio.current_task()
    if owner_task is None:
        raise RuntimeError("service heartbeat requires an owning task")

    async def publish() -> None:
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await publish_once(current_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "service_heartbeat_publish_failed",
                extra={
                    "error_type": type(exc).__name__,
                    "service": service,
                },
            )
            owner_task.cancel()
            raise

    task = asyncio.create_task(publish())
    try:
        yield
    finally:
        body_error = sys.exception()
        task.cancel()
        background_error: Exception | None = None
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            background_error = exc
        stopped_capabilities = (
            last_snapshot.capabilities if last_snapshot is not None else tuple(capabilities)
        )
        try:
            await publish_once(
                ServiceHeartbeatSnapshot(
                    status="stopped",
                    capabilities=stopped_capabilities,
                )
            )
        except Exception as exc:
            failure = classify_schema_probe_failure(exc)
            if background_error is None and body_error is None and failure != "transient":
                raise
            logger.warning(
                "service_heartbeat_stop_publish_failed",
                extra={
                    "error_type": type(exc).__name__,
                    "failure_class": failure or "secondary",
                    "service": service,
                },
            )
        if background_error is not None:
            raise background_error
