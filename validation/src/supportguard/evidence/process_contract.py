from __future__ import annotations

import json
import os
import platform
import signal
import subprocess  # nosec B404
import time
from pathlib import Path

from supportguard.contracts.process_identity import (
    ProcessBirthIdentity as ProcessBirthIdentity,
)
from supportguard.contracts.process_identity import (
    identity_matches as identity_matches,
)
from supportguard.contracts.process_identity import (
    process_birth_identity as process_birth_identity,
)

__all__ = ["ProcessBirthIdentity", "identity_matches", "process_birth_identity"]

INNER_TERM_GRACE_SECONDS = 4
INNER_KILL_GRACE_SECONDS = 2
INNER_RECEIPT_FLUSH_SECONDS = 2
OUTER_TERM_GRACE_SECONDS = 12
OUTER_KILL_GRACE_SECONDS = 4
OUTER_RECEIPT_FLUSH_SECONDS = 2


def session_processes(sid: int) -> list[dict[str, int]]:
    if os.name != "posix" or sid <= 1:
        raise RuntimeError("owned_session_invalid")
    system = platform.system().lower()
    columns = "pid=,ppid=,pgid=,state=" if system == "darwin" else "pid=,ppid=,pgid=,sid=,state="
    completed = subprocess.run(  # noqa: S603  # nosec B603
        ["/bin/ps", "-axo", columns],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    result: list[dict[str, int]] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        expected_fields = 4 if system == "darwin" else 5
        if len(fields) != expected_fields or not all(
            field.lstrip("-").isdigit() for field in fields[:-1]
        ):
            continue
        pid, ppid, pgid = map(int, fields[:3])
        if fields[-1].startswith("Z"):
            # A zombie has already terminated and cannot retain resources or
            # receive a signal.  Its parent (or subreaper) owns final waitpid.
            continue
        if system == "darwin":
            try:
                observed_sid = os.getsid(pid)
            except ProcessLookupError:
                continue
        else:
            observed_sid = int(fields[3])
        if observed_sid == sid:
            result.append({"pid": pid, "ppid": ppid, "pgid": pgid, "sid": observed_sid})
    return result


def terminate_owned_session(
    *,
    leader: ProcessBirthIdentity,
    expected_pgid: int,
    expected_sid: int,
    term_grace_seconds: float,
    kill_grace_seconds: float,
) -> dict[str, object]:
    if (
        expected_pgid != leader.pid
        or expected_sid != leader.pid
        or expected_pgid <= 1
        or expected_pgid == os.getpgrp()
    ):
        raise RuntimeError("owned_session_authorization_failed")
    escalations: list[str] = []
    errors: list[str] = []

    def groups() -> list[int]:
        rows = session_processes(expected_sid)
        return sorted(
            {row["pgid"] for row in rows if row["pid"] != os.getpid() and row["pgid"] > 1}
        )

    for name, sig, grace in (
        ("SIGTERM", signal.SIGTERM, term_grace_seconds),
        ("SIGKILL", signal.SIGKILL, kill_grace_seconds),
    ):
        current = groups()
        if not current:
            break
        if identity_matches(leader) is False and expected_pgid in current:
            raise RuntimeError("owned_session_birth_identity_changed")
        for pgid in current:
            try:
                os.killpg(pgid, sig)
                escalations.append(f"{name}:{pgid}")
            except ProcessLookupError:
                continue
            except OSError as exc:
                errors.append(f"{name}:{pgid}:{type(exc).__name__}")
        deadline = time.monotonic() + grace
        while groups() and time.monotonic() < deadline:
            time.sleep(0.02)
    survivors = session_processes(expected_sid)
    return {
        "escalations": escalations,
        "errors": errors,
        "final_survivors": survivors,
    }


def atomic_write_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
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
