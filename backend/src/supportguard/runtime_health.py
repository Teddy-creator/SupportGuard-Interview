"""Lightweight database-backed runtime health probe for container checks."""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg  # type: ignore[import-untyped]

from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD


async def healthy(*, instance_id: str, service: str) -> bool:
    database_url = os.environ.get("DATABASE_URL", "").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    if not database_url:
        return False
    connection = await asyncpg.connect(database_url, timeout=3)
    try:
        result = await connection.fetchval(
            "SELECT coalesce((payload->>'healthy')::boolean,false) "
            "AND payload->>'migration_head'=$3 FROM (SELECT "
            "supportguard_record_service_heartbeat($1,$2,'__healthcheck__') AS payload) probe",
            instance_id,
            service,
            CURRENT_PRODUCT_DATABASE_HEAD,
            timeout=2,
        )
        return result is True
    finally:
        await connection.close(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--service", required=True)
    arguments = parser.parse_args()
    try:
        return (
            0
            if asyncio.run(healthy(instance_id=arguments.instance, service=arguments.service))
            else 1
        )
    except (OSError, TimeoutError, asyncpg.PostgresError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
