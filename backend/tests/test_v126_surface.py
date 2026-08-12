from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.config import get_settings
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import ToolCallContext, UsageInput
from supportguard.db.models import (
    ApiUsageBucket,
    ApiUsageSnapshot,
    ServiceInstanceHeartbeat,
    Subscription,
)
from supportguard.main import create_app
from supportguard.services.business import BusinessService

ROOT = Path(__file__).resolve().parents[2]
REVIEW_PACKET_PATH = Path("docs/corrective-review-gate-v1.2.6.md")
EVIDENCE_PAYLOAD_PATH = Path(
    "evals/reports/evidence/v1.2.6/20260719T095339Z-c02a042c/evidence-payload-index.json"
)
SUBMISSION_INDEX_PATH = EVIDENCE_PAYLOAD_PATH.with_name("submission-index.json")
REVIEW_PACKET_SHA256 = "6c3ca4770ccafd12914f0918febc8774552c9abccec1bb5e29cdcf7f11bcf519"
EVIDENCE_PAYLOAD_SHA256 = "e3786557e88106f23fdbbbdd7ce69a1b0a1d30786f9e278a66c144d353eb1e40"
SUBMISSION_INDEX_SHA256 = "05f55555b5977aa198f84fb3cf428ca02d0c5956c7283ec5d879a977aa4d17e2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_path(root: Path, value: object, *, label: str) -> Path:
    assert isinstance(value, str), f"{label} path must be a string"
    relative = Path(value)
    assert not relative.is_absolute(), f"{label} path must be repository-relative"
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    assert resolved.is_relative_to(resolved_root), f"{label} path escapes repository root"
    return resolved


