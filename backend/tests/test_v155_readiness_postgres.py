from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard import __version__
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD


def _role_url(base: str, role: str) -> str:
    return make_url(base).set(username=role, password=role).render_as_string(
        hide_password=False
    )


def _payload(*, status: str = "ready", migration_head: str) -> str:
    return json.dumps(
        {
            "schema_version": "service-heartbeat.v2",
            "version": __version__,
            "status": status,
            "capabilities": [
                "agent",
                "runtime_manifest:" + "a" * 64,
                "read_mcp:ready:1:" + "b" * 64,
                "action_mcp:ready:1:" + "c" * 64,
                f"migration_head:{migration_head}",
            ],
            "migration_head": migration_head,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_component_readiness_persists_typed_facts_and_rejects_head_drift() -> None:
    base = os.getenv("TEST_DATABASE_URL")
    if not base or not make_url(base).drivername.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL is not configured for PostgreSQL")
    worker = create_async_engine(
        os.getenv("TEST_WORKER_DATABASE_URL") or _role_url(base, "supportguard_worker")
    )
    api = create_async_engine(_role_url(base, "supportguard_api"))
    admin = create_async_engine(base)
    ready_id = f"worker-v155-ready-{uuid4().hex}"
    drift_id = f"worker-v155-drift-{uuid4().hex}"
    try:
        async with worker.begin() as connection:
            ready = await connection.scalar(
                text(
                    "SELECT supportguard_record_service_heartbeat("
                    ":instance_id,'worker',:payload)"
                ),
                {
                    "instance_id": ready_id,
                    "payload": _payload(migration_head=CURRENT_PRODUCT_DATABASE_HEAD),
                },
            )
            drift = await connection.scalar(
                text(
                    "SELECT supportguard_record_service_heartbeat("
                    ":instance_id,'worker',:payload)"
                ),
                {
                    "instance_id": drift_id,
                    "payload": _payload(migration_head="b000c0a1d000"),
                },
            )
            healthcheck = await connection.scalar(
                text(
                    "SELECT supportguard_record_service_heartbeat("
                    ":instance_id,'worker','__healthcheck__')"
                ),
                {"instance_id": ready_id},
            )
        assert isinstance(ready, dict) and ready["healthy"] is True
        assert ready["migration_head"] == CURRENT_PRODUCT_DATABASE_HEAD
        assert isinstance(drift, dict) and drift["healthy"] is False
        assert drift["status"] == "degraded"
        assert isinstance(healthcheck, dict) and healthcheck["healthy"] is True
        assert healthcheck["capabilities"] == ready["capabilities"]

        async with api.connect() as connection:
            snapshot = await connection.scalar(
                text("SELECT supportguard_api_runtime_snapshot()")
            )
        assert isinstance(snapshot, dict)
        assert snapshot["migration_head"] == CURRENT_PRODUCT_DATABASE_HEAD
        components = {
            item["instance_id"]: item for item in snapshot["worker_components"]
        }
        assert components[ready_id]["status"] == "ready"
        assert components[drift_id]["status"] == "degraded"
        assert "runtime_manifest:" + "a" * 64 in components[ready_id]["capabilities"]

        async with admin.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id,status,capabilities FROM service_instance_heartbeats "
                        "WHERE id IN (:ready_id,:drift_id) ORDER BY id"
                    ),
                    {"ready_id": ready_id, "drift_id": drift_id},
                )
            ).mappings()
            persisted = {str(row["id"]): row for row in rows}
        assert persisted[ready_id]["status"] == "ready"
        assert persisted[drift_id]["status"] == "degraded"
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()
