from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from supportguard.agent.contracts import (
    AgentContractDrift,
    canonical_runtime_manifest,
    contract_manifest,
    validate_candidate_code_version,
    validate_contract_bundle,
)
from supportguard.config import Settings
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.mcp.manager import MCPManager
from supportguard.memory.service import follow_up_questions
from supportguard.providers.base import StructuredProvider
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.rag.intent import canonical_document_version, resolve_retrieval_intent
from supportguard.runtime.worker import worker_heartbeat_snapshot
from supportguard.services.heartbeats import (
    ServiceHeartbeatSnapshot,
    bind_heartbeat_to_rollout,
    heartbeat_wire_payload,
)
from supportguard.services.runtime_queue import RuntimeWorker
from supportguard.services.runtime_timing import RuntimeTiming
from supportguard.services.schema_rollout import schema_rollout_for_head


def test_canonical_runtime_manifest_is_stable_and_uses_current_contract() -> None:
    settings = Settings(
        app_env="test",
        code_version="a" * 40,
        embedding_mode="deterministic-fixture",
        _env_file=None,
    )
    first = canonical_runtime_manifest(
        settings=settings,
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
    )
    second = deepcopy(first)

    assert first == second
    assert first.prompt_version == "agent_decide.v6+bound_evidence_synthesis.v1"
    assert first.schema_version == "agent-contract.v5.2"
    assert first.prompt_hash == contract_manifest()["prompt_hash"]
    assert len(first.schema_hash) == 64
    assert len(first.embedding_fingerprint) == 64
    assert len(first.content_hash) == 64


def test_runtime_manifest_hash_excludes_repository_commit_provenance() -> None:
    first = canonical_runtime_manifest(
        settings=Settings(
            app_env="test",
            code_version="a" * 40,
            embedding_mode="deterministic-fixture",
            _env_file=None,
        ),
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
    )
    second = canonical_runtime_manifest(
        settings=Settings(
            app_env="test",
            code_version="b" * 40,
            embedding_mode="deterministic-fixture",
            _env_file=None,
        ),
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
    )

    assert first.code_commit != second.code_commit
    assert first.content_hash == second.content_hash
    assert "code_commit" not in first.identity_dict()


def test_startup_fails_closed_on_prompt_schema_or_candidate_identity_drift() -> None:
    validate_contract_bundle()
    with pytest.raises(AgentContractDrift, match="citation_binding"):
        validate_contract_bundle(prompt="drifted prompt")
    with pytest.raises(AgentContractDrift, match="production_code_commit_unbound"):
        validate_candidate_code_version(
            Settings(app_env="production", code_version="development", _env_file=None)
        )


def test_historical_version_is_canonical_at_intent_boundary() -> None:
    assert canonical_document_version(" v2.2 ") == "2.2"
    intent = resolve_retrieval_intent("请只按历史 v2.2 文档回答")
    assert intent.intent == "historical"
    assert intent.historical_version == "2.2"


@pytest.mark.parametrize(
    ("terminal", "reason", "mode", "expected"),
    [
        ("resolved", "answered", "agent", []),
        ("rejected", "out_of_scope", "agent", []),
        ("failed", "provider_failed", "agent", ["retryable_failure"]),
        ("failed", "proposal_not_durable", "agent", ["retryable_failure"]),
        ("needs_clarification", "needs_clarification", "agent", ["clarification"]),
        (
            "manual_takeover",
            "manual_takeover",
            "human_queue",
            ["durable_human_queue"],
        ),
    ],
)
def test_memory_follow_up_semantics_never_invent_human_work(
    terminal: str,
    reason: str,
    mode: str,
    expected: list[str],
) -> None:
    assert (
        follow_up_questions(
            terminal_state=terminal,
            finish_reason=reason,
            automation_mode=mode,
        )
        == expected
    )


def test_heartbeat_wire_contract_is_bounded_and_carries_migration_identity() -> None:
    payload = json.loads(
        heartbeat_wire_payload(
            bind_heartbeat_to_rollout(
                ServiceHeartbeatSnapshot(
                    status="ready",
                    capabilities=("agent", "runtime_manifest:" + "a" * 64),
                ),
                schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD),
                service="worker",
            )
        )
    )

    assert payload["schema_version"] == "service-heartbeat.v2"
    assert payload["status"] == "ready"
    assert payload["migration_head"] == CURRENT_PRODUCT_DATABASE_HEAD
    assert payload["capabilities"][:2] == [
        "agent",
        "runtime_manifest:" + "a" * 64,
    ]
    assert f"database_head:{CURRENT_PRODUCT_DATABASE_HEAD}" in payload["capabilities"]
    assert "writer_contract:3:contract" in payload["capabilities"]

    with pytest.raises(ValueError, match="bounded"):
        heartbeat_wire_payload(
            ServiceHeartbeatSnapshot(
                status="ready",
                capabilities=tuple(f"capability:{index}" for index in range(25)),
            )
        )


def test_worker_readiness_is_derived_from_live_component_state() -> None:
    settings = Settings(app_env="test", code_version="a" * 40, _env_file=None)
    provider = cast(StructuredProvider, DeterministicFakeProvider())
    timing = RuntimeTiming.from_settings(settings)
    worker = cast(
        RuntimeWorker,
        SimpleNamespace(last_progress_at=datetime.now(UTC), timing=timing),
    )
    ready_server = {
        "state": "ready",
        "process": "running",
        "session": "ready",
        "schema": "verified",
        "schema_hash": "b" * 64,
        "generation": 1,
    }
    manager = cast(
        MCPManager,
        SimpleNamespace(health=lambda: {"read": ready_server, "action": ready_server}),
    )
    assert (
        worker_heartbeat_snapshot(
            settings=settings,
            provider=provider,
            manager=manager,
            worker=worker,
        ).status
        == "ready"
    )

    worker.last_progress_at = datetime.now(UTC) - timedelta(minutes=2)
    degraded = worker_heartbeat_snapshot(
        settings=settings,
        provider=provider,
        manager=manager,
        worker=worker,
    )
    assert degraded.status == "degraded"
    assert "redis_consumer:stale" in degraded.capabilities

    # A segment can legitimately exceed the consumer-progress horizon while
    # its fenced job heartbeat continues to commit every interval. That
    # heartbeat must keep readiness green instead of causing a restart/takeover.
    worker.last_lease_heartbeat_at = datetime.now(UTC)
    active = worker_heartbeat_snapshot(
        settings=settings,
        provider=provider,
        manager=manager,
        worker=worker,
    )
    assert active.status == "ready"
    assert "redis_consumer:recent" in active.capabilities