def _assert_review_packet_phase(
    root: Path,
    *,
    require_tracked: bool,
) -> dict[str, object]:
    review_packet = root / REVIEW_PACKET_PATH
    submission_indexes = sorted(
        (root / "evals/reports/evidence/v1.2.6").glob("*/submission-index.json")
    )
    if not submission_indexes:
        assert not review_packet.exists(), "pre-evidence review packet must be absent"
        return {"phase": "pre_evidence", "review_packet_exists": False}

    assert len(submission_indexes) == 1, "post-evidence submission index must be unique"
    submission_path = submission_indexes[0]
    assert submission_path.relative_to(root) == SUBMISSION_INDEX_PATH
    submission = json.loads(submission_path.read_text())
    assert set(submission) == {
        "evidence_payload_index",
        "review_packet",
        "schema_version",
        "tested_code_commit",
    }
    assert submission["schema_version"] == "supportguard.corrective.v1.2.6.submission-index.v1"
    tested_code_commit = submission["tested_code_commit"]
    assert isinstance(tested_code_commit, str)
    assert len(tested_code_commit) == 40
    assert all(character in "0123456789abcdef" for character in tested_code_commit)

    packet_entry = submission["review_packet"]
    payload_entry = submission["evidence_payload_index"]
    assert isinstance(packet_entry, dict)
    assert isinstance(payload_entry, dict)
    assert set(packet_entry) == {"path", "sha256"}
    assert set(payload_entry) == {"path", "sha256"}
    assert packet_entry["path"] == REVIEW_PACKET_PATH.as_posix()
    assert payload_entry["path"] == EVIDENCE_PAYLOAD_PATH.as_posix()

    packet_path = _repository_path(root, packet_entry["path"], label="review packet")
    payload_path = _repository_path(root, payload_entry["path"], label="evidence payload")
    assert packet_path == review_packet.resolve()
    assert payload_path.parent == submission_path.resolve().parent
    assert packet_path.is_file(), "post-evidence review packet must exist"
    assert payload_path.is_file(), "post-evidence payload index must exist"
    assert _sha256(packet_path) == packet_entry["sha256"], "review packet hash mismatch"
    assert _sha256(payload_path) == payload_entry["sha256"], "payload index hash mismatch"

    packet_text = packet_path.read_text()
    assert "human_review_status: pending" in packet_text
    assert str(payload_entry["sha256"]) in packet_text
    assert _sha256(submission_path) not in packet_text

    if require_tracked:
        tracked = subprocess.run(  # noqa: S603  # nosec B603
            [
                "/usr/bin/git",
                "ls-files",
                "--error-unmatch",
                packet_path.relative_to(root).as_posix(),
                payload_path.relative_to(root).as_posix(),
                submission_path.relative_to(root).as_posix(),
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert tracked.returncode == 0
        assert len(tracked.stdout.splitlines()) == 3

    return {
        "phase": "post_evidence",
        "review_packet_exists": True,
        "review_packet_sha256": _sha256(packet_path),
        "evidence_payload_sha256": _sha256(payload_path),
        "submission_index_sha256": _sha256(submission_path),
    }


@pytest.mark.asyncio
async def test_usage_windows_use_distinct_complete_minute_buckets_and_frozen_bounds(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_business_facts(db_session)
    logical_time = datetime.now(UTC).replace(second=37, microsecond=123456)
    window_end = logical_time.replace(second=0, microsecond=0)
    await db_session.execute(delete(ApiUsageBucket))
    db_session.add_all(
        [
            ApiUsageBucket(
                id=f"usage_bucket_surface_{minute}",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                bucket_start=window_end - timedelta(minutes=minute + 1),
                bucket_end=window_end - timedelta(minutes=minute),
                request_count=minute + 1,
                input_token_count=(minute + 1) * 10,
                output_token_count=minute + 1,
                concurrency_peak=(minute % 50) + 1,
                concurrency_end=(minute % 40) + 1,
                source_version=1,
            )
            for minute in range(1440)
        ]
    )
    snapshot = await db_session.get(ApiUsageSnapshot, "usage_demo")
    subscription = await db_session.get(Subscription, "sub_demo")
    assert snapshot is not None and subscription is not None
    snapshot.observed_at = window_end
    subscription.updated_at = window_end
    await db_session.flush()
    monkeypatch.setattr("supportguard.services.business.utc_now", lambda: logical_time)
    service = BusinessService(
        db_session,
        test_capability=issue_test_runtime_capability(testing=True),
    )
    context = ToolCallContext.fixture(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        ticket_id="ticket_demo",
        run_id="run_demo",
        tool_call_id="usage-surface",
        trace_id="trace-usage-surface",
    )
    results = {
        window: await service.query_api_usage(context, UsageInput(window=window))
        for window in ("1m", "5m", "1h", "24h")
    }
    assert {result.window_end for result in results.values()} == {window_end}
    assert {
        key: int((value.window_end - value.window_start).total_seconds() / 60)
        for key, value in results.items()
    } == {"1m": 1, "5m": 5, "1h": 60, "24h": 1440}
    assert len({result.request_count for result in results.values()}) == 4
    assert all(result.freshness_status == "fresh" for result in results.values())
    operands = {
        "windows": sorted(results),
        "window_minutes": {
            key: int((value.window_end - value.window_start).total_seconds() / 60)
            for key, value in results.items()
        },
        "window_end_count": len({result.window_end for result in results.values()}),
        "request_count_distinct": len({result.request_count for result in results.values()}),
        "logical_second": logical_time.second,
        "logical_microsecond": logical_time.microsecond,
        "window_end_second": window_end.second,
        "window_end_microsecond": window_end.microsecond,
        "freshness_statuses": sorted({result.freshness_status for result in results.values()}),
    }
    for predicate_id in (
        "usage_window_schema_exact",
        "usage_window_applied",
        "usage_non_minute_boundary_exact",
        "usage_freshness_truthful",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-16",
            predicate_id=predicate_id,
            subject_kind="usage_window_runtime",
            operands=operands,
        )


@pytest.mark.asyncio
async def test_account_surface_comes_only_from_customer_authority(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    service = BusinessService(
        db_session,
        test_capability=issue_test_runtime_capability(testing=True),
    )
    result = await service.query_account(
        ToolCallContext.fixture(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            run_id="run_demo",
            tool_call_id="account-surface",
            trace_id="trace-account-surface",
        )
    )
    payload = result.model_dump(mode="json")
    assert result.source_refs[0].source_type == "business_record"
    assert result.source_refs[0].source_id == "customer:cust_demo"
    assert set(payload).isdisjoint(
        {
            "plan",
            "subscription_status",
            "balance",
            "remaining_balance",
            "rpm_limit",
            "concurrency_limit",
            "email",
            "display_name",
            "api_key",
        }
    )
    forbidden = {
        "plan",
        "subscription_status",
        "balance",
        "remaining_balance",
        "rpm_limit",
        "concurrency_limit",
        "email",
        "display_name",
        "api_key",
    }
    operands = {
        "source_type": result.source_refs[0].source_type,
        "source_id": result.source_refs[0].source_id,
        "forbidden_fields_present": sorted(set(payload) & forbidden),
        "payload_fields": sorted(payload),
    }
    for predicate_id in ("account_surface_disjoint", "account_fields_authoritative"):
        record_predicate_operands(
            requirement_id="C6-P0-16",
            predicate_id=predicate_id,
            subject_kind="authoritative_account_projection",
            operands=operands,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_heartbeat_and_readiness_share_active_timing_snapshot() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin = create_async_engine(database_url)
    api = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker = create_async_engine(
        make_url(database_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    instance_id = f"worker-surface-{uuid4().hex[:12]}"
    try:
        async with api.connect() as connection:
            before = await connection.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
        assert isinstance(before, dict)
        async with worker.begin() as connection:
            await connection.execute(
                text(
                    "SELECT supportguard_record_service_heartbeat("
                    ":instance_id,'worker','surface-test')"
                ),
                {"instance_id": instance_id},
            )
        async with api.connect() as connection:
            after = await connection.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
        assert isinstance(after, dict)
        assert after["ready_worker_count"] == before["ready_worker_count"] + 1
        async with admin.begin() as connection:
            stored = await connection.execute(
                text(
                    "SELECT timing_version,runtime_config_hash "
                    "FROM service_instance_heartbeats WHERE id=:instance_id"
                ),
                {"instance_id": instance_id},
            )
            timing_version, config_hash = stored.one()
            assert timing_version == after["timing_version"]
            assert config_hash == after["config_hash"]
            await connection.execute(
                update(ServiceInstanceHeartbeat)
                .where(ServiceInstanceHeartbeat.id == instance_id)
                .values(runtime_config_hash="0" * 64)
            )
        async with api.connect() as connection:
            mismatched = await connection.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
        assert isinstance(mismatched, dict)
        assert mismatched["ready_worker_count"] == before["ready_worker_count"]
    finally:
        async with admin.begin() as connection:
            await connection.execute(
                delete(ServiceInstanceHeartbeat).where(ServiceInstanceHeartbeat.id == instance_id)
            )
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_http_replay_and_conflict_precede_upgrade_fence_with_zero_new_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_API_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_API_REDIS_URL are required")
    suffix = uuid4().hex
    admin = create_async_engine(database_url)
    api_url = (
        make_url(database_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    fence_id = f"upgrade_http_{suffix}"
    key = f"http-fence-{suffix}"
    message = f"Fence replay contract {suffix}"
    monkeypatch.setenv("DATABASE_URL", api_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.setenv("TENANT_COMMANDS_PER_MINUTE", "10000")
    monkeypatch.setenv("PRINCIPAL_COMMANDS_PER_MINUTE", "10000")
    get_settings.cache_clear()
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE runtime_jobs SET created_at=clock_timestamp(),"
                    "available_at=clock_timestamp() "
                    "WHERE status IN ('queued','retry_wait','leased')"
                )
            )
        with TestClient(create_app(testing=False)) as client:
            login = client.post(
                "/api/demo-sessions",
                json={"role": "customer", "customer_id": "cust_demo"},
            )
            assert login.status_code == 200
            csrf = str(login.json()["csrf_token"])
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": key}
            accepted = client.post("/api/tickets", headers=headers, json={"message": message})
            assert accepted.status_code == 202
            original = accepted.json()
            async with admin.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO supportguard_control.upgrade_runs("
                        "id,database_uuid,source_revision,target_revision,phase,"
                        "actor_instance_ref,status_version) "
                        "SELECT :id,database_uuid,'surface-source','surface-target',"
                        "'quiescing','surface-test',1 "
                        "FROM supportguard_control.database_identity"
                    ),
                    {"id": fence_id},
                )
                before = (
                    int(await connection.scalar(text("SELECT count(*) FROM support_tickets"))),
                    int(await connection.scalar(text("SELECT count(*) FROM runtime_jobs"))),
                    int(await connection.scalar(text("SELECT count(*) FROM outbox_events"))),
                    int(await connection.scalar(text("SELECT count(*) FROM idempotency_requests"))),
                )
            replay = client.post("/api/tickets", headers=headers, json={"message": message})
            conflict = client.post(
                "/api/tickets", headers=headers, json={"message": f"{message} changed"}
            )
            rejected = client.post(
                "/api/tickets",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": f"{key}-new",
                },
                json={"message": "new acceptance during fence"},
            )
            assert replay.status_code == 202
            assert replay.json() == {**original, "reused": True}
            assert conflict.status_code == 409
            assert rejected.status_code == 503
            assert rejected.json() == {
                "public_code": "service_unavailable",
                "message": "服务暂时不可用，请稍后重试。",
                "retryable": True,
                "request_id": rejected.headers["X-Request-ID"],
            }
            async with admin.connect() as connection:
                after = (
                    int(await connection.scalar(text("SELECT count(*) FROM support_tickets"))),
                    int(await connection.scalar(text("SELECT count(*) FROM runtime_jobs"))),
                    int(await connection.scalar(text("SELECT count(*) FROM outbox_events"))),
                    int(await connection.scalar(text("SELECT count(*) FROM idempotency_requests"))),
                )
            assert after == before
            operands = {
                "accepted_status": accepted.status_code,
                "replay_status": replay.status_code,
                "replay_reused": replay.json().get("reused"),
                "conflict_status": conflict.status_code,
                "fenced_new_status": rejected.status_code,
                "fenced_public_code": rejected.json().get("public_code"),
                "row_counts_before": list(before),
                "row_counts_after": list(after),
                "response_contract": "product-problem.v1",
            }
            # v1.5.12 deliberately supersedes only the historical public error
            # wire shape.  Do not fabricate the removed v1.2.6 error_code or
            # schema_version operands for upgrade_fence_503_no_acceptance.
            # Replay ordering, conflict semantics, 503 fencing and zero writes
            # remain covered by the still-valid predicates below.
            for predicate_id in (
                "pre_fence_success_replay_during_fence_read_only",
                "fenced_existing_hash_conflict_409",
            ):
                record_predicate_operands(
                    requirement_id="C6-P0-17",
                    predicate_id=predicate_id,
                    subject_kind="fenced_http_acceptance",
                    operands=operands,
                )
    finally:
        async with admin.begin() as connection:
            await connection.execute(
                text("DELETE FROM supportguard_control.upgrade_runs WHERE id=:id"),
                {"id": fence_id},
            )
        await admin.dispose()
        get_settings.cache_clear()


def test_review_packet_phase_contract_accepts_both_phases_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    assert _assert_review_packet_phase(tmp_path, require_tracked=False) == {
        "phase": "pre_evidence",
        "review_packet_exists": False,
    }

    packet_path = tmp_path / REVIEW_PACKET_PATH
    payload_path = tmp_path / EVIDENCE_PAYLOAD_PATH
    submission_path = tmp_path / SUBMISSION_INDEX_PATH
    packet_path.parent.mkdir(parents=True)
    payload_path.parent.mkdir(parents=True)
    payload_path.write_text('{"schema_version":"fixture-payload.v1"}\n')
    payload_sha256 = _sha256(payload_path)
    packet_text = (
        "# Fixture Review Packet\n\n"
        "human_review_status: pending\n\n"
        f"Evidence Payload Index SHA256: `{payload_sha256}`\n"
    )
    packet_path.write_text(packet_text)
    submission = {
        "evidence_payload_index": {
            "path": EVIDENCE_PAYLOAD_PATH.as_posix(),
            "sha256": payload_sha256,
        },
        "review_packet": {
            "path": REVIEW_PACKET_PATH.as_posix(),
            "sha256": _sha256(packet_path),
        },
        "schema_version": "supportguard.corrective.v1.2.6.submission-index.v1",
        "tested_code_commit": "1" * 40,
    }
    submission_path.write_text(json.dumps(submission, indent=2, sort_keys=True) + "\n")

    post_evidence = _assert_review_packet_phase(tmp_path, require_tracked=False)
    assert post_evidence["phase"] == "post_evidence"
    packet_path.write_text(packet_text + "tampered\n")
    with pytest.raises(AssertionError, match="review packet hash mismatch"):
        _assert_review_packet_phase(tmp_path, require_tracked=False)

    packet_path.write_text(packet_text)
    submission_path.write_text(
        json.dumps(
            {
                **submission,
                "schema_version": "supportguard.corrective.v1.2.6.invalid",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    with pytest.raises(AssertionError):
        _assert_review_packet_phase(tmp_path, require_tracked=False)
