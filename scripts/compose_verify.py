#!/usr/bin/env python3
"""Verify the frozen local Compose topology without mutating application state."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed Docker CLI diagnostics only
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Callable
from typing import Any, cast

MCP_READY_TIMEOUT_SECONDS = 30.0
MCP_READY_POLL_SECONDS = 0.5
WORKER_HEARTBEAT_TIMEOUT_SECONDS = 60.0
WORKER_HEARTBEAT_POLL_SECONDS = 1.0
EMBEDDING_CONTRACT_KEYS = (
    "EMBEDDING_MODE",
    "EMBEDDING_MODEL",
    "EMBEDDING_REVISION",
)


def run(*args: str) -> str:
    # All callers pass fixed executable names and locally derived container IDs.
    return subprocess.run(  # noqa: S603  # nosec B603
        args, check=True, capture_output=True, text=True
    ).stdout


def compose_rows() -> list[dict[str, Any]]:
    output = run("docker", "compose", "ps", "--format", "json")
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def container_environment(container_id: str, *, runner: Callable[..., str] = run) -> dict[str, str]:
    raw = json.loads(runner("docker", "inspect", container_id))
    if not isinstance(raw, list) or len(raw) != 1:
        raise RuntimeError("container inspect is invalid")
    if not isinstance(raw[0], dict) or not isinstance(raw[0].get("Config"), dict):
        raise RuntimeError("container config is invalid")
    config = cast(dict[str, Any], raw[0]["Config"])
    values = config.get("Env")
    if not isinstance(values, list):
        raise RuntimeError("container environment is invalid")
    return {
        str(item).partition("=")[0]: str(item).partition("=")[2]
        for item in values
        if "=" in str(item)
    }


def verify_embedding_contract(
    worker_rows: list[dict[str, Any]], *, runner: Callable[..., str] = run
) -> None:
    bootstrap_ids = [
        item
        for item in runner("docker", "compose", "ps", "-a", "-q", "bootstrap-demo").splitlines()
        if item
    ]
    require(len(bootstrap_ids) == 1, "expected one bootstrap-demo container")
    environments = [
        container_environment(bootstrap_ids[0], runner=runner),
        *(container_environment(str(row["ID"]), runner=runner) for row in worker_rows),
    ]
    expected = {key: environments[0].get(key, "") for key in EMBEDDING_CONTRACT_KEYS}
    for key, value in expected.items():
        require(bool(value), f"bootstrap-demo missing {key}")
    for environment in environments[1:]:
        for key, value in expected.items():
            require(environment.get(key) == value, f"embedding contract mismatch: {key}")


def verify_active_index_contract(*, runner: Callable[..., str] = run) -> dict[str, str]:
    raw = runner(
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "bootstrap-demo",
        "python",
        "-m",
        "supportguard.rag.contract_check",
    )
    payload = json.loads(raw)
    require(isinstance(payload, dict), "active index contract response is invalid")
    require(payload.get("status") == "passed", "active index contract did not pass")
    require(bool(payload.get("index_version")), "active index version is missing")
    require(
        isinstance(payload.get("pipeline_fingerprint"), str)
        and len(str(payload["pipeline_fingerprint"])) == 64,
        "active index pipeline fingerprint is invalid",
    )
    return cast(dict[str, str], payload)


def wait_for_worker_mcp_children(
    worker_rows: list[dict[str, Any]],
    *,
    runner: Callable[..., str] = run,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    timeout_seconds: float = MCP_READY_TIMEOUT_SECONDS,
) -> None:
    deadline = clock() + timeout_seconds
    expected = len(worker_rows)
    while True:
        child_commands: list[str] = []
        for row in worker_rows:
            child_commands.extend(runner("docker", "top", row["ID"]).splitlines())
        read_count = sum("supportguard.mcp.read_server" in command for command in child_commands)
        action_count = sum(
            "supportguard.mcp.action_server" in command for command in child_commands
        )
        require(read_count <= expected, "worker owns duplicate read MCP children")
        require(action_count <= expected, "worker owns duplicate action MCP children")
        if read_count == expected and action_count == expected:
            return
        if clock() >= deadline:
            raise RuntimeError(
                "worker MCP children did not converge before timeout "
                f"(read={read_count}, action={action_count}, expected={expected})"
            )
        sleeper(MCP_READY_POLL_SECONDS)


def wait_for_worker_heartbeats(
    *,
    snapshot_loader: Callable[[], dict[str, Any]],
    expected: int,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    timeout_seconds: float = WORKER_HEARTBEAT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = clock() + timeout_seconds
    last_ready: object = None
    while True:
        snapshot = snapshot_loader()
        dependencies = snapshot.get("dependencies")
        if not isinstance(dependencies, dict):
            raise RuntimeError("dependency snapshot is invalid")
        postgres = dependencies.get("postgres")
        if not isinstance(postgres, dict):
            raise RuntimeError("postgres dependency snapshot is invalid")
        require(postgres.get("status") == "healthy", "postgres")
        workers = dependencies.get("workers")
        if not isinstance(workers, dict):
            raise RuntimeError("worker dependency snapshot is invalid")
        last_ready = workers.get("ready_instances")
        require(
            isinstance(last_ready, int) and not isinstance(last_ready, bool),
            "worker ready instance count is invalid",
        )
        if last_ready == expected:
            return snapshot
        if clock() >= deadline:
            raise RuntimeError(
                "worker heartbeats did not converge before timeout "
                f"(ready={last_ready}, expected={expected})"
            )
        sleeper(WORKER_HEARTBEAT_POLL_SECONDS)


def main() -> int:
    rows = compose_rows()
    running = [row for row in rows if row["State"] == "running"]
    counts = Counter(str(row["Service"]) for row in running)
    expected = {
        "postgres": 1,
        "redis": 1,
        "demo-temporal": 1,
        "api": 1,
        "dispatcher": 1,
        "reconciler": 1,
        "worker": 2,
        "frontend": 1,
    }
    for service, count in expected.items():
        require(
            counts[service] == count,
            f"expected {count} running {service}, got {counts[service]}",
        )
    for row in running:
        if row["Service"] in {"postgres", "redis", "api", "frontend"}:
            require(row["Health"] == "healthy", f"{row['Service']} is not healthy")

    worker_rows = [row for row in running if row["Service"] == "worker"]
    wait_for_worker_mcp_children(worker_rows)
    verify_embedding_contract(worker_rows)
    active_index = verify_active_index_contract()

    for row in running:
        if row["Service"] not in {"postgres", "redis"}:
            user = run("docker", "inspect", "--format", "{{.Config.User}}", row["ID"]).strip()
            require(user not in {"", "0", "root"}, f"{row['Service']} runs as root")

    secret_allowlist = {
        "DEEPSEEK_API_KEY": {"worker"},
        "APP_SECRET_KEY": {"api"},
        "INTERNAL_API_TOKEN": {"api"},
        "MCP_READ_DATABASE_URL": {"worker"},
        "MCP_ACTION_DATABASE_URL": {"worker"},
    }
    for row in running:
        environment = json.loads(run("docker", "inspect", row["ID"]))[0]["Config"]["Env"]
        names = {str(item).partition("=")[0] for item in environment}
        for variable, allowed in secret_allowlist.items():
            require(
                variable not in names or row["Service"] in allowed,
                f"{row['Service']} received forbidden secret variable {variable}",
            )

    token = os.getenv("INTERNAL_API_TOKEN", "compose-internal-health-token")
    api_host = os.getenv("API_HOST", "127.0.0.1")
    api_port = int(os.getenv("V125_API_PORT", os.getenv("API_HOST_PORT", "8000")))
    request = urllib.request.Request(
        f"http://{api_host}:{api_port}/internal/health/dependencies",
        headers={"X-Internal-Token": token},
    )

    def load_dependencies() -> dict[str, Any]:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request, timeout=5
        ) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("dependency response is invalid")
        return cast(dict[str, Any], payload)

    wait_for_worker_heartbeats(snapshot_loader=load_dependencies, expected=2)
    print(
        json.dumps(
            {
                "status": "passed",
                "services": expected,
                "mcp_children": 4,
                "embedding_contract": "cohesive",
                "knowledge_index_version": active_index["index_version"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"compose verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
