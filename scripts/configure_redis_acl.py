#!/usr/bin/env python3
"""Apply the checked-in development Redis ACL to a fresh CI Redis service."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from redis.asyncio import Redis


def acl_commands(path: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        tokens = shlex.split(raw_line)
        if not tokens:
            continue
        if tokens[0] != "user" or len(tokens) < 3:
            raise RuntimeError("invalid Redis ACL fixture line")
        commands.append(tokens[1:])
    # Disabling the unauthenticated default user must be the final operation.
    return sorted(commands, key=lambda command: command[0] == "default")


async def main() -> None:
    redis = Redis.from_url(
        os.getenv("REDIS_ADMIN_URL", "redis://localhost:6379/0"),
        decode_responses=False,
    )
    commands = acl_commands(Path("ops/redis-users.acl"))
    try:
        for command in commands:
            await redis.execute_command("ACL", "SETUSER", *command)
    finally:
        await redis.aclose()
    print(f"configured {len(commands)} Redis ACL users")


if __name__ == "__main__":
    asyncio.run(main())
