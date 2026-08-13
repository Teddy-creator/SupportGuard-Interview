from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.api.approval_projection import project_approval_detail
from supportguard.api.health import ReadinessSnapshot
from supportguard.api.reader_contract import approval_reader_identity
from supportguard.config import Settings
from supportguard.db.base import Base
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.main import create_app, lifespan
from supportguard.mcp import read_server
from supportguard.mcp.action_server import server_lifespan
from supportguard.runtime.worker import worker_runtime
from supportguard.services.heartbeats import (
    WRITER_BINARY_CONTRACT,
    ServiceHeartbeatSnapshot,
    bind_heartbeat_to_rollout,
    heartbeat_wire_payload,
    service_heartbeat,
)
from supportguard.services.schema_rollout import (
    BACKFILL_HEAD,
    CONTRACT_HEAD,
    EXPAND_HEAD,
    LEGACY_READER_HEAD,
    POST_CONTRACT_HEADS,
    RuntimeSchemaUnavailable,
    WriterContractUnavailable,
    inspect_schema_rollout,
    inspect_writer_schema_rollout,
    require_current_runtime_schema,
    schema_rollout_for_head,
)


class _DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _PostgresFixtureError(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        super().__init__("sanitized fixture database error")


def _dbapi_error(sqlstate: str) -> DBAPIError:
    return DBAPIError(
        statement=None,
        params=None,
        orig=_PostgresFixtureError(sqlstate),
    )


def test_schema_rollout_contract_accepts_only_the_interview_baseline() -> None:
    legacy_heads = (
        LEGACY_READER_HEAD,
        EXPAND_HEAD,
        BACKFILL_HEAD,
        CONTRACT_HEAD,
        *POST_CONTRACT_HEADS,
    )
    assert POST_CONTRACT_HEADS[-1] == "b207c0a1d001"
    assert CURRENT_PRODUCT_DATABASE_HEAD == "i204_action_terminal_order"
    for head in legacy_heads:
        snapshot = schema_rollout_for_head(head)
        assert snapshot.database_head == head
        assert snapshot.database_identity == (
            "legacy_final" if head == POST_CONTRACT_HEADS[-1] else "legacy_history"
        )
        assert snapshot.reader_compatible is False
        assert snapshot.writer_contract == "unsupported"
        assert snapshot.writer_contract_generation == -1
        assert snapshot.writer_enabled is False
        assert snapshot.current_writer_compatible is False
        assert snapshot.serving_mode == "unavailable"

    current = schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD)
    assert current.database_identity == "interview_baseline"
    assert current.reader_compatible is True
    assert current.writer_contract == "contract"
    assert current.writer_contract_generation == 3
    assert current.writer_enabled is True
    assert current.current_writer_compatible is True
    assert current.serving_mode == "full"

    unsupported = schema_rollout_for_head("b999-unknown")
    assert unsupported.reader_compatible is False
    assert unsupported.writer_enabled is False
    assert unsupported.current_writer_compatible is False
    assert unsupported.serving_mode == "unavailable"


