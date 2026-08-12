#!/usr/bin/env python3
"""Fail-closed static checks for build context and Compose secret distribution."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def compose_config() -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603
        ["docker", "compose", "config", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(json.loads(completed.stdout))


def main() -> int:
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())
    for required in (".env", ".env.*", "*.pem", "*.key", ".codex", ".claude"):
        require(required in ignored, f"build_context_missing_ignore:{required}")

    services = dict(compose_config().get("services", {}))
    env_names = {
        name: set(dict(service.get("environment", {}))) for name, service in services.items()
    }
    secret_matrix = {
        "DEEPSEEK_API_KEY": {"worker"},
        "APP_SECRET_KEY": {"api"},
        "INTERNAL_API_TOKEN": {"api"},
        "MCP_READ_DATABASE_URL": {"worker"},
        "MCP_ACTION_DATABASE_URL": {"worker"},
    }
    for variable, allowed in secret_matrix.items():
        present = {name for name, names in env_names.items() if variable in names}
        require(present <= allowed, f"secret_scope_violation:{variable}")
    require("env_file" not in (ROOT / "docker-compose.yml").read_text(), "compose_env_file")
    print(
        json.dumps(
            {
                "status": "passed",
                "checked_services": len(services),
                "secret_variables": len(secret_matrix),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
