#!/usr/bin/env python3
"""Run destructive-but-reversible Redis and Worker process recovery cases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from supportguard.config import Settings
from supportguard.db.models import (
    BusinessAction,
    OutboxEvent,
    RuntimeJob,
    RuntimeTimingSnapshot,
    ServiceInstanceHeartbeat,
)
from supportguard.db.session import create_engine, create_session_factory


class WorkerConvergenceOutcome(StrEnum):
    PROCESS_RESTART_OR_EXIT = "worker_process_restart_or_exit"
    RESOURCE_PRESSURE = "worker_resource_pressure"
    DEPENDENCY_UNAVAILABLE = "worker_dependency_unavailable"
    HEARTBEAT_MISSING = "worker_heartbeat_missing"
    HEALTHCHECK_FAILED = "worker_healthcheck_failed"
    CONVERGENCE_CONTRACT_INVALID = "worker_convergence_contract_invalid"
    CONTRACT_PASS = "contract_pass"  # noqa: S105  # nosec B105
    DIAGNOSTIC_AMBIGUOUS = "diagnostic_ambiguous"


class WorkerConvergenceFacts(BaseModel):
    """Redacted final facts for one bounded two-Worker convergence attempt."""

    model_config = ConfigDict(frozen=True)

    observations_complete: bool
    expected_replicas: int = Field(default=2, ge=1)
    recreate_requested: bool = False
    prior_replica_count: int = Field(default=0, ge=0)
    identities_replaced: bool = True
    exact_replica_count: int = Field(ge=0)
    unique_identities: bool
    running_count: int = Field(ge=0)
    restarting_count: int = Field(ge=0)
    exited_count: int = Field(ge=0)
    oom_killed_count: int = Field(ge=0)
    restart_count_total: int = Field(ge=0)
    resource_pressure_observed: bool
    postgres_probe_ok_count: int = Field(ge=0)
    redis_probe_ok_count: int = Field(ge=0)
    fresh_ready_heartbeat_count: int = Field(ge=0)
    docker_healthy_count: int = Field(ge=0)
    docker_health_nonzero_count: int = Field(ge=0)
    independent_ready_count: int = Field(ge=0)


def _facts_are_contradictory(facts: WorkerConvergenceFacts) -> bool:
    expected = facts.expected_replicas
    replica_bound_counts = (
        facts.running_count,
        facts.restarting_count,
        facts.exited_count,
        facts.oom_killed_count,
        facts.postgres_probe_ok_count,
        facts.redis_probe_ok_count,
        facts.fresh_ready_heartbeat_count,
        facts.docker_healthy_count,
        facts.docker_health_nonzero_count,
        facts.independent_ready_count,
    )
    if expected != 2 or any(value > facts.exact_replica_count for value in replica_bound_counts):
        return True
    if facts.prior_replica_count > expected:
        return True
    if facts.running_count + facts.exited_count > facts.exact_replica_count:
        return True
    if facts.restarting_count > facts.running_count:
        return True
    if facts.oom_killed_count > facts.exited_count:
        return True
    if facts.docker_healthy_count > facts.running_count:
        return True
    if facts.independent_ready_count > min(
        facts.running_count,
        facts.postgres_probe_ok_count,
        facts.redis_probe_ok_count,
        facts.fresh_ready_heartbeat_count,
    ):
        return True
    if facts.docker_healthy_count and (
        facts.postgres_probe_ok_count < facts.docker_healthy_count
        or facts.fresh_ready_heartbeat_count < facts.docker_healthy_count
    ):
        return True
    return facts.oom_killed_count > 0 and not facts.resource_pressure_observed


def classify_worker_convergence(
    facts: WorkerConvergenceFacts,
) -> WorkerConvergenceOutcome:
    if not facts.observations_complete or _facts_are_contradictory(facts):
        return WorkerConvergenceOutcome.DIAGNOSTIC_AMBIGUOUS
    expected = facts.expected_replicas
    if facts.exact_replica_count != expected or not facts.unique_identities:
        return WorkerConvergenceOutcome.DIAGNOSTIC_AMBIGUOUS
    if facts.recreate_requested and not facts.identities_replaced:
        return WorkerConvergenceOutcome.CONVERGENCE_CONTRACT_INVALID
    if facts.resource_pressure_observed or facts.oom_killed_count:
        return WorkerConvergenceOutcome.RESOURCE_PRESSURE
    if (
        facts.running_count != expected
        or facts.restarting_count
        or facts.exited_count
        or facts.restart_count_total
    ):
        return WorkerConvergenceOutcome.PROCESS_RESTART_OR_EXIT
    if facts.postgres_probe_ok_count != expected or facts.redis_probe_ok_count != expected:
        return WorkerConvergenceOutcome.DEPENDENCY_UNAVAILABLE
    if facts.fresh_ready_heartbeat_count != expected:
        return WorkerConvergenceOutcome.HEARTBEAT_MISSING
    if facts.independent_ready_count != expected:
        return WorkerConvergenceOutcome.DIAGNOSTIC_AMBIGUOUS
    if facts.docker_health_nonzero_count:
        return WorkerConvergenceOutcome.HEALTHCHECK_FAILED
    if facts.docker_healthy_count != expected:
        return WorkerConvergenceOutcome.CONVERGENCE_CONTRACT_INVALID
    return WorkerConvergenceOutcome.CONTRACT_PASS


DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://supportguard:supportguard@localhost:5432/supportguard",
)
API_PORT = int(os.getenv("V125_API_PORT", os.getenv("API_HOST_PORT", "8000")))
API_HOST = os.getenv("API_HOST", "127.0.0.1")
BACKEND_IMAGE = os.getenv("BACKEND_IMAGE", "supportguard-backend:local")
WORKER_CONVERGENCE_TIMEOUT_SECONDS = 180.0
WORKER_CONVERGENCE_POLL_SECONDS = 1.0
WORKER_LOG_TAIL = 160
SAFE_EVENT = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


class WorkerHealthError(RuntimeError):
    def __init__(self, code: str, message: str, transitions: list[dict[str, object]]) -> None:
        super().__init__(message)
        self.code = code
        self.transitions = transitions


class DiagnosedWorkerConvergenceError(RuntimeError):
    def __init__(self, diagnostic: dict[str, object]) -> None:
        outcome = str(diagnostic.get("outcome", "diagnostic_ambiguous"))
        super().__init__(f"worker convergence failed: {outcome}")
        self.diagnostic = diagnostic


def command_output(*args: str) -> str:
    return subprocess.run(  # noqa: S603,S607  # nosec B603
        list(args), check=True, capture_output=True, text=True
    ).stdout.strip()


def bounded_command(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603,S607  # nosec B603
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def execution_identity() -> dict[str, str]:
    tested_commit = os.getenv("TESTED_CODE_COMMIT")
    tested_tree = os.getenv("TESTED_TREE_HASH")
    if bool(tested_commit) != bool(tested_tree):
        raise RuntimeError("tested code commit and tree identity must be supplied together")
    return {
        "tested_code_commit": tested_commit or command_output("git", "rev-parse", "HEAD"),
        "tested_tree": tested_tree or command_output("git", "rev-parse", "HEAD^{tree}"),
        "runner_or_image_digest": command_output(
            "docker",
            "image",
            "inspect",
            BACKEND_IMAGE,
            "--format",
            "{{.Id}}",
        ),
    }


def compose(*args: str) -> None:
    subprocess.run(  # noqa: S603,S607  # nosec B603
        ["/usr/local/bin/docker", "compose", *args],
        check=True,
        env={**os.environ, "DEMO_FAKE_PROVIDER": "true"},
    )


def require_fake_workers() -> None:
    result = subprocess.run(  # noqa: S603,S607  # nosec B603
        [
            "/usr/local/bin/docker",
            "compose",
            "exec",
            "-T",
            "worker",
            "sh",
            "-c",
            '[ "$DEMO_FAKE_PROVIDER" = "true" ]',
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("process fault evidence requires DEMO_FAKE_PROVIDER=true workers")


def worker_rows() -> list[dict[str, object]]:
    output = command_output("/usr/local/bin/docker", "compose", "ps", "worker", "--format", "json")
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def wait_for_worker_health(
    *,
    row_loader: Callable[[], list[dict[str, object]]] = worker_rows,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    timeout_seconds: float = WORKER_CONVERGENCE_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    deadline = clock() + timeout_seconds
    transitions: list[dict[str, object]] = []
    last_projection: str | None = None
    poll_count = 0
    while True:
        poll_count += 1
        rows = row_loader()
        status = [
            {key: row.get(key) for key in ("ID", "Name", "Service", "State", "Health", "ExitCode")}
            for row in rows
        ]
        projection = json.dumps(status, sort_keys=True)
        if projection != last_projection:
            transitions.append(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "poll_index": poll_count,
                    "workers": status,
                }
            )
            last_projection = projection
        transitions[-1]["last_observed_poll_index"] = poll_count
        if len(rows) > 2:
            raise WorkerHealthError(
                "unexpected_replica_count",
                f"unexpected worker replica count: {len(rows)}",
                transitions,
            )
        if len(rows) == 2 and all(
            row.get("State") == "running" and row.get("Health") == "healthy" for row in rows
        ):
            return transitions
        if clock() >= deadline:
            raise WorkerHealthError(
                "worker_health_timeout",
                "workers did not converge before timeout: " + json.dumps(status, sort_keys=True),
                transitions,
            )
        sleeper(WORKER_CONVERGENCE_POLL_SECONDS)


def converge_workers(*, force_recreate: bool = False) -> list[dict[str, object]]:
    """Start both replicas and prove container plus authoritative runtime readiness."""

    started = time.monotonic()
    arguments = ["up", "-d", "--no-deps"]
    if force_recreate:
        arguments.append("--force-recreate")
    arguments.extend(("--scale", "worker=2", "worker"))
    compose(*arguments)
    transitions = wait_for_worker_health()
    remaining = max(
        0.0,
        WORKER_CONVERGENCE_TIMEOUT_SECONDS - (time.monotonic() - started),
    )
    try:
        heartbeat_transitions = wait_for_worker_ready_heartbeats(
            timeout_seconds=remaining,
        )
    except WorkerHealthError as exc:
        raise WorkerHealthError(
            exc.code,
            str(exc),
            [*transitions, *exc.transitions],
        ) from exc
    return [*transitions, *heartbeat_transitions]


def compose_container_ids(*, service: str | None = None) -> list[str]:
    arguments = ["/usr/local/bin/docker", "compose", "ps", "-a", "-q"]
    if service:
        arguments.append(service)
    output = command_output(*arguments)
    return [item for item in output.splitlines() if item]


def worker_identity_projection() -> list[dict[str, str]]:
    worker_ids = compose_container_ids(service="worker")
    if not worker_ids:
        return []
    value = json.loads(command_output("/usr/local/bin/docker", "inspect", *worker_ids))
    if not isinstance(value, list):
        raise RuntimeError("worker identity inspect is invalid")
    projection: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        config = cast(dict[str, Any], item.get("Config") or {})
        labels = cast(dict[str, Any], config.get("Labels") or {})
        projection.append(
            {
                "container_id": str(item.get("Id", "")),
                "name": str(item.get("Name", "")).lstrip("/"),
                "hostname": str(config.get("Hostname", "")),
                "image_id": str(item.get("Image", "")),
                "compose_service": str(labels.get("com.docker.compose.service", "")),
            }
        )
    return projection


def _safe_diagnostic_text(value: object) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    if SAFE_EVENT.fullmatch(text_value):
        return text_value
    return f"sha256:{hashlib.sha256(text_value.encode()).hexdigest()}"


def _safe_health_log(entries: object) -> list[dict[str, object]]:
    if not isinstance(entries, list):
        return []
    safe: list[dict[str, object]] = []
    for item in entries[-20:]:
        if not isinstance(item, dict):
            continue
        safe.append(
            {
                "start": str(item.get("Start", "")),
                "end": str(item.get("End", "")),
                "exit_code": int(item.get("ExitCode", -1)),
                "output": _safe_diagnostic_text(item.get("Output")),
            }
        )
    return safe


def _safe_worker_log(container_id: str) -> dict[str, object]:
    completed = bounded_command(
        "/usr/local/bin/docker", "logs", "--tail", str(WORKER_LOG_TAIL), container_id
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    lines = combined.splitlines()[-WORKER_LOG_TAIL:]
    events: list[dict[str, str]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            item = None
        if not isinstance(item, dict):
            continue
        event: dict[str, str] = {}
        for key in ("timestamp", "level", "logger", "event", "error_code", "error_type"):
            value = _safe_diagnostic_text(item.get(key))
            if value:
                event[key] = value
        if event:
            events.append(event)
    return {
        "container_id": container_id,
        "exit_code": completed.returncode,
        "line_count": len(lines),
        "sha256": hashlib.sha256(combined.encode()).hexdigest(),
        "structured_events": events[-40:],
        "unstructured_text_retained": False,
    }


POSTGRES_PROBE = """
import asyncio, os
import asyncpg
async def main():
    url=os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://',1)
    connection=await asyncpg.connect(url,timeout=3)
    try:
        assert await connection.fetchval('SELECT 1',timeout=2) == 1
    finally:
        await connection.close(timeout=2)