@pytest.mark.asyncio
async def test_sqlite_current_metadata_fixture_reports_current_head_without_pg_probe(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/rollout.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            reader = await inspect_schema_rollout(session)
            writer = await inspect_writer_schema_rollout(
                session,
                service="worker",
            )
    finally:
        await engine.dispose()

    assert reader.database_head == CURRENT_PRODUCT_DATABASE_HEAD
    assert writer.database_head == CURRENT_PRODUCT_DATABASE_HEAD
    assert reader.current_writer_compatible is True
    assert writer.current_writer_compatible is True


@pytest.mark.asyncio
async def test_runtime_startup_fence_requires_explicit_sqlite_fixture_identity(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/startup-fence.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        with pytest.raises(RuntimeSchemaUnavailable, match="service=api"):
            await require_current_runtime_schema(factory, service="api")
        snapshot = await require_current_runtime_schema(
            factory,
            service="api",
            current_metadata_fixture=True,
        )
    finally:
        await engine.dispose()

    assert snapshot.database_head == CURRENT_PRODUCT_DATABASE_HEAD
    assert snapshot.current_writer_compatible is True


def test_approval_reader_identity_is_honest_across_schema_generations() -> None:
    legacy = approval_reader_identity(
        {
            "action_type": "refund",
            "proposal": {"resource_id": "bill_legacy"},
        }
    )
    assert legacy.model_dump() == {
        "resource_type": "billing_record_id",
        "resource_id": "bill_legacy",
        "origin_turn_id": None,
        "identity_source": "proposal_compat",
        "identity_complete": False,
    }

    persisted = approval_reader_identity(
        {
            "action_type": "refund",
            "resource_type": "billing_record_id",
            "resource_id": "bill_current",
            "origin_turn_id": "turn_current",
            "proposal": {"resource_id": "must_not_override"},
        }
    )
    assert persisted.model_dump() == {
        "resource_type": "billing_record_id",
        "resource_id": "bill_current",
        "origin_turn_id": "turn_current",
        "identity_source": "persisted",
        "identity_complete": True,
    }

    unavailable = approval_reader_identity(
        {
            "action_type": "refund",
            "action_payload": {"billing_record_id": "untrusted_payload_only"},
        }
    )
    assert unavailable.identity_source == "unavailable"
    assert unavailable.identity_complete is False
    assert unavailable.resource_id is None


def test_approval_reader_projection_never_prefers_action_payload_identity() -> None:
    projected = project_approval_detail(
        {
            "id": "approval_current",
            "ticket_id": "ticket_current",
            "action_type": "refund",
            "resource_type": "billing_record_id",
            "resource_id": "authoritative_resource",
            "origin_turn_id": "turn_current",
            "action_payload": {
                "billing_record_id": "untrusted_payload_resource",
                "refund_reason": "untrusted prose",
            },
            "resource_facts": {
                "status": "charged",
                "amount": "49.00",
                "currency": "USD",
                "version": 1,
            },
            "proposal_summary": {
                "status": "bound",
                "resource_version": 1,
            },
            "snapshot_summary": {
                "resource_version": 1,
                "policy_bound": True,
                "citation_count": 1,
            },
            "ticket_summary": {"status": "awaiting_approval"},
            "risk": "high",
            "business_version": 1,
            "status_version": 1,
            "status": "pending",
            "actionable": False,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )

    assert projected.resource_summary == "authoritative_resource"
    assert projected.resource_identity.identity_source == "persisted"
    assert projected.action_payload.billing_record_id == "authoritative_resource"


def test_approval_reader_projects_action_specific_review_diff() -> None:
    now = datetime.now(UTC)
    refund = project_approval_detail(
        {
            "id": "approval_refund",
            "ticket_id": "ticket_refund",
            "action_type": "refund",
            "resource_type": "billing_record_id",
            "resource_id": "bill_current",
            "origin_turn_id": "turn_refund",
            "action_payload": {
                "refund_reason": "Duplicate charge verified.",
            },
            "resource_facts": {
                "status": "charged",
                "amount": "49.00",
                "currency": "USD",
                "version": 2,
            },
            "proposal_summary": {
                "status": "bound",
                "resource_version": 2,
            },
            "snapshot_summary": {
                "resource_version": 2,
                "policy_bound": True,
                "citation_count": 1,
            },
            "review_context": {
                "tool_observations": [
                    {
                        "tool_name": "query_billing_record",
                        "data": {
                            "billing_record_id": "bill_current",
                            "status": "charged",
                        },
                    }
                ]
            },
            "ticket_summary": {"status": "awaiting_approval"},
            "risk": "high",
            "business_version": 2,
            "status_version": 1,
            "status": "pending",
            "actionable": True,
            "created_at": now,
            "updated_at": now,
        }
    )
    assert [item.model_dump() for item in refund.proposed_diff] == [
        {
            "field": "账单退款状态",
            "current": "charged",
            "proposed": "退款 49.00 USD",
        },
        {
            "field": "退款理由",
            "current": "无",
            "proposed": "按原始审批快照",
        },
    ]

    entitlement = project_approval_detail(
        {
            "id": "approval_entitlement",
            "ticket_id": "ticket_entitlement",
            "action_type": "entitlement_change",
            "resource_type": "subscription_id",
            "resource_id": "sub_current",
            "origin_turn_id": "turn_entitlement",
            "requested_change": {
                "change_type": "quota_change",
                "target": {
                    "concurrency_limit": 60,
                    "rpm_limit": 100,
                },
            },
            "resource_facts": {
                "status": "active",
                "plan": "pro",
                "concurrency_limit": 40,
                "rpm_limit": 50,
                "version": 4,
            },
            "proposal_summary": {
                "status": "bound",
                "resource_version": 4,
            },
            "snapshot_summary": {
                "resource_version": 4,
                "policy_bound": True,
                "citation_count": 1,
            },
            "ticket_summary": {"status": "awaiting_approval"},
            "risk": "high",
            "business_version": 4,
            "status_version": 1,
            "status": "pending",
            "actionable": True,
            "created_at": now,
            "updated_at": now,
        }
    )
    assert [item.model_dump() for item in entitlement.proposed_diff] == [
        {
            "field": "rpm_limit",
            "current": "50",
            "proposed": "100",
        },
        {
            "field": "concurrency_limit",
            "current": "40",
            "proposed": "60",
        },
    ]


def test_heartbeat_binds_actual_head_and_writer_contract_generation() -> None:
    rollout = schema_rollout_for_head(EXPAND_HEAD)
    effective = bind_heartbeat_to_rollout(
        ServiceHeartbeatSnapshot(
            status="ready",
            capabilities=("agent", f"migration_head:{CONTRACT_HEAD}"),
        ),
        rollout,
        service="worker",
    )
    payload = json.loads(heartbeat_wire_payload(effective))

    assert effective.status == "degraded"
    assert payload["migration_head"] == EXPAND_HEAD
    assert payload["database_head"] == EXPAND_HEAD
    assert payload["database_identity"] == "legacy_history"
    assert payload["writer_contract"] == "unsupported"
    assert payload["writer_contract_generation"] == -1
    assert f"database_head:{EXPAND_HEAD}" in payload["capabilities"]
    assert "writer_contract:-1:unsupported" in payload["capabilities"]
    assert f"writer_binary:{WRITER_BINARY_CONTRACT}" in payload["capabilities"]
    assert f"migration_head:{CONTRACT_HEAD}" not in payload["capabilities"]

    legacy_payload = json.loads(
        heartbeat_wire_payload(
            bind_heartbeat_to_rollout(
                ServiceHeartbeatSnapshot(
                    status="ready",
                    capabilities=("agent",),
                ),
                schema_rollout_for_head(LEGACY_READER_HEAD),
                service="worker",
            )
        )
    )
    assert f"writer_binary:{WRITER_BINARY_CONTRACT}" in legacy_payload["capabilities"]

    with pytest.raises(ValueError, match="bound"):
        heartbeat_wire_payload(
            ServiceHeartbeatSnapshot(
                status="ready",
                capabilities=("agent",),
            )
        )
    with pytest.raises(ValueError, match="inconsistent"):
        heartbeat_wire_payload(
            ServiceHeartbeatSnapshot(
                status="ready",
                capabilities=("agent",),
                writer_contract="legacy",
                writer_contract_generation=0,
            )
        )


@pytest.mark.parametrize(
    "database_head",
    [EXPAND_HEAD, CONTRACT_HEAD, POST_CONTRACT_HEADS[-2]],
)
def test_non_current_http_mutation_fails_closed_but_reader_and_session_bootstrap_work(
    monkeypatch: pytest.MonkeyPatch,
    database_head: str,
) -> None:
    async def non_current_rollout(_session: object) -> object:
        return schema_rollout_for_head(database_head)

    monkeypatch.setattr(
        "supportguard.main.inspect_schema_rollout",
        non_current_rollout,
    )
    with TestClient(create_app(testing=True)) as client:
        live = client.get("/api/health/live")
        session = client.post(
            "/api/demo-sessions",
            json={"role": "customer", "customer_id": "cust_demo"},
        )
        mutation = client.post(
            "/api/conversations",
            json={"message": "must remain read only during rollout"},
            headers={"Idempotency-Key": "reader-rollout-write-denied"},
        )

    assert live.status_code == 200
    assert session.status_code == 200
    assert mutation.status_code == 503
    assert mutation.json() == {
        "public_code": "service_unavailable",
        "message": "服务暂时不可用，请稍后重试。",
        "retryable": True,
        "request_id": mutation.headers["X-Request-ID"],
    }
    assert mutation.headers["X-Request-ID"].startswith("request_")


def test_schema_probe_failure_is_logged_and_fails_closed(
    monkeypatch,
    caplog,
) -> None:
    async def unavailable_rollout(_session):
        raise SQLAlchemyTimeoutError("fixture probe unavailable")

    monkeypatch.setattr(
        "supportguard.main.inspect_schema_rollout",
        unavailable_rollout,
    )
    with (
        caplog.at_level("WARNING", logger="supportguard.main"),
        TestClient(create_app(testing=True)) as client,
    ):
        mutation = client.post(
            "/api/conversations",
            json={"message": "must fail closed when the schema probe is unavailable"},
            headers={"Idempotency-Key": "reader-rollout-probe-unavailable"},
        )

    assert mutation.status_code == 503
    assert mutation.json() == {
        "public_code": "service_unavailable",
        "message": "服务暂时不可用，请稍后重试。",
        "retryable": True,
        "request_id": mutation.headers["X-Request-ID"],
    }
    records = [
        record for record in caplog.records if record.message == "schema_rollout_probe_unavailable"
    ]
    assert len(records) == 1
    assert records[0].error_type == "TimeoutError"
    assert records[0].request_method == "POST"
    assert "fixture probe unavailable" not in caplog.text


def test_schema_probe_contract_bug_is_not_misreported_as_an_upgrade(
    monkeypatch,
) -> None:
    async def invalid_probe_contract(_session):
        raise RuntimeError("fixture programmer error")

    monkeypatch.setattr(
        "supportguard.main.inspect_schema_rollout",
        invalid_probe_contract,
    )
    with TestClient(create_app(testing=True)) as client:
        mutation = client.post(
            "/api/conversations",
            json={"message": "must not label a contract bug as an upgrade"},
            headers={"Idempotency-Key": "reader-rollout-contract-bug"},
        )

    assert mutation.status_code == 500
    assert mutation.json()["public_code"] == "internal_error"
    assert set(mutation.json()) == {
        "public_code",
        "message",
        "retryable",
        "request_id",
    }


@pytest.mark.parametrize(
    ("probe_error", "expected_status", "expected_code", "expected_retryable"),
    [
        (_dbapi_error("57P03"), 503, "dependency_unavailable", True),
        (_dbapi_error("42501"), 503, "schema_probe_misconfigured", False),
        (_dbapi_error("22000"), 500, "internal_error", True),
        (_PostgresFixtureError("28P01"), 503, "schema_probe_misconfigured", False),
        (ConnectionRefusedError(), 503, "dependency_unavailable", True),
    ],
)
def test_schema_probe_failure_classification_is_honest(
    monkeypatch,
    probe_error: BaseException,
    expected_status: int,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    async def failed_probe(_session):
        raise probe_error

    monkeypatch.setattr(
        "supportguard.main.inspect_schema_rollout",
        failed_probe,
    )
    with TestClient(create_app(testing=True)) as client:
        mutation = client.post(
            "/api/conversations",
            json={"message": "classify the schema probe failure honestly"},
            headers={"Idempotency-Key": f"reader-rollout-{expected_code}"},
        )

    payload = mutation.json()
    assert mutation.status_code == expected_status
    assert payload["public_code"] == (
        "service_unavailable" if expected_status == 503 else "internal_error"
    )
    assert payload["retryable"] is expected_retryable
    if expected_status == 503:
        assert expected_code not in str(payload)


@pytest.mark.asyncio
async def test_background_heartbeat_failure_stops_the_owning_service(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/heartbeat.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    calls = 0

    def snapshot_provider() -> ServiceHeartbeatSnapshot:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("fixture heartbeat snapshot failed")
        return ServiceHeartbeatSnapshot(
            status="ready",
            capabilities=("agent",),
        )

    try:
        with pytest.raises(RuntimeError, match="fixture heartbeat snapshot failed"):
            async with service_heartbeat(
                factory,
                instance_id="heartbeat-owner-failure",
                service="worker",
                capabilities=["agent"],
                interval_seconds=0.01,
                snapshot_provider=snapshot_provider,
            ):
                await asyncio.sleep(1)
    finally:
        await engine.dispose()
    assert calls == 2


def test_compatible_reader_has_an_explicit_routable_readiness_mode(
    monkeypatch,
) -> None:
    snapshot = ReadinessSnapshot(
        status="compatible_read_only",
        snapshot_id="a" * 64,
        evaluated_at=datetime(2026, 7, 28, tzinfo=UTC),
        timing_version=1,
        dependencies={
            "migration": {
                "status": "compatible_read_only",
                "actual": LEGACY_READER_HEAD,
                "expected": CURRENT_PRODUCT_DATABASE_HEAD,
                "writer_contract_generation": 0,
                "writer_enabled": False,
            }
        },
    )

    async def compatible_snapshot(_request):
        return snapshot

    monkeypatch.setattr(
        "supportguard.api.readiness.evaluate_readiness",
        compatible_snapshot,
    )
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "read_only"}


@pytest.mark.asyncio
async def test_worker_fails_before_provider_mcp_or_consumer_initialization(
    monkeypatch,
) -> None:
    engine = _DisposableEngine()
    events: list[str] = []

    async def deny_writer(_factory, *, service):
        events.append(f"fence:{service}")
        raise WriterContractUnavailable(
            service="worker",
            snapshot=schema_rollout_for_head(EXPAND_HEAD),
        )

    def provider_must_not_start(*_args, **_kwargs):
        events.append("provider")
        raise AssertionError("provider initialized before the writer fence")

    monkeypatch.setattr("supportguard.runtime.worker.validate_contract_bundle", lambda: None)
    monkeypatch.setattr(
        "supportguard.runtime.worker.validate_candidate_code_version",
        lambda _settings: None,
    )
    monkeypatch.setattr("supportguard.runtime.worker.create_engine", lambda _settings: engine)
    monkeypatch.setattr(
        "supportguard.runtime.worker.create_session_factory",
        lambda _engine, *, settings: object(),
    )
    monkeypatch.setattr(
        "supportguard.runtime.worker.require_current_writer_contract",
        deny_writer,
    )
    monkeypatch.setattr("supportguard.runtime.worker.build_provider", provider_must_not_start)

    with pytest.raises(WriterContractUnavailable):
        async with worker_runtime(Settings(_env_file=None)):
            pytest.fail("pre-contract worker unexpectedly started")

    assert events == ["fence:worker"]
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_api_fails_before_redis_or_serving_on_noncurrent_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _DisposableEngine()
    events: list[str] = []
    app = FastAPI()
    app.state.testing = False

    async def deny_runtime(_factory, *, service, current_metadata_fixture=False):
        assert current_metadata_fixture is False
        events.append(f"fence:{service}")
        raise RuntimeSchemaUnavailable(
            service="api",
            snapshot=schema_rollout_for_head(POST_CONTRACT_HEADS[-1]),
        )

    def redis_must_not_start(*_args, **_kwargs):
        events.append("redis")
        raise AssertionError("Redis initialized before the API schema fence")

    monkeypatch.setattr("supportguard.main.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("supportguard.main.validate_contract_bundle", lambda: None)
    monkeypatch.setattr("supportguard.main.validate_candidate_code_version", lambda _settings: None)
    monkeypatch.setattr("supportguard.main.create_engine", lambda _settings: engine)
    monkeypatch.setattr(
        "supportguard.main.create_session_factory",
        lambda _engine, *, settings: object(),
    )
    monkeypatch.setattr(
        "supportguard.main.create_scoped_session_factory",
        lambda _engine, *, settings: object(),
    )
    monkeypatch.setattr("supportguard.main.build_oidc_authenticator", lambda _settings: object())
    monkeypatch.setattr("supportguard.main.require_current_runtime_schema", deny_runtime)
    monkeypatch.setattr("supportguard.main.Redis.from_url", redis_must_not_start)

    with pytest.raises(RuntimeSchemaUnavailable, match="service=api"):
        async with lifespan(app):
            pytest.fail("non-current API unexpectedly started serving")

    assert events == ["fence:api"]
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_independent_read_mcp_fails_before_tool_factory_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _DisposableEngine()
    events: list[str] = []

    async def deny_runtime(_factory, *, service, current_metadata_fixture=False):
        assert current_metadata_fixture is False
        events.append(f"fence:{service}")
        raise RuntimeSchemaUnavailable(
            service="read_mcp",
            snapshot=schema_rollout_for_head("unknown_revision"),
        )

    def scoped_factory_must_not_start(*_args, **_kwargs):
        events.append("scoped_factory")
        raise AssertionError("Read MCP tool factory initialized before schema fence")

    monkeypatch.setattr(
        read_server,
        "get_settings",
        lambda: Settings(_env_file=None),
    )
    monkeypatch.setattr(read_server, "create_engine", lambda _settings: engine)
    monkeypatch.setattr(
        read_server,
        "create_session_factory",
        lambda _engine, *, settings: object(),
    )
    monkeypatch.setattr(read_server, "require_current_runtime_schema", deny_runtime)
    monkeypatch.setattr(
        read_server,
        "create_scoped_session_factory",
        scoped_factory_must_not_start,
    )

    with pytest.raises(RuntimeSchemaUnavailable, match="service=read_mcp"):
        async with read_server.server_lifespan(None):  # type: ignore[arg-type]
            pytest.fail("non-current Read MCP unexpectedly accepted initialization")

    assert events == ["fence:read_mcp"]
    assert engine.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["dispatcher", "reconciler"])
async def test_cli_write_loops_fail_before_redis_initialization(
    monkeypatch,
    service: str,
) -> None:
    from supportguard.cli import run_runtime_service

    engine = _DisposableEngine()
    events: list[str] = []

    async def deny_writer(_factory, *, service):
        events.append(f"fence:{service}")
        raise WriterContractUnavailable(
            service=service,
            snapshot=schema_rollout_for_head(BACKFILL_HEAD),
        )

    def redis_must_not_start(*_args, **_kwargs):
        events.append("redis")
        raise AssertionError("Redis initialized before the writer fence")

    monkeypatch.setattr(
        "supportguard.cli.get_settings",
        lambda: Settings(_env_file=None),
    )
    monkeypatch.setattr("supportguard.cli.create_engine", lambda _settings: engine)
    monkeypatch.setattr(
        "supportguard.cli.create_session_factory",
        lambda _engine: object(),
    )
    monkeypatch.setattr(
        "supportguard.cli.require_current_writer_contract",
        deny_writer,
    )
    monkeypatch.setattr("supportguard.cli.Redis.from_url", redis_must_not_start)

    with pytest.raises(WriterContractUnavailable):
        await run_runtime_service(service)

    assert events == [f"fence:{service}"]
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_independent_action_mcp_fails_before_accepting_tools(
    monkeypatch,
) -> None:
    engine = _DisposableEngine()
    events: list[str] = []

    async def deny_writer(_factory, *, service):
        events.append(f"fence:{service}")
        raise WriterContractUnavailable(
            service="action_mcp",
            snapshot=schema_rollout_for_head(LEGACY_READER_HEAD),
        )

    monkeypatch.setattr(
        "supportguard.mcp.action_server.get_settings",
        lambda: Settings(_env_file=None),
    )
    monkeypatch.setattr(
        "supportguard.mcp.action_server.create_engine",
        lambda _settings: engine,
    )
    monkeypatch.setattr(
        "supportguard.mcp.action_server.create_session_factory",
        lambda _engine: object(),
    )
    monkeypatch.setattr(
        "supportguard.mcp.action_server.require_current_writer_contract",
        deny_writer,
    )

    with pytest.raises(WriterContractUnavailable):
        async with server_lifespan(None):  # type: ignore[arg-type]
            pytest.fail("pre-contract Action MCP unexpectedly started")

    assert events == ["fence:action_mcp"]
    assert engine.disposed is True
