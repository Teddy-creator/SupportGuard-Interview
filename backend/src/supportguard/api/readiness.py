from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from supportguard.api.auth import app_settings
from supportguard.db.models import RuntimeJob, ServiceInstanceHeartbeat
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.services.schema_rollout import (
    SchemaRolloutSnapshot,
    reference_head_is_contract,
    schema_rollout_for_head,
    schema_rollout_from_capability,
)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["supportguard"] = "supportguard"
    version: str
    provider_mode: str
    provider_model: str
    tool_call_mode: str
    mcp: dict[str, dict[str, object]]
    auth_mode: Literal["development", "production"]


class ReadinessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["healthy", "compatible_read_only", "degraded"]
    snapshot_id: str
    evaluated_at: datetime
    timing_version: int
    dependencies: dict[str, object]


async def evaluate_readiness(request: Request) -> ReadinessSnapshot:
    settings = app_settings(request)
    dependencies: dict[str, object] = {}
    ready = True
    compatible_read_only = False
    timing_version = 0
    rollout: SchemaRolloutSnapshot | None = None
    migration_head_source = "unavailable"
    postgresql_capabilities_verified = False
    try:
        async with request.app.state.factory() as session:
            await session.execute(text("SELECT 1"))
            if session.get_bind().dialect.name == "postgresql":
                snapshot = await session.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
                if not isinstance(snapshot, dict):
                    raise RuntimeError("runtime control-plane snapshot is invalid")
                backlog = int(snapshot["active_count"])
                raw_oldest = snapshot.get("oldest_created_at")
                oldest = datetime.fromisoformat(str(raw_oldest)) if raw_oldest else None
                backlog_limit = int(snapshot["backlog_count_limit"])
                oldest_limit_seconds = int(snapshot["oldest_backlog_seconds"])
                timing_version = int(snapshot["timing_version"])
                timing_config_hash = str(snapshot["config_hash"])
                database_now = datetime.fromisoformat(str(snapshot["database_now"]))
                workers = int(snapshot["ready_worker_count"])
                worker_components = snapshot.get("worker_components", [])
                rollout = schema_rollout_from_capability(snapshot)
                migration_head = rollout.database_head
                migration_head_source = "postgresql_runtime_snapshot"
                postgresql_capabilities_verified = True
            else:
                backlog = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(RuntimeJob)
                        .where(RuntimeJob.status.in_({"queued", "retry_wait", "leased"}))
                    )
                    or 0
                )
                oldest = await session.scalar(
                    select(func.min(RuntimeJob.created_at)).where(
                        RuntimeJob.status.in_({"queued", "retry_wait", "leased"})
                    )
                )
                backlog_limit = settings.max_durable_backlog
                oldest_limit_seconds = settings.runtime_operational_horizon_seconds
                timing_version = 1
                timing_config_hash = "settings-fixture"
                database_now = datetime.now(UTC)
                cutoff = database_now - timedelta(seconds=30)
                workers = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ServiceInstanceHeartbeat)
                        .where(
                            ServiceInstanceHeartbeat.service == "worker",
                            ServiceInstanceHeartbeat.status == "ready",
                            ServiceInstanceHeartbeat.last_heartbeat_at >= cutoff,
                            ServiceInstanceHeartbeat.timing_version == timing_version,
                            ServiceInstanceHeartbeat.runtime_config_hash == timing_config_hash,
                        )
                    )
                    or 0
                )
                heartbeat_rows = (
                    await session.scalars(
                        select(ServiceInstanceHeartbeat).where(
                            ServiceInstanceHeartbeat.service == "worker",
                            ServiceInstanceHeartbeat.last_heartbeat_at >= cutoff,
                        )
                    )
                ).all()
                worker_components = [
                    {
                        "instance_id": heartbeat.id,
                        "status": heartbeat.status,
                        "version": heartbeat.version,
                        "capabilities": heartbeat.capabilities,
                        "last_heartbeat_at": heartbeat.last_heartbeat_at,
                        "timing_version": heartbeat.timing_version,
                        "runtime_config_hash": heartbeat.runtime_config_hash,
                    }
                    for heartbeat in heartbeat_rows
                ]
                # SQLite test databases are built from the current ORM metadata.
                # Keep PostgreSQL-only capability proof explicitly false.
                migration_head = CURRENT_PRODUCT_DATABASE_HEAD
                rollout = schema_rollout_for_head(migration_head)
                migration_head_source = "current_metadata_fixture"
            if database_now.tzinfo is None:
                database_now = database_now.replace(tzinfo=UTC)
        if rollout is None:
            rollout = schema_rollout_for_head("")
        compatible_read_only = rollout.reader_compatible and not rollout.current_writer_compatible
        dependencies["postgres"] = {"status": "healthy", "backlog": backlog}
        dependencies["workers"] = {
            "status": (
                "not_required"
                if compatible_read_only
                else "healthy"
                if workers >= 1
                else "degraded"
            ),
            "ready_instances": workers,
            "timing_version": timing_version,
            "runtime_config_hash": timing_config_hash,
            "components": worker_components,
        }
        dependencies["migration"] = {
            "status": (
                "healthy"
                if rollout.current_writer_compatible and reference_head_is_contract()
                else "compatible_read_only"
                if rollout.reader_compatible
                else "unsupported"
            ),
            "actual": migration_head,
            "expected": CURRENT_PRODUCT_DATABASE_HEAD,
            "database_identity": rollout.database_identity,
            "head_source": migration_head_source,
            "postgresql_capabilities_verified": postgresql_capabilities_verified,
            "reader_contract": rollout.reader_contract,
            "reader_compatible": rollout.reader_compatible,
            "writer_contract": rollout.writer_contract,
            "writer_contract_generation": rollout.writer_contract_generation,
            "writer_enabled": rollout.writer_enabled,
        }
        if not rollout.reader_compatible:
            ready = False
        if rollout.current_writer_compatible and (
            migration_head != CURRENT_PRODUCT_DATABASE_HEAD or not reference_head_is_contract()
        ):
            ready = False
        if rollout.current_writer_compatible and workers < 1:
            ready = False
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age_seconds = (
            max(0.0, (database_now - oldest).total_seconds()) if oldest is not None else 0.0
        )
        postgres_state: dict[str, object] = {
            "status": "healthy",
            "backlog": backlog,
            "oldest_backlog_seconds": oldest_age_seconds,
            "backlog_limit": backlog_limit,
            "oldest_limit_seconds": oldest_limit_seconds,
        }
        dependencies["postgres"] = postgres_state
        if rollout.current_writer_compatible and (
            backlog >= backlog_limit or oldest_age_seconds >= oldest_limit_seconds
        ):
            ready = False
            postgres_state["status"] = "degraded"
    except Exception as exc:
        ready = False
        dependencies["postgres"] = {"status": "unavailable", "error": type(exc).__name__}
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        dependencies["redis"] = {"status": "healthy"}
    except Exception as exc:
        dependencies["redis"] = {"status": "degraded", "error": type(exc).__name__}
    finally:
        await redis.aclose()
    dependencies["auth"] = {"status": "healthy", "mode": settings.auth_mode}
    evaluated_at = datetime.now(UTC)
    readiness_status = (
        "compatible_read_only"
        if ready and compatible_read_only
        else "healthy"
        if ready
        else "degraded"
    )
    identity = json.dumps(
        {
            "status": readiness_status,
            "timing_version": timing_version,
            "dependencies": dependencies,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return ReadinessSnapshot(
        status=readiness_status,
        snapshot_id=hashlib.sha256(identity).hexdigest(),
        evaluated_at=evaluated_at,
        timing_version=timing_version,
        dependencies=dependencies,
    )


def require_internal_token(
    request: Request,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    expected = app_settings(request).internal_api_token.get_secret_value()
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


__all__ = [
    "HealthResponse",
    "ReadinessSnapshot",
    "evaluate_readiness",
    "require_internal_token",
]