asyncio.run(main())
""".strip()

REDIS_PROBE = """
import asyncio, os
from redis.asyncio import Redis
async def main():
    client=Redis.from_url(os.environ['REDIS_URL'])
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
asyncio.run(main())
""".strip()


def _worker_probe(container_id: str, hostname: str, kind: str) -> dict[str, object]:
    arguments: tuple[str, ...]
    if kind == "postgres":
        arguments = ("python", "-c", POSTGRES_PROBE)
    elif kind == "redis":
        arguments = ("python", "-c", REDIS_PROBE)
    elif kind == "runtime_health":
        arguments = (
            "python",
            "-m",
            "supportguard.runtime_health",
            "--service",
            "worker",
            "--instance",
            hostname,
        )
    else:
        raise ValueError(f"unknown worker probe: {kind}")
    try:
        completed = bounded_command(
            "/usr/local/bin/docker", "exec", container_id, *arguments, timeout=12
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": None}
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
    }


async def _heartbeat_snapshot(instance_ids: Sequence[str]) -> dict[str, object]:
    engine = create_engine(Settings(_env_file=None, database_url=DATABASE_URL))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            timing = await session.scalar(
                select(RuntimeTimingSnapshot).where(RuntimeTimingSnapshot.is_active.is_(True))
            )
            database_now = await session.scalar(select(func.now()))
            rows = list(
                (
                    await session.scalars(
                        select(ServiceInstanceHeartbeat).where(
                            ServiceInstanceHeartbeat.id.in_(list(instance_ids))
                        )
                    )
                ).all()
            )
        if timing is None or database_now is None:
            raise RuntimeError("active runtime timing snapshot is missing")
        cutoff = database_now - timedelta(seconds=timing.max_job_age_seconds)
        heartbeats = []
        for row in rows:
            fresh = (
                row.service == "worker"
                and row.status == "ready"
                and row.last_heartbeat_at >= cutoff
                and row.timing_version == timing.timing_version
                and row.runtime_config_hash == timing.config_hash
            )
            heartbeats.append(
                {
                    "instance_id": row.id,
                    "service": row.service,
                    "status": row.status,
                    "last_heartbeat_at": row.last_heartbeat_at.isoformat(),
                    "timing_version": row.timing_version,
                    "runtime_config_hash": row.runtime_config_hash,
                    "fresh_ready": fresh,
                }
            )
        return {
            "database_now": database_now.isoformat(),
            "freshness_seconds": timing.max_job_age_seconds,
            "active_timing_version": timing.timing_version,
            "active_runtime_config_hash": timing.config_hash,
            "rows": heartbeats,
        }
    finally:
        await engine.dispose()


def wait_for_worker_ready_heartbeats(
    *,
    identity_loader: Callable[[], list[dict[str, str]]] = worker_identity_projection,
    snapshot_loader: Callable[[Sequence[str]], dict[str, object]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    timeout_seconds: float = WORKER_CONVERGENCE_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    """Wait until both current container identities publish fresh ready truth."""

    if snapshot_loader is None:

        def load_snapshot(instance_ids: Sequence[str]) -> dict[str, object]:
            return asyncio.run(_heartbeat_snapshot(instance_ids))

        snapshot_loader = load_snapshot
    deadline = clock() + timeout_seconds
    transitions: list[dict[str, object]] = []
    last_projection: str | None = None
    poll_count = 0
    while True:
        poll_count += 1
        identities = identity_loader()
        instance_ids = [item["hostname"] for item in identities if item.get("hostname")]
        snapshot = snapshot_loader(instance_ids)
        raw_rows = snapshot.get("rows", [])
        rows = {str(item.get("instance_id")): item for item in raw_rows if isinstance(item, dict)}
        ready_ids = sorted(
            instance_id
            for instance_id in instance_ids
            if isinstance(rows.get(instance_id), dict)
            and rows[instance_id].get("fresh_ready") is True
        )
        projection = json.dumps(
            {
                "instance_ids": sorted(instance_ids),
                "ready_ids": ready_ids,
            },
            sort_keys=True,
        )
        if projection != last_projection:
            transitions.append(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "poll_index": poll_count,
                    "worker_heartbeat": json.loads(projection),
                }
            )
            last_projection = projection
        transitions[-1]["last_observed_poll_index"] = poll_count
        if (
            len(instance_ids) == 2
            and len(set(instance_ids)) == 2
            and ready_ids == sorted(instance_ids)
        ):
            return transitions
        if clock() >= deadline:
            raise WorkerHealthError(
                "worker_heartbeat_timeout",
                "workers did not publish two identity-bound ready heartbeats "
                f"before timeout: {projection}",
                transitions,
            )
        sleeper(WORKER_CONVERGENCE_POLL_SECONDS)


def _resource_snapshot(container_ids: Sequence[str]) -> list[dict[str, object]]:
    if not container_ids:
        return []
    completed = bounded_command(
        "/usr/local/bin/docker",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        *container_ids,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError("docker_stats_failed")
    rows = []
    for line in completed.stdout.splitlines():
        item = json.loads(line)
        rows.append(
            {key: item.get(key) for key in ("ID", "Name", "CPUPerc", "MemUsage", "MemPerc", "PIDs")}
        )
    return rows


def _shadow_workload_snapshot(label: str) -> dict[str, object]:
    completed = bounded_command(
        "/usr/local/bin/docker",
        "ps",
        "-q",
        "--filter",
        f"label={label}",
    )
    if completed.returncode:
        raise RuntimeError("shadow_container_list_failed")
    container_ids = [item for item in completed.stdout.splitlines() if item]
    identities: list[dict[str, str]] = []
    if container_ids:
        value = json.loads(command_output("/usr/local/bin/docker", "inspect", *container_ids))
        if not isinstance(value, list):
            raise RuntimeError("shadow container inspect is invalid")
        for item in value:
            if not isinstance(item, dict):
                continue
            state = cast(dict[str, Any], item.get("State") or {})
            identities.append(
                {
                    "container_id": str(item.get("Id", "")),
                    "name": str(item.get("Name", "")).lstrip("/"),
                    "image_id": str(item.get("Image", "")),
                    "state": str(state.get("Status", "")),
                }
            )
    return {
        "label": label,
        "running_container_count": len(container_ids),
        "containers": identities,
        "resources": _resource_snapshot(container_ids),
    }


def capture_worker_convergence(
    *,
    transitions: list[dict[str, object]],
    wait_error_code: str | None,
    before_recreate: list[dict[str, str]] | None = None,
    before_recreate_error: str | None = None,
    recreate_requested: bool = False,
) -> dict[str, object]:
    collection_errors: list[str] = []
    if before_recreate_error:
        collection_errors.append(before_recreate_error)
    worker_ids: list[str] = []
    inspected: list[dict[str, Any]] = []
    heartbeat: dict[str, object] = {"rows": []}
    resources: list[dict[str, object]] = []
    shadow_workload: dict[str, object] | None = None
    logs: list[dict[str, object]] = []
    probes: dict[str, dict[str, dict[str, object]]] = {}

    try:
        worker_ids = compose_container_ids(service="worker")
        if worker_ids:
            raw = command_output("/usr/local/bin/docker", "inspect", *worker_ids)
            value = json.loads(raw)
            if isinstance(value, list):
                inspected = [item for item in value if isinstance(item, dict)]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        collection_errors.append(f"worker_inspect:{type(exc).__name__}")

    hostnames = [str(item.get("Config", {}).get("Hostname", "")) for item in inspected]
    hostnames = [item for item in hostnames if item]
    try:
        heartbeat = asyncio.run(_heartbeat_snapshot(hostnames))
    except (OSError, RuntimeError, ValueError) as exc:
        collection_errors.append(f"heartbeat_snapshot:{type(exc).__name__}")

    raw_heartbeat_rows = heartbeat.get("rows", [])
    if not isinstance(raw_heartbeat_rows, list):
        raw_heartbeat_rows = []
    heartbeat_rows: dict[str, dict[str, Any]] = {
        str(item.get("instance_id")): item for item in raw_heartbeat_rows if isinstance(item, dict)
    }
    containers: list[dict[str, Any]] = []
    for item in inspected:
        container_id = str(item.get("Id", ""))
        config = cast(dict[str, Any], item.get("Config") or {})
        labels = cast(dict[str, Any], config.get("Labels") or {})
        hostname = str(config.get("Hostname", ""))
        state = cast(dict[str, Any], item.get("State") or {})
        health = cast(dict[str, Any], state.get("Health") or {})
        health_log = _safe_health_log(health.get("Log"))
        running = bool(state.get("Running"))
        container_probes: dict[str, dict[str, object]] = {}
        if running:
            for kind in ("postgres", "redis", "runtime_health"):
                container_probes[kind] = _worker_probe(container_id, hostname, kind)
        else:
            for kind in ("postgres", "redis", "runtime_health"):
                container_probes[kind] = {"status": "not_run", "exit_code": None}
        probes[container_id] = container_probes
        heartbeat_row = heartbeat_rows.get(hostname)
        containers.append(
            {
                "container_id": container_id,
                "name": str(item.get("Name", "")).lstrip("/"),
                "hostname": hostname,
                "image_id": str(item.get("Image", "")),
                "compose_service": str(labels.get("com.docker.compose.service", "")),
                "state": {
                    "status": str(state.get("Status", "")),
                    "running": running,
                    "restarting": bool(state.get("Restarting")),
                    "oom_killed": bool(state.get("OOMKilled")),
                    "exit_code": int(state.get("ExitCode", -1)),
                    "restart_count": int(item.get("RestartCount", 0)),
                    "started_at": str(state.get("StartedAt", "")),
                    "finished_at": str(state.get("FinishedAt", "")),
                },
                "docker_health": {
                    "status": str(health.get("Status", "missing")),
                    "failing_streak": int(health.get("FailingStreak", 0)),
                    "attempts": health_log,
                },
                "heartbeat": heartbeat_row,
                "probes": container_probes,
            }
        )
        try:
            logs.append(_safe_worker_log(container_id))
            if logs[-1]["exit_code"] != 0:
                collection_errors.append("worker_log:nonzero_exit")
        except (OSError, subprocess.SubprocessError) as exc:
            collection_errors.append(f"worker_log:{type(exc).__name__}")

    try:
        resource_ids: list[str] = []
        for service in ("worker", "postgres", "redis", "api"):
            resource_ids.extend(compose_container_ids(service=service))
        resources = _resource_snapshot(resource_ids)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        collection_errors.append(f"resource_snapshot:{type(exc).__name__}")

    shadow_label = os.getenv("PROCESS_FAULT_SHADOW_LABEL")
    if shadow_label:
        try:
            shadow_workload = _shadow_workload_snapshot(shadow_label)
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            collection_errors.append(f"shadow_workload:{type(exc).__name__}")

    exact_count = len(containers)
    before_recreate = before_recreate or []
    prior_ids = {item["container_id"] for item in before_recreate}
    current_ids = {str(item["container_id"]) for item in containers}
    identities_replaced = not prior_ids or prior_ids.isdisjoint(current_ids)
    running_count = sum(1 for item in containers if item["state"]["running"])
    restarting_count = sum(1 for item in containers if item["state"]["restarting"])
    exited_count = sum(1 for item in containers if item["state"]["status"] in {"exited", "dead"})
    oom_count = sum(1 for item in containers if item["state"]["oom_killed"])
    postgres_ok = sum(1 for item in containers if item["probes"]["postgres"]["status"] == "passed")
    redis_ok = sum(1 for item in containers if item["probes"]["redis"]["status"] == "passed")
    heartbeat_ok = sum(
        1
        for item in containers
        if isinstance(item.get("heartbeat"), dict) and item["heartbeat"].get("fresh_ready") is True
    )
    docker_healthy = sum(1 for item in containers if item["docker_health"]["status"] == "healthy")
    docker_health_nonzero = sum(
        1
        for item in containers
        if item["docker_health"]["attempts"]
        and item["docker_health"]["attempts"][-1]["exit_code"] != 0
    )
    independent_ready = sum(
        1
        for item in containers
        if item["probes"]["postgres"]["status"] == "passed"
        and item["probes"]["redis"]["status"] == "passed"
        and item["probes"]["runtime_health"]["status"] == "passed"
        and isinstance(item.get("heartbeat"), dict)
        and item["heartbeat"].get("fresh_ready") is True
    )
    complete = (
        not collection_errors
        and len(inspected) == len(worker_ids)
        and all(item.get("docker_health", {}).get("status") != "missing" for item in containers)
        and "database_now" in heartbeat
    )
    facts = WorkerConvergenceFacts(
        observations_complete=complete,
        recreate_requested=recreate_requested,
        prior_replica_count=len(before_recreate),
        identities_replaced=identities_replaced,
        exact_replica_count=exact_count,
        unique_identities=len(worker_ids) == len(set(worker_ids))
        and len(hostnames) == len(set(hostnames)),
        running_count=running_count,
        restarting_count=restarting_count,
        exited_count=exited_count,
        oom_killed_count=oom_count,
        restart_count_total=sum(int(item["state"]["restart_count"]) for item in containers),
        resource_pressure_observed=oom_count > 0,
        postgres_probe_ok_count=postgres_ok,
        redis_probe_ok_count=redis_ok,
        fresh_ready_heartbeat_count=heartbeat_ok,
        docker_healthy_count=docker_healthy,
        docker_health_nonzero_count=docker_health_nonzero,
        independent_ready_count=independent_ready,
    )
    outcome = classify_worker_convergence(facts)
    return {
        "schema_version": "worker-convergence-diagnostic.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "compose_project": os.getenv("COMPOSE_PROJECT_NAME", Path.cwd().name),
        "step_name": "process-faults:worker-force-recreate",
        "timeout_seconds": WORKER_CONVERGENCE_TIMEOUT_SECONDS,
        "poll_seconds": WORKER_CONVERGENCE_POLL_SECONDS,
        "poll_denominator": max(
            (cast(int, item.get("last_observed_poll_index", 0)) for item in transitions),
            default=0,
        ),
        "wait_error_code": wait_error_code,
        "before_recreate": before_recreate,
        "identities_replaced": identities_replaced,
        "observed_transitions": transitions,
        "containers": containers,
        "heartbeat_snapshot": heartbeat,
        "resource_snapshot": resources,
        "shadow_workload": shadow_workload,
        "worker_logs": logs,
        "facts": facts.model_dump(mode="json"),
        "outcome": outcome.value,
        "first_break_boundary": (
            outcome.value if outcome is not WorkerConvergenceOutcome.CONTRACT_PASS else None
        ),
        "collection_errors": collection_errors,
    }


async def diagnosed_converge_workers(*, force_recreate: bool = False) -> dict[str, object]:
    before_recreate: list[dict[str, str]] = []
    before_recreate_error: str | None = None
    try:
        before_recreate = await asyncio.to_thread(worker_identity_projection)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        before_recreate_error = f"before_recreate:{type(exc).__name__}"
    try:
        # v1.2.14 retained call shape: await asyncio.to_thread(converge_workers)
        transitions = await asyncio.to_thread(converge_workers, force_recreate=force_recreate)
        diagnostic = await asyncio.to_thread(
            capture_worker_convergence,
            transitions=transitions or [],
            wait_error_code=None,
            before_recreate=before_recreate,
            before_recreate_error=before_recreate_error,
            recreate_requested=force_recreate,
        )
    except WorkerHealthError as exc:
        diagnostic = await asyncio.to_thread(
            capture_worker_convergence,
            transitions=exc.transitions,
            wait_error_code=exc.code,
            before_recreate=before_recreate,
            before_recreate_error=before_recreate_error,
            recreate_requested=force_recreate,
        )
    if diagnostic.get("outcome") != WorkerConvergenceOutcome.CONTRACT_PASS.value:
        raise DiagnosedWorkerConvergenceError(diagnostic)
    return diagnostic


async def customer_client() -> tuple[httpx.AsyncClient, str]:
    client = httpx.AsyncClient(base_url=f"http://{API_HOST}:{API_PORT}", timeout=20)
    response = await client.post(
        "/api/demo-sessions", json={"role": "customer", "customer_id": "cust_demo"}
    )
    response.raise_for_status()
    return client, str(response.json()["csrf_token"])


async def submit(client: httpx.AsyncClient, csrf: str, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/tickets",
        json={"message": "atlas-chat 返回 429 concurrency_limit_exceeded，为什么？"},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"fault-{label}-{uuid4().hex}",
        },
    )
    if response.status_code != 202:
        raise RuntimeError(f"command was not accepted: {response.status_code} {response.text}")
    return {key: str(response.json()[key]) for key in ("ticket_id", "run_id", "job_id")}


async def wait_job(job_id: str, statuses: set[str], timeout_seconds: float) -> RuntimeJob:
    engine = create_engine(Settings(_env_file=None, database_url=DATABASE_URL))
    factory = create_session_factory(engine)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            async with factory() as session:
                job = await session.get(RuntimeJob, job_id)
                if job is not None and job.status in statuses:
                    return job
            await asyncio.sleep(0.05)
        async with factory() as session:
            job = await session.get(RuntimeJob, job_id)
            if job is not None and job.status in statuses:
                return job
    finally:
        await engine.dispose()
    raise RuntimeError(f"job {job_id} did not reach {sorted(statuses)}")


async def redis_recovery(client: httpx.AsyncClient, csrf: str) -> dict[str, object]:
    await asyncio.to_thread(compose, "stop", "redis")
    try:
        accepted = await submit(client, csrf, "redis")
        engine = create_engine(Settings(_env_file=None, database_url=DATABASE_URL))
        factory = create_session_factory(engine)
        async with factory() as session:
            job = await session.get(RuntimeJob, accepted["job_id"])
            outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.job_id == accepted["job_id"])
            )
            durable_before_recovery = bool(
                job is not None
                and job.status == "queued"
                and outbox is not None
                and outbox.published_at is None
            )
        await engine.dispose()
    finally:
        await asyncio.to_thread(compose, "start", "redis")
    started = time.monotonic()
    terminal = await wait_job(accepted["job_id"], {"succeeded", "dead"}, 120)
    return {
        "injection": "docker compose stop redis",
        "accepted_status": 202,
        "durable_before_recovery": durable_before_recovery,
        "job_id": accepted["job_id"],
        "run_id": accepted["run_id"],
        "final_status": terminal.status,
        "recovery_seconds": time.monotonic() - started,
        "lost_jobs": int(terminal.status != "succeeded"),
    }


async def worker_kill(client: httpx.AsyncClient, csrf: str) -> dict[str, object]:
    # Redis recovery may legitimately restart supervised workers.  Establish a
    # new strict identity/restart epoch before injecting the independent kill.
    before_kill_convergence = await diagnosed_converge_workers(force_recreate=True)
    accepted = await submit(client, csrf, "worker-kill")
    leased = await wait_job(accepted["job_id"], {"leased"}, 60)
    if not leased.lease_owner:
        raise RuntimeError("leased job has no owner")
    killed_owner = leased.lease_owner
    initial_fence = leased.fencing_token
    await asyncio.to_thread(
        subprocess.run, ["/usr/local/bin/docker", "kill", killed_owner], check=True
    )  # noqa: S603,S607
    started = time.monotonic()
    try:
        terminal = await wait_job(accepted["job_id"], {"succeeded", "dead"}, 120)
    finally:
        # The exact leased-worker kill is itself a controlled process exit.  A
        # new force-recreate epoch proves post-fault convergence without
        # teaching the strict classifier to ignore unexpected restart counts.
        after_kill_convergence = await diagnosed_converge_workers(force_recreate=True)
    return {
        "injection": "docker kill leased worker",
        "job_id": accepted["job_id"],
        "run_id": accepted["run_id"],
        "killed_owner": killed_owner,
        "initial_fencing_token": initial_fence,
        "final_fencing_token": terminal.fencing_token,
        "final_status": terminal.status,
        "recovery_seconds": time.monotonic() - started,
        "stale_fence_rejected": terminal.fencing_token > initial_fence,
        "epoch_reset_contract": "force-recreate-new-identity.v1",
        "before_kill_convergence": before_kill_convergence,
        "after_kill_convergence": after_kill_convergence,
    }


async def duplicate_actions() -> int:
    engine = create_engine(Settings(_env_file=None, database_url=DATABASE_URL))
    factory = create_session_factory(engine)
    async with factory() as session:
        groups = await session.scalar(
            select(func.count())
            .select_from(BusinessAction)
            .where(BusinessAction.status == "succeeded")
            .group_by(
                BusinessAction.tenant_id,
                BusinessAction.action_type,
                BusinessAction.resource_id,
                BusinessAction.resource_version,
            )
            .having(func.count() > 1)
            .limit(1)
        )
    await engine.dispose()
    return int(groups or 0)


def write_process_fault_report(report: dict[str, object]) -> tuple[Path, str]:
    encoded = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    digest = hashlib.sha256(encoded).hexdigest()
    configured_path = os.getenv("PROCESS_FAULT_REPORT_PATH")
    path = (
        Path(configured_path)
        if configured_path
        else Path("evals/reports/evidence") / f"process-faults-{digest[:16]}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise RuntimeError(f"append-only process fault artifact already exists: {path}") from exc
    return path, digest


def _failure_code(exc: Exception, stage: str) -> str:
    if isinstance(exc, DiagnosedWorkerConvergenceError):
        return f"worker_convergence_failed:{exc.diagnostic.get('outcome')}"
    return f"process_fault_failed:{stage}:{type(exc).__name__}"


async def main() -> None:
    convergence_only = os.getenv("PROCESS_FAULT_CONVERGENCE_ONLY", "false") == "true"
    report: dict[str, object] = {
        "schema_version": (
            "worker-convergence-preflight.v1" if convergence_only else "process-fault-report.v1"
        ),
        "diagnostic_schema_version": "worker-convergence-diagnostic.v1",
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        **execution_identity(),
    }
    client: httpx.AsyncClient | None = None
    stage = "worker_force_recreate"
    try:
        report["worker_convergence"] = await diagnosed_converge_workers(force_recreate=True)
        if convergence_only:
            report["status"] = "passed"
            path, digest = write_process_fault_report(report)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "outcome": report["worker_convergence"]["outcome"],  # type: ignore[index]
                        "artifact": str(path),
                        "artifact_sha256": digest,
                    },
                    ensure_ascii=False,
                )
            )
            return
        require_fake_workers()
        stage = "customer_session"
        client, csrf = await customer_client()
        stage = "redis_recovery"
        report["redis_recovery"] = await redis_recovery(client, csrf)
        stage = "worker_kill"
        report["worker_kill"] = await worker_kill(client, csrf)
        stage = "duplicate_business_action_check"
        report["duplicate_business_action_groups"] = await duplicate_actions()
        redis_result = report["redis_recovery"]
        worker_result = report["worker_kill"]
        if not isinstance(redis_result, dict) or not isinstance(worker_result, dict):
            raise RuntimeError("process fault result schema invalid")
        if (
            redis_result.get("final_status") != "succeeded"
            or redis_result.get("lost_jobs") != 0
            or worker_result.get("final_status") != "succeeded"
            or worker_result.get("stale_fence_rejected") is not True
            or report["duplicate_business_action_groups"] != 0
        ):
            raise RuntimeError("process fault acceptance predicates failed")
        report["status"] = "passed"
    except Exception as exc:
        if isinstance(exc, DiagnosedWorkerConvergenceError):
            report[f"{stage}_diagnostic"] = exc.diagnostic
        error_code = _failure_code(exc, stage)
        report["status"] = "failed"
        report["failure"] = {"stage": stage, "error_code": error_code}
        try:
            path, digest = write_process_fault_report(report)
        except (OSError, RuntimeError) as artifact_exc:
            raise RuntimeError("diagnostic_artifact_write_failed") from artifact_exc
        raise RuntimeError(f"{error_code}:artifact={path}:sha256={digest}") from exc
    finally:
        if client is not None:
            await client.aclose()

    path, digest = write_process_fault_report(report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "outcome": report["worker_convergence"]["outcome"],  # type: ignore[index]
                "artifact": str(path),
                "artifact_sha256": digest,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
