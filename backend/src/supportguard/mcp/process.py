from __future__ import annotations

import os
from pathlib import Path


def register_managed_process() -> None:
    """Create a private process group and publish this exact MCP child PID."""
    pid_file = os.getenv("SUPPORTGUARD_MCP_PID_FILE")
    if not pid_file:
        return
    try:
        os.setsid()
    except OSError as exc:
        if os.getpgrp() != os.getpid():
            raise RuntimeError("MCP child could not establish a private process group") from exc
    path = Path(pid_file)
    path.write_text(str(os.getpid()), encoding="ascii")
