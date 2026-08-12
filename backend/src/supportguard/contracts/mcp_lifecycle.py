from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Literal

from supportguard.contracts.process_identity import process_birth_identity

REGISTRY_SCHEMA = "v129-owned-session-registry.v1"
REGISTRY_ENV = "SUPPORTGUARD_V129_OWNED_SESSION_REGISTRY"
PARTITION_ENV = "SUPPORTGUARD_V129_PARTITION_ID"
PARTITION_LEADER_ENV = "SUPPORTGUARD_V129_PARTITION_LEADER_PID"
OWNER_NODE_ENV = "SUPPORTGUARD_MCP_OWNER_NODE"

LifecycleState = Literal["registered", "confirmed", "closed"]


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise RuntimeError("mcp_registry_record_malformed")
    return value


def _process_parents() -> dict[int, int]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        ["/bin/ps", "-axo", "pid=,ppid="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    parents: dict[int, int] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(field.isdigit() for field in fields):
            parents[int(fields[0])] = int(fields[1])
    return parents


def ancestry_hash(*, ancestor_pid: int, descendant_pid: int) -> str:
    """Bind a registered MCP child to the invocation-owned process ancestry."""

    parents = _process_parents()
    chain = [descendant_pid]
    current = descendant_pid
    while current != ancestor_pid and current > 1 and len(chain) < 128:
        current = parents.get(current, 0)
        chain.append(current)
    if current != ancestor_pid:
        raise RuntimeError("mcp_registry_ancestry_invalid")
    return hashlib.sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()


def _atomic_write_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise RuntimeError("append_only_artifact_collision") from exc
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_record(
    *,
    registry: Path,
    partition_id: str,
    state: LifecycleState,
    module: str,
    pid: int,
    partition_leader_pid: int,
    schema_hash: str | None,
    owner_node: str | None = None,
    discovery_count: int | None = None,
    call_count: int | None = None,
) -> dict[str, object]:
    """Append one registered or confirmed MCP lifecycle fact.

    The neutral contract writes observed facts only. Evidence readers own all
    cross-record adjudication and acceptance decisions.
    """

    if state not in {"registered", "confirmed", "closed"}:
        raise RuntimeError("mcp_registry_state_invalid")
    birth = process_birth_identity(pid)
    pgid = os.getpgid(pid)
    sid = os.getsid(pid)
    if pid <= 1 or pid != pgid or pid != sid:
        raise RuntimeError("mcp_registry_private_session_invalid")
    ancestry = ancestry_hash(
        ancestor_pid=partition_leader_pid,
        descendant_pid=pid,
    )
    payload: dict[str, object] = {
        "schema": REGISTRY_SCHEMA,
        "partition_id": partition_id,
        "state": state,
        "module": module,
        "schema_hash": schema_hash,
        "leader_pid": pid,
        "pgid": pgid,
        "sid": sid,
        "process_birth_identity": birth.payload(),
        "ancestry_hash": ancestry,
        "owner_node": owner_node,
        "discovery_count": discovery_count,
        "call_count": call_count,
    }
    birth_hash = hashlib.sha256(
        json.dumps(birth.payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _atomic_write_json(registry / f"{state}-{pid}-{birth_hash}.json", payload)
    return payload


def write_closed_record(
    *,
    registry: Path,
    confirmed: dict[str, object],
    discovery_count: int,
    call_count: int,
) -> None:
    """Append the terminal lifecycle fact using one confirmed identity."""

    payload = dict(confirmed)
    payload.update(
        {
            "state": "closed",
            "discovery_count": discovery_count,
            "call_count": call_count,
        }
    )
    birth = payload.get("process_birth_identity")
    if not isinstance(birth, dict):
        raise RuntimeError("mcp_registry_record_malformed")
    birth_hash = hashlib.sha256(
        json.dumps(birth, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    pid = _integer(payload.get("leader_pid"))
    _atomic_write_json(registry / f"closed-{pid}-{birth_hash}.json", payload)
