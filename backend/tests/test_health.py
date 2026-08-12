from datetime import UTC, datetime

from fastapi.testclient import TestClient

from current_predicate_facts import record_predicate_operands
from supportguard.api import health as health_compatibility
from supportguard.api import readiness
from supportguard.api.health import ReadinessSnapshot
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.main import create_app


def test_health_returns_versioned_service_status() -> None:
    with TestClient(create_app(testing=True)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "supportguard"
    assert payload["version"] == "0.1.0"
    assert payload["provider_mode"] == "fake"
    assert payload["provider_model"] == "deterministic-fake"
    assert payload["tool_call_mode"] == "native_fixture"
    assert payload["mcp"]["read"]["process"] == "in-process-test"
    assert payload["mcp"]["read"]["session"] == "test-owned"
    assert payload["mcp"]["read"]["schema"] == "test"
    assert payload["mcp"]["action"]["process"] == "in-process-test"
    assert payload["mcp"]["action"]["session"] == "test-owned"
    assert payload["mcp"]["action"]["schema"] == "test"


def test_health_compatibility_facade_reexports_the_readiness_owner() -> None:
    assert health_compatibility.HealthResponse is readiness.HealthResponse
    assert health_compatibility.ReadinessSnapshot is readiness.ReadinessSnapshot
    assert health_compatibility.evaluate_readiness is readiness.evaluate_readiness
    assert health_compatibility.require_internal_token is readiness.require_internal_token


def test_public_readiness_is_minimal_and_internal_auth_failures_are_indistinguishable() -> None:
    with TestClient(create_app(testing=True)) as client:
        public = client.get("/api/health/ready")
        missing = client.get("/internal/health/dependencies")
        wrong = client.get(
            "/internal/health/dependencies",
            headers={"X-Internal-Token": "wrong-token"},
        )
        internal = client.get(
            "/internal/health/dependencies",
            headers={"X-Internal-Token": "local-internal-health-token"},
        )

    assert public.status_code == 503
    assert public.json() == {"status": "unavailable"}
    assert "postgres" not in public.text
    assert missing.status_code == wrong.status_code == 404
    assert missing.content == wrong.content
    assert missing.content == b'{"detail":"Not Found"}'
    assert missing.headers["content-type"] == wrong.headers["content-type"]
    assert missing.headers["content-type"] == "application/json"
    assert internal.status_code == 200
    assert set(internal.json()) == {
        "status",
        "snapshot_id",
        "evaluated_at",
        "timing_version",
        "dependencies",
    }
    migration = internal.json()["dependencies"]["migration"]
    assert migration["actual"] == CURRENT_PRODUCT_DATABASE_HEAD
    assert migration["expected"] == CURRENT_PRODUCT_DATABASE_HEAD
    assert migration["head_source"] == "current_metadata_fixture"
    assert migration["postgresql_capabilities_verified"] is False
    assert migration["writer_enabled"] is True
    operands = {
        "public_status": public.status_code,
        "public_payload": public.json(),
        "public_dependency_name_count": sum(
            name in public.text for name in ("postgres", "redis", "worker", "mcp")
        ),
        "missing_internal_status": missing.status_code,
        "wrong_internal_status": wrong.status_code,
        "auth_failure_bodies_equal": missing.content == wrong.content,
        "auth_failure_content_type_equal": (
            missing.headers["content-type"] == wrong.headers["content-type"]
        ),
        "internal_status": internal.status_code,
        "internal_fields": sorted(internal.json()),
    }
    for predicate_id in (
        "public_readiness_shape_exact",
        "dependency_detail_internal_only",
        "internal_auth_failure_indistinguishable",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-17",
            predicate_id=predicate_id,
            subject_kind="readiness_http_boundary",
            operands=operands,
        )


def test_public_and_internal_readiness_project_the_same_snapshot(monkeypatch) -> None:
    snapshot = ReadinessSnapshot(
        status="healthy",
        snapshot_id="a" * 64,
        evaluated_at=datetime(2026, 7, 15, tzinfo=UTC),
        timing_version=7,
        dependencies={"postgres": {"status": "healthy"}},
    )

    async def fixed_snapshot(_request):
        return snapshot

    monkeypatch.setattr("supportguard.api.readiness.evaluate_readiness", fixed_snapshot)
    with TestClient(create_app(testing=True)) as client:
        public = client.get("/api/health/ready")
        internal = client.get(
            "/internal/health/dependencies",
            headers={"X-Internal-Token": "local-internal-health-token"},
        )

    assert public.status_code == 200
    assert public.json() == {"status": "ready"}
    assert internal.json() == snapshot.model_dump(mode="json")
    operands = {
        "public_status": public.status_code,
        "public_payload": public.json(),
        "internal_snapshot_id": internal.json()["snapshot_id"],
        "expected_snapshot_id": snapshot.snapshot_id,
        "internal_timing_version": internal.json()["timing_version"],
        "expected_timing_version": snapshot.timing_version,
        "evaluator_patch_target": "supportguard.api.readiness.evaluate_readiness",
    }
    for predicate_id in (
        "public_internal_readiness_consistent",
        "readiness_evaluator_single_source",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-17",
            predicate_id=predicate_id,
            subject_kind="readiness_snapshot_projection",
            operands=operands,
        )
