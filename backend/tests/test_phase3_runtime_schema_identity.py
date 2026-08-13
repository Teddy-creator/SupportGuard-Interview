from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from supportguard import cli, runtime_health
from supportguard.db.reference_contract import (
    CURRENT_PRODUCT_DATABASE_HEAD,
    LEGACY_PRODUCT_DATABASE_HEAD,
)
from supportguard.services.heartbeats import (
    ServiceHeartbeatSnapshot,
    bind_heartbeat_to_rollout,
    heartbeat_wire_payload,
)
from supportguard.services.schema_rollout import (
    POST_CONTRACT_HEADS,
    schema_rollout_for_head,
    schema_rollout_for_revisions,
    schema_rollout_from_capability,
)


def test_interview_baseline_is_the_only_current_runtime_identity() -> None:
    assert CURRENT_PRODUCT_DATABASE_HEAD == "i203_demo_truthful_refund"
    assert LEGACY_PRODUCT_DATABASE_HEAD == POST_CONTRACT_HEADS[-1] == "b207c0a1d001"

    current = schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD)
    assert current.database_identity == "interview_baseline"
    assert current.current_writer_compatible is True
    assert current.reader_compatible is True
    assert current.serving_mode == "full"

    legacy = schema_rollout_for_head(LEGACY_PRODUCT_DATABASE_HEAD)
    assert legacy.database_identity == "legacy_final"
    assert legacy.current_writer_compatible is False
    assert legacy.reader_compatible is False
    assert legacy.serving_mode == "unavailable"


@pytest.mark.parametrize(
    "revisions",
    [[], [CURRENT_PRODUCT_DATABASE_HEAD, LEGACY_PRODUCT_DATABASE_HEAD], [None], "not-a-list"],
)
def test_missing_multiple_and_malformed_revision_sets_fail_closed(revisions: object) -> None:
    rollout = schema_rollout_for_revisions(revisions)
    assert rollout.database_identity == "unknown"
    assert rollout.serving_mode == "unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"migration_head": None},
        {"migration_head": [CURRENT_PRODUCT_DATABASE_HEAD]},
        {"migration_head": "unknown_revision"},
        {"migration_head": LEGACY_PRODUCT_DATABASE_HEAD},
    ],
)
def test_runtime_capability_rejects_non_baseline_identity(payload: object) -> None:
    rollout = schema_rollout_from_capability(payload)
    assert rollout.current_writer_compatible is False
    assert rollout.serving_mode == "unavailable"


def test_heartbeat_projects_derived_baseline_identity() -> None:
    bound = bind_heartbeat_to_rollout(
        ServiceHeartbeatSnapshot(status="ready", capabilities=("agent",)),
        schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD),
        service="worker",
    )
    payload = json.loads(heartbeat_wire_payload(bound))
    assert payload["migration_head"] == CURRENT_PRODUCT_DATABASE_HEAD
    assert payload["database_identity"] == "interview_baseline"
    assert "database_identity:interview_baseline" in payload["capabilities"]


def test_cli_exposes_only_the_current_interview_database_entrypoints() -> None:
    parser = cli.build_parser()
    baseline = parser.parse_args(["db", "baseline-upgrade"])
    baseline_roles = parser.parse_args(["db", "bootstrap-interview-roles"])
    assert baseline.db_command == "baseline-upgrade"
    assert baseline_roles.db_command == "bootstrap-interview-roles"
    with pytest.raises(SystemExit):
        parser.parse_args(["db", "upgrade"])
    with pytest.raises(SystemExit):
        parser.parse_args(["db", "bootstrap-roles"])

    called = False
    owner = ModuleType("supportguard.db.interview_baseline")

    def upgrade_interview_baseline() -> None:
        nonlocal called
        called = True

    owner.upgrade_interview_baseline = upgrade_interview_baseline  # type: ignore[attr-defined]
    previous = sys.modules.get(owner.__name__)
    sys.modules[owner.__name__] = owner
    try:
        cli.upgrade_interview_baseline_database()
    finally:
        if previous is None:
            sys.modules.pop(owner.__name__, None)
        else:
            sys.modules[owner.__name__] = previous
    assert called is True


@pytest.mark.asyncio
async def test_container_runtime_health_binds_database_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Connection:
        async def fetchval(self, query: str, *args: object, **kwargs: object) -> bool:
            observed["query"] = query
            observed["args"] = args
            observed["timeout"] = kwargs.get("timeout")
            return True

        async def close(self, **kwargs: object) -> None:
            observed["close_timeout"] = kwargs.get("timeout")

    async def connect(database_url: str, **kwargs: object) -> Connection:
        observed["database_url"] = database_url
        observed["connect_timeout"] = kwargs.get("timeout")
        return Connection()

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://runtime@database/supportguard")
    monkeypatch.setattr(runtime_health.asyncpg, "connect", connect)

    assert await runtime_health.healthy(instance_id="worker-1", service="worker") is True
    assert observed["args"] == (
        "worker-1",
        "worker",
        CURRENT_PRODUCT_DATABASE_HEAD,
    )
    assert "payload->>'migration_head'=$3" in str(observed["query"])
