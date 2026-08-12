#!/usr/bin/env python3
from __future__ import annotations

import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable

MAX_ATTEMPTS = 3
TRANSIENT_MARKERS = (
    "sslerror",
    "unexpected_eof",
    "unexpected eof",
    "connection reset",
    "connection aborted",
    "max retries exceeded",
    "read timed out",
    "connect timeout",
    "temporary failure",
    "502 bad gateway",
    "503 service unavailable",
)


def run_audit(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        completed = runner(  # noqa: S603  # nosec B603
            [sys.executable, "-m", "pip_audit", "--skip-editable"],
            capture_output=True,
            text=True,
            check=False,
        )
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        if completed.returncode == 0:
            return 0
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        transient = any(marker in combined for marker in TRANSIENT_MARKERS)
        if not transient or attempt == MAX_ATTEMPTS:
            return completed.returncode
        print(
            f"pip-audit transient transport failure; retrying attempt {attempt + 1}/"
            f"{MAX_ATTEMPTS}",
            file=sys.stderr,
        )
        sleeper(float(attempt))
    raise RuntimeError("pip_audit_retry_state_unreachable")


if __name__ == "__main__":
    raise SystemExit(run_audit())
