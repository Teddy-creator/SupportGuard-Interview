from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard.config import Settings


@dataclass(frozen=True, slots=True)
class RuntimeTiming:
    """One versioned source for queue admission, recovery, and readiness time bounds."""

    schema_version: str
    operational_horizon: timedelta
    job_lease: timedelta
    heartbeat_interval: timedelta
    reconciler_interval: timedelta
    pel_min_idle_ms: int
    timing_version: int = 1
    max_attempts: int = 5
    max_delivery_generation: int = 5
    redelivery_grace_seconds: int = 15
    backlog_count_limit: int = 500
    oldest_backlog_seconds: int = 600
    config_hash: str = "settings-fixture"

    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeTiming:
        return cls(
            schema_version=settings.runtime_threshold_schema_version,
            operational_horizon=timedelta(seconds=settings.runtime_operational_horizon_seconds),
            job_lease=timedelta(seconds=settings.runtime_job_lease_seconds),
            heartbeat_interval=timedelta(seconds=settings.runtime_heartbeat_interval_seconds),
            reconciler_interval=timedelta(seconds=settings.runtime_reconciler_interval_seconds),
            pel_min_idle_ms=settings.redis_pel_min_idle_ms,
            max_attempts=5,
            max_delivery_generation=5,
            redelivery_grace_seconds=15,
            backlog_count_limit=settings.max_durable_backlog,
            oldest_backlog_seconds=settings.runtime_operational_horizon_seconds,
        )

    @classmethod
    async def from_database(
        cls,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> RuntimeTiming:
        """Load the immutable active snapshot; production never trusts process thresholds."""
        async with factory() as session:
            if session.get_bind().dialect.name != "postgresql":
                return cls.from_settings(settings)
            snapshot = await session.scalar(text("SELECT supportguard_runtime_timing_snapshot()"))
        if not isinstance(snapshot, dict):
            raise RuntimeError("runtime control-plane snapshot is invalid")
        return cls(
            schema_version=f"runtime-timing.v{int(snapshot['timing_version'])}",
            operational_horizon=timedelta(seconds=int(snapshot["max_job_age_seconds"])),
            job_lease=timedelta(seconds=int(snapshot["lease_seconds"])),
            heartbeat_interval=timedelta(seconds=settings.runtime_heartbeat_interval_seconds),
            reconciler_interval=timedelta(seconds=settings.runtime_reconciler_interval_seconds),
            pel_min_idle_ms=settings.redis_pel_min_idle_ms,
            timing_version=int(snapshot["timing_version"]),
            max_attempts=int(snapshot["max_attempts"]),
            max_delivery_generation=int(snapshot["max_delivery_generation"]),
            redelivery_grace_seconds=int(snapshot["redelivery_grace_seconds"]),
            backlog_count_limit=int(snapshot["backlog_count_limit"]),
            oldest_backlog_seconds=int(snapshot["oldest_backlog_seconds"]),
            config_hash=str(snapshot["config_hash"]),
        )
