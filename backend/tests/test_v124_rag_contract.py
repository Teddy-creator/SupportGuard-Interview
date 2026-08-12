from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from current_predicate_facts import record_predicate_operands
from supportguard.agent.context import ContextAssembler, ContextBudget, ContextBudgetExceeded
from supportguard.agent.contracts import (
    AgentContractDrift,
    contract_manifest,
    prompt_text,
    runtime_provenance,
    validate_contract_bundle,
)
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.schemas import CandidateCitation, CandidateResponse
from supportguard.config import Settings
from supportguard.db.base import Base
from supportguard.db.models import ProviderRuntimeEvent
from supportguard.providers.deepseek import ProviderError
from supportguard.rag.citations import (
    CitationPublicationConflict,
    CitationPublicationValidator,
)
from supportguard.rag.context_projection import project_context_evidence
from supportguard.rag.embeddings import DeterministicEmbedding
from supportguard.rag.ingest import ingest_corpus
from supportguard.rag.repository import KnowledgeRepository
from supportguard.rag.service import RetrievalService
from supportguard.rag.types import (
    EligibilityEnvelope,
    RetrievalFilter,
    RetrievalScopeSnapshot,
    SourceLocatorV2,
)
from supportguard.runtime.worker import worker_runtime
from supportguard.tools.gateway import READ_TOOL_ARGUMENTS, native_read_tool_schemas


def _write_versioned_corpus(root: Path) -> Path:
    source_dir = root / "knowledge" / "source_docs"
    source_dir.mkdir(parents=True)
    (source_dir / "current.md").write_text(
        "# Quotas\n\n## Current\n\nThe concurrency-limit policy is forty.",
        encoding="utf-8",
    )
    (source_dir / "historical.md").write_text(
        "# Quotas\n\n## Historical\n\nThe concurrency-limit policy was twenty.",
        encoding="utf-8",
    )
    manifest = root / "knowledge" / "manifests" / "documents.json"
    manifest.parent.mkdir(parents=True)
    common = {
        "title": "Quota policy",
        "document_type": "official_policy",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "authority_level": 100,
        "applicable_plan": None,
        "applicable_region": None,
    }
    manifest.write_text(
        json.dumps(
            [
                {
                    **common,
                    "document_id": "quota-current",
                    "version": "2.0",
                    "status": "active",
                    "effective_at": "2026-01-01T00:00:00+00:00",
                    "source_path": "knowledge/source_docs/current.md",
                },
                {
                    **common,
                    "document_id": "quota-historical",
                    "version": "1.0",
                    "status": "deprecated",
                    "effective_at": "2025-01-01T00:00:00+00:00",
                    "source_path": "knowledge/source_docs/historical.md",
                },
            ]
        ),
        encoding="utf-8",
    )
    manifest.with_name("temporal-backfill.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "knowledge-temporal-backfill.v1",
                "entries": [
                    {
                        "document_id": "quota-current",
                        "document_family_key": "quota-policy",
                        "effective_from": "2026-01-01T00:00:00+00:00",
                        "effective_until": None,
                        "applicable_plan": None,
                        "applicable_region": None,
                    },
                    {
                        "document_id": "quota-historical",
                        "document_family_key": "quota-policy",
                        "effective_from": "2025-01-01T00:00:00+00:00",
                        "effective_until": "2026-01-01T00:00:00+00:00",
                        "applicable_plan": None,
                        "applicable_region": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _scope_snapshot() -> RetrievalScopeSnapshot:
    return RetrievalScopeSnapshot(
        tenant_id="tenant-test",
        customer_id="customer-test",
        subscription_id="subscription-test",
        subscription_version=1,
        plan="pro",
    )


@pytest.mark.asyncio
async def test_retrieval_trace_records_filter_selection_and_separate_compare_groups(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = _write_versioned_corpus(tmp_path)
    embedding = DeterministicEmbedding()
    await ingest_corpus(db_session, root=tmp_path, manifest_path=manifest, embedding=embedding)
    await db_session.commit()

    class CountingRepository(KnowledgeRepository):
        pin_count = 0

        async def pin_active_snapshot(self):
            self.pin_count += 1
            return await super().pin_active_snapshot()

    repository = CountingRepository(db_session)
    service = RetrievalService(repository, embedding)

    _, current, trace = await service.retrieve_with_trace(
        "concurrency-limit", scope_snapshot=_scope_snapshot()
    )
    assert current.chunks
    assert {item["filter_outcome"] for item in trace.pre_filter_candidates} == {
        "eligible",
        "excluded",
    }
    assert trace.selected_candidates
    assert any("status_filtered" in item["filter_reasons"] for item in trace.pre_filter_candidates)

    pin_count_before_compare = repository.pin_count
    _, compared, compared_trace = await service.retrieve_with_trace(
        "concurrency-limit",
        intent="compare",
        historical_version="v1.0",
        scope_snapshot=_scope_snapshot(),
    )
    assert compared.refusal_reason is None
    assert compared.conflict is False
    assert {item.chunk.evidence_group for item in compared.chunks} == {
        "current",
        "historical",
    }
    assert {item["group"] for item in compared_trace.evidence_groups} == {
        "current",
        "historical",
    }
    assert repository.pin_count == 2  # one current request plus one compare request
    assert all(
        {"filter", "selected_candidates", "omission_decisions", "citations"} <= set(group)
        for group in compared_trace.evidence_groups
    )
    assert len({group["filter"]["logical_time"] for group in compared_trace.evidence_groups}) == 1
    assert compared_trace.pipeline_contract["reranker"] == "disabled"
    assert compared_trace.pipeline_contract["snapshot_id"] == compared_trace.corpus_snapshot_id
    assert (
        compared_trace.pipeline_contract["group_isolation"]
        == "single-transaction-independent-lanes.v1"
    )
    assert (
        compared_trace.pipeline_contract["version_relation"]
        == "published-transition-not-conflict.v1"
    )
    compare_request_pin_delta = repository.pin_count - pin_count_before_compare
    compare_pin_count = repository.pin_count

    filter_calls: list[RetrievalFilter] = []

    async def record_filter(filters: RetrievalFilter) -> None:
        filter_calls.append(filters)

    _, discovered, discovered_trace = await RetrievalService(
        repository,
        embedding,
        on_filter_ready=record_filter,
    ).retrieve_with_trace(
        "concurrency-limit",
        intent="compare",
        scope_snapshot=_scope_snapshot(),
    )
    assert len(filter_calls) == 1
    assert filter_calls[0].intent == "compare"
    assert discovered.conflict is False
    assert discovered.refusal_reason is None
    assert {item.chunk.evidence_group for item in discovered.chunks} == {
        "current",
        "historical",
    }
    assert {group["filter"]["intent"] for group in discovered_trace.evidence_groups} == {
        "current",
        "historical",
    }
    historical_group = next(
        group for group in discovered_trace.evidence_groups if group["group"] == "historical"
    )
    assert historical_group["filter"]["version"] == "1.0"
    assert historical_group["filter"]["temporal_selector"] == {
        "mode": "version",
        "historical_version": "1.0",
        "claim_effective_time": None,
    }
    publication_filter = RetrievalFilter.model_validate(historical_group["filter"])
    allowed_statuses, publication_claim_time = (
        CitationPublicationValidator._publication_temporal_contract(  # noqa: SLF001
            group="historical",
            filters=publication_filter,
            trace_logical_time=publication_filter.logical_time,
            publication_checked_at=datetime.now(UTC),
        )
    )
    assert allowed_statuses == {"active", "deprecated"}
    assert publication_claim_time is None
    assert (
        historical_group["pipeline_contract"]["lane_selector"] == "published-transition-discovery"
    )
    assert discovered_trace.pipeline_contract["historical_discovery"]["selected_version"] == "1.0"
    retrieved_evidence = []
    citation_binding_map: dict[str, dict[str, str]] = {}
    for index, item in enumerate(discovered.chunks):
        payload = item.chunk.model_dump(mode="json")
        payload["supporting_span_eligible"] = True
        retrieved_evidence.append(payload)
        citation_binding_map[f"citation-{index}"] = {
            "chunk_id": item.chunk.chunk_id,
            "document_id": item.chunk.document_id,
            "version": item.chunk.version,
            "content_hash": item.chunk.content_hash,
            "locator_hash": item.chunk.source_locator.locator_hash,
        }
    canonical = AgentRuntimeServices._canonicalize_grounded_conflict_clarification(
        {
            "run_id": "run-retrieval-to-graph",
            "redacted_message": "两个已发布版本的限制不同，但我还没提供区域。",
            "agent_finish_reason": "needs_clarification",
            "classification": {
                "issue_type": "product_knowledge",
                "policy_boundary": "allowed",
            },
            "evidence_conflict": discovered.conflict,
            "evidence": retrieved_evidence,
            "citation_binding_map": citation_binding_map,
            "tool_observations": [
                {
                    "tool_name": "search_knowledge",
                    "status": "ok",
                    "run_id": "run-retrieval-to-graph",
                    "data": {
                        "conflict": discovered.conflict,
                        "refusal_reason": discovered.refusal_reason,
                    },
                }
            ],
        },
        CandidateResponse(
            answer="请补充部署区域。",
            action="answer",
            knowledge_chunk_ids=[],
            business_source_ids=[],
        ),
    )
    assert canonical.answer == "请补充部署区域。"
    assert canonical.knowledge_citations == []
    assert canonical.knowledge_chunk_ids == []

    _, before_boundary, before_trace = await service.retrieve_with_trace(
        "concurrency-limit",
        intent="historical",
        explicit_as_of=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        scope_snapshot=_scope_snapshot(),
    )
    assert {item.chunk.version for item in before_boundary.chunks} == {"1.0"}
    assert before_trace.filter_contract.temporal_selector.mode == "as_of"

    _, at_boundary, at_trace = await service.retrieve_with_trace(
        "concurrency-limit",
        intent="historical",
        explicit_as_of=datetime(2026, 1, 1, tzinfo=UTC),
        scope_snapshot=_scope_snapshot(),
    )
    assert {item.chunk.version for item in at_boundary.chunks} == {"2.0"}
    assert at_trace.filter_contract.temporal_selector.mode == "as_of"

    _, version_at_time, version_at_time_trace = await service.retrieve_with_trace(
        "concurrency-limit",
        intent="historical",
        historical_version="1.0",
        explicit_as_of=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        scope_snapshot=_scope_snapshot(),
    )
    assert {item.chunk.version for item in version_at_time.chunks} == {"1.0"}
    assert version_at_time_trace.filter_contract.temporal_selector.mode == "version_as_of"
    temporal_manifest = json.loads(
        manifest.with_name("temporal-backfill.v1.json").read_text(encoding="utf-8")
    )
    operands = {
        "before_boundary_versions": sorted({item.chunk.version for item in before_boundary.chunks}),
        "at_boundary_versions": sorted({item.chunk.version for item in at_boundary.chunks}),
        "version_at_time_versions": sorted({item.chunk.version for item in version_at_time.chunks}),
        "before_mode": before_trace.filter_contract.temporal_selector.mode,
        "at_mode": at_trace.filter_contract.temporal_selector.mode,
        "version_at_time_mode": version_at_time_trace.filter_contract.temporal_selector.mode,
        "compare_groups": sorted({item["group"] for item in compared_trace.evidence_groups}),
        "compare_group_logical_time_count": len(
            {item["filter"]["logical_time"] for item in compared_trace.evidence_groups}
        ),
        "compare_pin_count": compare_pin_count,
        "compare_request_pin_delta": compare_request_pin_delta,
        "temporal_manifest_schema": temporal_manifest["schema_version"],
        "temporal_manifest_entry_count": len(temporal_manifest["entries"]),
        "temporal_manifest_sha256": hashlib.sha256(
            json.dumps(temporal_manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    for predicate_id in (
        "historical_date_single_valid",
        "compare_single_snapshot_groups_independent",
        "temporal_backfill_manifest_exact",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-13",
            predicate_id=predicate_id,
            subject_kind="versioned_retrieval_runtime",
            operands=operands,
        )
    for predicate_id in ("compare_single_snapshot", "groups_persisted_replayable"):
        record_predicate_operands(
            requirement_id="C5-P0-13",
            predicate_id=predicate_id,
            subject_kind="versioned_retrieval_runtime",
            operands=operands,
        )
    record_predicate_operands(
        requirement_id="C4-P0-10c",
        predicate_id="c4_p0_10c",
        subject_kind="versioned_retrieval_runtime",
        operands=operands,
    )
    pipeline_operands = {
        "pipeline_fingerprint": trace.pipeline_fingerprint,
        "pipeline_contract": trace.pipeline_contract,
        "pipeline_fingerprint_recomputed": _content_hash(trace.pipeline_contract),
        "omitted_candidates": trace.omission_decisions,
        "omission_reasons": sorted(
            {
                item["reason"]
                for item in trace.omission_decisions
                if isinstance(item, dict) and "reason" in item
            }
        ),
    }
    for predicate_id in ("omission_reason_exact", "pipeline_fingerprint_complete"):
        record_predicate_operands(
            requirement_id="C5-P0-14",
            predicate_id=predicate_id,
            subject_kind="rag_pipeline_trace_contract",
            operands=pipeline_operands,
        )


@pytest.mark.asyncio
async def test_v158_compare_without_historical_group_fails_closed(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = _write_versioned_corpus(tmp_path)
    documents = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(
        json.dumps([item for item in documents if item["status"] == "active"]),
        encoding="utf-8",
    )
    temporal_path = manifest.with_name("temporal-backfill.v1.json")
    temporal = json.loads(temporal_path.read_text(encoding="utf-8"))
    temporal["entries"] = [
        item for item in temporal["entries"] if item["document_id"] == "quota-current"
    ]
    temporal_path.write_text(json.dumps(temporal), encoding="utf-8")
    embedding = DeterministicEmbedding()
    await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=embedding,
    )
    await db_session.commit()

    _, evidence, trace = await RetrievalService(
        KnowledgeRepository(db_session), embedding
    ).retrieve_with_trace(
        "concurrency-limit",
        intent="compare",
        scope_snapshot=_scope_snapshot(),
    )

    assert evidence.conflict is False
    assert evidence.refusal_reason == "compare_evidence_group_missing"
    assert {item.chunk.evidence_group for item in evidence.chunks} == {"current"}
    assert {group["group"] for group in trace.evidence_groups} == {"current"}


@pytest.mark.asyncio
async def test_v158_compare_does_not_invent_conflict_for_same_published_identity(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = _write_versioned_corpus(tmp_path)
    documents = json.loads(manifest.read_text(encoding="utf-8"))
    historical = next(item for item in documents if item["status"] == "deprecated")
    historical["version"] = "2.0"
    historical["source_path"] = "knowledge/source_docs/current.md"
    manifest.write_text(json.dumps(documents), encoding="utf-8")
    embedding = DeterministicEmbedding()
    await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=embedding,
    )
    await db_session.commit()

    _, evidence, trace = await RetrievalService(
        KnowledgeRepository(db_session), embedding
    ).retrieve_with_trace(
        "concurrency-limit",
        intent="compare",
        historical_version="2.0",
        scope_snapshot=_scope_snapshot(),
    )

    assert {item.chunk.evidence_group for item in evidence.chunks} == {
        "current",
        "historical",
    }
    assert evidence.conflict is False
    assert evidence.refusal_reason is None
    assert trace.pipeline_contract["historical_discovery"]["selected_version"] == "2.0"


@pytest.mark.asyncio
async def test_retrieval_fails_closed_for_incompatible_embedding_contract(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = _write_versioned_corpus(tmp_path)
    await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=DeterministicEmbedding(),
    )
    await db_session.commit()

    class DifferentPooling(DeterministicEmbedding):
        pooling = "mean"

    service = RetrievalService(KnowledgeRepository(db_session), DifferentPooling())
    with pytest.raises(RuntimeError, match="incompatible_with_active_index") as contract_error:
        await service.retrieve_with_trace("concurrency", scope_snapshot=_scope_snapshot())
    record_predicate_operands(
        requirement_id="C4-P0-10e",
        predicate_id="c4_p0_10e",
        subject_kind="embedding_index_compatibility",
        operands={
            "ingest_pooling": DeterministicEmbedding.pooling,
            "query_pooling": DifferentPooling.pooling,
            "pooling_differs": DeterministicEmbedding.pooling != DifferentPooling.pooling,
            "contract_error": str(contract_error.value),
            "retrieval_count": 0,
        },
    )


def test_context_never_silently_drops_protected_current_evidence() -> None:
    assembler = ContextAssembler(
        ContextBudget(
            max_input_tokens=500,
            output_reserve=100,
            evidence_tokens=10,
            tool_tokens=100,
        )
    )
    with pytest.raises(ContextBudgetExceeded, match="protected context section") as budget_error:
        assembler.assemble(
            run_id="run",
            step_index=1,
            user_goal="help",
            trusted_task_state={"ticket_id": "ticket"},
            tools=[],
            latest_observations=[],
            evidence=[{"chunk_id": "chunk", "content": "x" * 200}],
            history=[],
            remaining_budget={"llm_calls": 1},
        )
    record_predicate_operands(
        requirement_id="C4-P0-10d",
        predicate_id="c4_p0_10d",
        subject_kind="protected_context_budget",
        operands={
            "max_input_tokens": assembler.budget.max_input_tokens,
            "evidence_tokens": assembler.budget.evidence_tokens,
            "protected_evidence_bytes": 200,
            "budget_error": str(budget_error.value),
            "silent_drop_count": 0,
        },
    )


def test_context_projection_keeps_decision_facts_without_audit_only_lineage() -> None:
    observations = [
        {
            "tool_name": name,
            "tool_call_id": f"call-{index}",
            "attempt_index": 1,
            "status": "ok",
            "retryable": False,
            "observed_at": "2026-07-14T00:00:00Z",
            "source_refs": [{"source_type": "business_record", "source_id": name}],
            "data": {"current_value": index},
            "invocation_id": f"invocation-{index}",
            "observation_id": f"observation-{index}",
            "observation_content_hash": "a" * 64,
            "turn_group_id": "turn-audit-only",
        }
        for index, name in enumerate(("search_knowledge", "query_account", "query_api_usage"))
    ]
    observations[0]["data"] = {
        "evidence": [
            {
                "evidence_id": "chunk-1:evidence",
                "chunk_id": "chunk-1",
                "supporting_span": "grounded",
            }
        ],
        "index_version": "index-1",
    }
    projected = [AgentRuntimeServices._project_context_observation(item) for item in observations]
    assert all("invocation_id" not in item for item in projected)
    assert projected[0]["data"] == {
        "index_version": "index-1",
        "evidence_ids": ["chunk-1:evidence"],
    }
    assembled = ContextAssembler().assemble(
        run_id="run",
        step_index=2,
        user_goal="diagnose",
        trusted_task_state={"ticket_id": "ticket"},
        tools=[],
        latest_observations=projected,
        evidence=[],
        history=[],
        remaining_budget={"llm_calls": 4, "tool_rounds": 1, "tool_attempts": 3},
    )
    assert not any(item["section"] == "latest_observations" for item in assembled.manifest.omitted)


def test_prompt_schema_and_actual_runtime_provenance_are_content_addressed() -> None:
    manifest = contract_manifest()
    assert manifest["prompt_version"] == "agent_decide.v5+bound_evidence_synthesis.v1"
    assert manifest["schema_version"] == "agent-contract.v5.1"
    assert all(len(str(manifest[key])) == 64 for key in ("prompt_hash", "schema_hash"))
    provenance = runtime_provenance(
        model="deterministic-fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context-v1.2",
        code_version="test-tree",
    )
    assert provenance["provider_mode"] == "fake"
    assert provenance["model"] == "deterministic-fake"
    assert provenance["prompt_hash"] == manifest["prompt_hash"]
    validate_contract_bundle()
    with pytest.raises(AgentContractDrift, match="citation_binding") as prompt_drift:
        validate_contract_bundle(
            prompt=prompt_text().replace(
                "containing only the evidence's `citation_binding_id`",
                "containing copied document metadata",
            )
        )
    mutated_tools = native_read_tool_schemas(set(READ_TOOL_ARGUMENTS))
    mutated_tools[0]["function"]["parameters"] = {"type": "object"}  # type: ignore[index]
    with pytest.raises(AgentContractDrift, match="tool_schema_drift") as tool_drift:
        validate_contract_bundle(read_tools=mutated_tools)
    record_predicate_operands(
        requirement_id="C4-P0-09a",
        predicate_id="c4_p0_09a",
        subject_kind="agent_contract_content_addressing",
        operands={
            "prompt_version": manifest["prompt_version"],
            "schema_version": manifest["schema_version"],
            "prompt_hash": manifest["prompt_hash"],
            "schema_hash": manifest["schema_hash"],
            "provenance_prompt_hash": provenance["prompt_hash"],
            "provider_mode": provenance["provider_mode"],
            "prompt_drift_error": str(prompt_drift.value),
            "tool_drift_error": str(tool_drift.value),
        },
    )


def test_candidate_citation_wire_identity_is_binding_id_only() -> None:
    schema = CandidateCitation.model_json_schema()
    assert set(schema["properties"]) == {"citation_binding_id"}
    assert schema["required"] == ["citation_binding_id"]
    with pytest.raises(ValueError):
        CandidateCitation.model_validate(
            {
                "citation_binding_id": "citation_fixture",
                "chunk_id": "model_must_not_repeat_runtime_identity",
            }
        )
    record_predicate_operands(
        requirement_id="C6-P0-11",
        predicate_id="citation_binding_wire_id_single",
        subject_kind="provider_citation_wire_schema",
        operands={
            "schema_properties": sorted(schema["properties"]),
            "required_properties": schema["required"],
            "extra_identity_rejected": True,
        },
    )


@pytest.mark.asyncio
async def test_provider_initialization_failure_is_durable_and_never_falls_back(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL") or (
        f"sqlite+aiosqlite:///{tmp_path / 'provider.db'}"
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    if database_url.startswith("sqlite"):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        app_env="development",
        demo_fake_provider=False,
        deepseek_api_key=None,
        service_instance_id="provider-failure-test",
        code_version="test-tree",
    )
    with pytest.raises(ProviderError, match="DEEPSEEK_API_KEY"):
        async with worker_runtime(settings):
            pytest.fail("provider initialization must fail closed")

    engine = create_async_engine(database_url)
    async with AsyncSession(engine) as session:
        event = await session.scalar(
            select(ProviderRuntimeEvent).where(
                ProviderRuntimeEvent.service_instance_id == "provider-failure-test"
            )
        )
        assert event is not None
        assert event.status == "initialization_failed"
        assert event.provider_mode == "production"
        assert event.error_code == "ProviderError"
        assert event.runtime_provenance["model"] == settings.llm_model
        record_predicate_operands(
            requirement_id="C4-P0-09b",
            predicate_id="c4_p0_09b",
            subject_kind="provider_initialization_fail_closed",
            operands={
                "event_status": event.status,
                "provider_mode": event.provider_mode,
                "error_code": event.error_code,
                "runtime_model": event.runtime_provenance["model"],
                "configured_model": settings.llm_model,
                "fake_provider_enabled": settings.demo_fake_provider,
            },
        )
    await engine.dispose()


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _CitationSession:
    def __init__(
        self,
        document: Any,
        chunk: Any,
        ingest: Any,
        claim: Any,
        context: Any,
        trace: Any,
        binding: Any,
        membership: Any,
        payload_binding_rows: list[tuple[Any, Any]] | None = None,
    ):
        self._source_rows = [(chunk, document, ingest)]
        self._claim = claim
        self._context = context
        self._trace = trace
        self._binding = binding
        self._membership = membership
        self._payload_binding_rows = payload_binding_rows or [(binding, membership)]
        self._invocation = SimpleNamespace(
            id="invocation-1",
            run_id="run-1",
            job_id="job-origin-1",
            segment_id="marker-origin-1",
            fencing_token=1,
        )
        self._observation = SimpleNamespace(
            id="observation-1",
            invocation_id="invocation-1",
            run_id="run-1",
            job_id="job-origin-1",
            segment_id="marker-origin-1",
            fencing_token=1,
            status="ok",
        )
        self._terminal_transport = SimpleNamespace(
            id="transport-terminal-1",
            tenant_id="tenant-test",
            run_id="run-1",
            job_id="job-executor-1",
            invocation_id="invocation-1",
            agent_call_attempt_id="read-attempt-1",
            fencing_token=2,
            status="succeeded",
        )
        self._terminal_attempt = SimpleNamespace(
            id="read-attempt-1",
            tenant_id="tenant-test",
            run_id="run-1",
            job_id="job-executor-1",
            fencing_token=2,
            logical_invocation_id="invocation-1",
            call_kind="read_mcp",
            status="succeeded",
        )
        self._execute_count = 0

    async def execute(self, _statement: Any) -> _Scalars:
        self._execute_count += 1
        rows = {
            1: [
                (
                    self._binding,
                    self._membership,
                    self._trace,
                    self._invocation,
                    self._observation,
                )
            ],
            2: self._payload_binding_rows,
            3: self._source_rows,
            4: [(self._claim, self._context)],
        }
        return _Scalars(rows[self._execute_count])

    async def scalars(self, _statement: Any) -> _Scalars:
        return _Scalars([self._trace])

    async def scalar(self, _statement: Any) -> Any:
        if "api_request_traces" in str(_statement):
            return None
        if "tool_transport_attempts" in str(_statement):
            return 1
        return datetime(2026, 7, 15, tzinfo=UTC)

    async def get(self, entity: Any, identity: str) -> Any:
        if entity.__name__ == "ToolTransportAttempt" and identity == "transport-terminal-1":
            return self._terminal_transport
        if entity.__name__ == "AgentCallAttempt" and identity == "read-attempt-1":
            return self._terminal_attempt
        if entity.__name__ == "ContextLedger" and identity == "context-1":
            return self._context
        if entity.__name__ == "Subscription" and identity == "subscription-test":
            return SimpleNamespace(
                id=identity,
                tenant_id="tenant-test",
                customer_id="customer-test",
                version=1,
                status="active",
                plan="pro",
            )
        return None


def _content_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@pytest.mark.asyncio
async def test_publication_revalidates_registered_claim_and_rejects_tampered_span() -> None:
    source = b"Refunds require an approved request."
    chunk_locator = SourceLocatorV2.build(
        document_key="refund-policy",
        document_internal_id="document-internal-1",
        document_version="2.0",
        source_bytes=source,
        corpus_snapshot_id="ingest-v2",
        index_version="index-v2",
        canonicalization_version="utf8-lf-nfc.v1",
        section_path="Refunds",
        byte_start=0,
        byte_end=len(source),
        chunker_fingerprint="c" * 64,
        embedding_fingerprint="e" * 64,
    )
    supporting = chunk_locator.subspan(parent_span=source, relative_start=0, relative_end=7)
    logical_time = datetime(2026, 7, 14, tzinfo=UTC)
    pipeline_contract = {
        "schema": "retrieval-pipeline.v2",
        "eligibility": "evidence-eligibility.v1",
    }
    filter_contract = {
        "intent": "current",
        "statuses": ["active"],
        "version": None,
        "minimum_authority": 50,
        "plan": "pro",
        "region": None,
        "effective_at": logical_time.isoformat(),
        "logical_time": logical_time.isoformat(),
        "index_version": "index-v2",
        "corpus_snapshot_id": "ingest-v2",
        "scope_snapshot": {
            "schema_version": "retrieval-scope-snapshot.v1",
            "tenant_id": "tenant-test",
            "customer_id": "customer-test",
            "subscription_id": "subscription-test",
            "subscription_version": 1,
            "plan": "pro",
            "region_trace_id": None,
            "region_trace_version": None,
            "region": None,
        },
        "eligibility_policy_version": "evidence-eligibility.v1",
        "pipeline_contract_hash": _content_hash(pipeline_contract),
        "schema_version": "filter-contract.v2",
        "temporal_selector": {
            "mode": "current",
            "claim_effective_time": logical_time.isoformat(),
        },
    }
    filter_contract = RetrievalFilter.model_validate(filter_contract).model_dump(mode="json")
    eligibility = EligibilityEnvelope(
        corpus_snapshot_id="ingest-v2",
        index_version="index-v2",
        document_internal_id="document-internal-1",
        chunk_id="chunk-refund",
        status="active",
        authority_level=100,
        applicable_plan=None,
        applicable_region=None,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        logical_time=logical_time,
        filter_hash=_content_hash(filter_contract),
        outcome="eligible",
        reason_code="eligible_hybrid_support",
    )
    evidence = [
        {
            "chunk_id": "chunk-refund",
            "document_id": "refund-policy",
            "version": "2.0",
            "index_version": "index-v2",
            "content_hash": hashlib.sha256(source).hexdigest(),
            "source_locator": supporting.model_dump(mode="json"),
            "chunk_locator": chunk_locator.model_dump(mode="json"),
            "eligibility_envelope": eligibility.model_dump(mode="json"),
            "evidence_group": "current",
        },
        {
            # A later bounded retrieval may return the same immutable chunk.
            # This payload occurrence is present in the complete context ledger
            # but is not the occurrence cited by the final claim.
            "chunk_id": "chunk-refund",
            "document_id": "background-policy",
            "version": "1.0",
            "index_version": "index-v2",
            "content_hash": "b" * 64,
            "supporting_span_eligible": False,
            "evidence_group": "current",
        },
    ]
    answer = "Refunds require approval."
    citation_binding_id = "citation-binding-1"
    claim = {
        "text": answer,
        "knowledge_locator_hashes": [supporting.locator_hash],
        "citation_binding_ids": [citation_binding_id],
        "observation_source_ids": [],
    }
    state = {
        "final": {
            "terminal_state": "resolved",
            "answer": answer,
            "knowledge_chunk_ids": ["chunk-refund"],
            "material_claims": [claim],
        },
        "evidence": evidence,
        "tool_observations": [],
    }
    record = SimpleNamespace(
        id="claim-1",
        claim_text=answer,
        answer_hash=hashlib.sha256(answer.encode()).hexdigest(),
        support_refs={
            "knowledge_locator_hashes": [supporting.locator_hash],
            "citation_binding_ids": [citation_binding_id],
            "observation_source_ids": [],
        },
        provider_attempt_id="provider-attempt-1",
        context_ledger_id="context-1",
    )
    document = SimpleNamespace(
        id="document-internal-1",
        document_key="refund-policy",
        version="2.0",
        ingest_run_id="ingest-v2",
        index_version="index-v2",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_until=None,
        authority_level=100,
        applicable_plan=None,
        applicable_region=None,
        status="active",
        content_hash=hashlib.sha256(source).hexdigest(),
        canonical_blob=source,
    )
    chunk = SimpleNamespace(
        chunk_key="chunk-refund",
        ingest_run_id="ingest-v2",
        index_version="index-v2",
        content_hash=hashlib.sha256(source).hexdigest(),
        locator_hash=chunk_locator.locator_hash,
        locator_schema_version="source-locator.v2",
        canonicalization_version="utf8-lf-nfc.v1",
        chunker_fingerprint="c" * 64,
        embedding_fingerprint="e" * 64,
    )
    ingest = SimpleNamespace(id="ingest-v2", index_version="index-v2", is_active=True)
    selected_candidate = {
        "chunk_id": "chunk-refund",
        "locator_hash": chunk_locator.locator_hash,
        "evidence_group": "current",
    }
    trace = SimpleNamespace(
        id="trace-1",
        logical_invocation_id="invocation-1",
        index_version="index-v2",
        corpus_snapshot_id="ingest-v2",
        selected_candidates=[selected_candidate],
        eligibility_envelopes=[eligibility.model_dump(mode="json")],
        evidence_groups=[
            {
                "group": "current",
                "filter": filter_contract,
                "selected_candidates": [selected_candidate],
            }
        ],
        filter_contract=filter_contract,
        temporal_selector=filter_contract["temporal_selector"],
        trace_logical_time=logical_time,
        origin_kind="agent_read_tool",
        trace_schema_version="retrieval-trace.v3",
        pipeline_contract=pipeline_contract,
        pipeline_fingerprint=_content_hash(pipeline_contract),
        run_id="run-1",
        job_id="job-origin-1",
        segment_id="marker-origin-1",
        fencing_token=1,
        origin_job_id="job-origin-1",
        origin_marker_id="marker-origin-1",
        origin_fencing_token=1,
        origin_segment_ref="marker-origin-1",
        terminal_transport_attempt_id="transport-terminal-1",
    )
    context = SimpleNamespace(
        id="context-1",
        run_id="run-1",
        component_manifest={
            "sections": [{"name": "retrieved_evidence", "content_hash": _content_hash(evidence)}]
        },
    )
    background_binding_id = "citation-binding-background"
    membership_inputs = [
        {
            "payload_ordinal": ordinal,
            "citation_binding_id": binding_id,
            "fragment_hash": _content_hash(
                project_context_evidence(evidence[ordinal], citation_binding_id=binding_id)
            ),
        }
        for ordinal, binding_id in enumerate((citation_binding_id, background_binding_id))
    ]
    membership_root = _content_hash(membership_inputs)
    membership = SimpleNamespace(
        id="membership-1",
        logical_invocation_id="invocation-1",
        origin_job_id="job-origin-1",
        origin_marker_id="marker-origin-1",
        origin_fencing_token=1,
        origin_segment_ref="marker-origin-1",
        payload_ordinal=0,
        payload_json_pointer="/retrieved_evidence/0",
        serialized_evidence_fragment_hash=_content_hash(
            project_context_evidence(evidence[0], citation_binding_id=citation_binding_id)
        ),
        ordered_membership_root_hash=membership_root,
    )
    binding = SimpleNamespace(
        id=citation_binding_id,
        retrieval_trace_id="trace-1",
        tool_invocation_id="invocation-1",
        observation_id="observation-1",
        selected_candidate_ordinal=0,
        temporal_selector=filter_contract["temporal_selector"],
        tenant_id="tenant-test",
        origin_job_id="job-origin-1",
        locator_hash=supporting.locator_hash,
        provider_attempt_id="provider-attempt-1",
        context_ledger_id="context-1",
    )
    background_membership = SimpleNamespace(
        id="membership-background",
        payload_ordinal=1,
        serialized_evidence_fragment_hash=membership_inputs[1]["fragment_hash"],
        ordered_membership_root_hash=membership_root,
    )
    background_binding = SimpleNamespace(
        id=background_binding_id,
        provider_attempt_id="provider-attempt-1",
        context_ledger_id="context-1",
    )
    payload_binding_rows = [
        (binding, membership),
        (background_binding, background_membership),
    ]

    def citation_session() -> _CitationSession:
        return _CitationSession(
            document,
            chunk,
            ingest,
            record,
            context,
            trace,
            binding,
            membership,
            payload_binding_rows,
        )

    await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)

    trace.pipeline_contract["eligibility"] = "tampered-policy"
    with pytest.raises(
        CitationPublicationConflict, match="citation_binding_incomplete"
    ) as pipeline_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.pipeline_contract["eligibility"] = "evidence-eligibility.v1"

    trace.evidence_groups[0]["filter"]["minimum_authority"] = 51
    with pytest.raises(
        CitationPublicationConflict, match="eligibility_trace_mismatch"
    ) as filter_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.evidence_groups[0]["filter"]["minimum_authority"] = 50

    trace.eligibility_envelopes[0]["outcome"] = "ineligible"
    with pytest.raises(
        CitationPublicationConflict, match="publication_eligibility_changed"
    ) as ineligible_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.eligibility_envelopes[0]["outcome"] = "eligible"

    # A copied Graph-state group cannot turn a trace-authoritative current citation
    # into a historical one and thereby make a deprecated source publishable.
    state["evidence"][0]["evidence_group"] = "historical"
    state["evidence"][0]["eligibility_envelope"]["status"] = "deprecated"
    trace.eligibility_envelopes[0]["status"] = "deprecated"
    document.status = "deprecated"
    context.component_manifest["sections"][0]["content_hash"] = _content_hash(state["evidence"])
    with pytest.raises(
        CitationPublicationConflict, match="publication_context_membership_changed"
    ) as membership_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    state["evidence"][0]["evidence_group"] = "current"
    state["evidence"][0]["eligibility_envelope"]["status"] = "active"
    trace.eligibility_envelopes[0]["status"] = "active"
    document.status = "active"
    context.component_manifest["sections"][0]["content_hash"] = _content_hash(state["evidence"])

    binding.selected_candidate_ordinal = 1
    with pytest.raises(
        CitationPublicationConflict, match="citation_binding_incomplete"
    ) as ordinal_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    binding.selected_candidate_ordinal = 0

    tampered = json.loads(json.dumps(state))
    tampered["evidence"][0]["source_locator"]["span_hash"] = "0" * 64
    with pytest.raises(CitationPublicationConflict, match="span_hash_mismatch") as span_error:
        await CitationPublicationValidator(citation_session()).validate(
            run_id="run-1", state=tampered
        )

    binding.provider_attempt_id = "wrong-provider-attempt"
    with pytest.raises(
        CitationPublicationConflict, match="citation_binding_wrong_provider_attempt"
    ) as wrong_attempt_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    binding.provider_attempt_id = "provider-attempt-1"

    trace.terminal_transport_attempt_id = None
    with pytest.raises(
        CitationPublicationConflict, match="citation_binding_incomplete"
    ) as incomplete_trace_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.terminal_transport_attempt_id = "transport-terminal-1"

    binding_operands = {
        "provider_attempt_id": record.provider_attempt_id,
        "binding_provider_attempt_id": binding.provider_attempt_id,
        "context_provider_attempt_id": "provider-attempt-1",
        "claim_binding_ids": claim["citation_binding_ids"],
        "payload_binding_ids": [item.id for item, _ in payload_binding_rows],
        "membership_root": membership.ordered_membership_root_hash,
        "recomputed_membership_root": _content_hash(membership_inputs),
        "membership_ordinals": [item.payload_ordinal for _, item in payload_binding_rows],
        "wrong_attempt_error": str(wrong_attempt_error.value),
        "wrong_attempt_publication_count": 0,
        "incomplete_trace_error": str(incomplete_trace_error.value),
        "incomplete_trace_publication_count": 0,
        "trace_origin_kind": trace.origin_kind,
        "required_origin_kind": "agent_read_tool",
        "origin_job_id": trace.origin_job_id,
        "origin_marker_id": trace.origin_marker_id,
        "origin_fencing_token": trace.origin_fencing_token,
        "binding_choice": sorted(claim["citation_binding_ids"])[0],
        "expected_binding_choice": citation_binding_id,
    }
    for predicate_id in (
        "provider_attempt_payload_membership_exact",
        "provider_request_membership_root_exact",
        "claim_binding_same_provider_attempt",
        "wrong_attempt_binding_zero",
        "incomplete_trace_publication_zero",
        "agent_read_origin_only",
        "duplicate_locator_binding_deterministic",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-11",
            predicate_id=predicate_id,
            subject_kind="citation_attempt_membership_gate",
            operands=binding_operands,
        )

    trace.trace_schema_version = "retrieval-trace.v2"
    with pytest.raises(CitationPublicationConflict) as legacy_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.trace_schema_version = "retrieval-trace.v3"
    trace.origin_kind = "offline"
    with pytest.raises(CitationPublicationConflict) as offline_error:
        await CitationPublicationValidator(citation_session()).validate(run_id="run-1", state=state)
    trace.origin_kind = "agent_read_tool"
    publication_operands = {
        "selected_candidates": trace.selected_candidates,
        "selected_candidate_count": 1,
        "selected_candidate_ordinal": binding.selected_candidate_ordinal,
        "filter_hash": eligibility.filter_hash,
        "recomputed_filter_hash": _content_hash(filter_contract),
        "pipeline_fingerprint": trace.pipeline_fingerprint,
        "recomputed_pipeline_fingerprint": _content_hash(pipeline_contract),
        "scope_tenant_id": filter_contract["scope_snapshot"]["tenant_id"],
        "binding_tenant_id": binding.tenant_id,
        "origin_job_id": trace.origin_job_id,
        "membership_origin_job_id": membership.origin_job_id,
        "eligibility_policy_version": filter_contract["eligibility_policy_version"],
        "pipeline_eligibility_policy": pipeline_contract["eligibility"],
        "document_status": document.status,
        "eligibility_status": eligibility.status,
        "document_authority": document.authority_level,
        "eligibility_authority": eligibility.authority_level,
        "trace_evidence_group": trace.evidence_groups[0]["group"],
        "state_evidence_group": state["evidence"][0]["evidence_group"],
        "resolved_span_hash": hashlib.sha256(supporting.resolve(source)).hexdigest(),
        "locator_span_hash": supporting.span_hash,
        "pipeline_error": str(pipeline_error.value),
        "filter_error": str(filter_error.value),
        "ineligible_error": str(ineligible_error.value),
        "membership_error": str(membership_error.value),
        "ordinal_error": str(ordinal_error.value),
        "span_error": str(span_error.value),
        "invalid_publication_count": 0,
    }
    for predicate_id in (
        "selected_candidate_exact",
        "filter_hash_recomputed",
        "pipeline_contract_hash_bound",
        "filter_scope_origin_bound",
        "eligibility_policy_version_bound",
        "authority_status_version_revalidated",
        "evidence_group_revalidated",
        "locator_span_content_bound",
        "ineligible_publication_zero",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-12",
            predicate_id=predicate_id,
            subject_kind="citation_publication_revalidation",
            operands=publication_operands,
        )
    locator_operands = {
        "locator_schema": supporting.locator_schema,
        "document_internal_id": supporting.document_internal_id,
        "chunk_document_internal_id": chunk_locator.document_internal_id,
        "corpus_snapshot_id": supporting.corpus_snapshot_id,
        "chunk_snapshot_id": chunk_locator.corpus_snapshot_id,
        "resolved_span_hash": hashlib.sha256(supporting.resolve(source)).hexdigest(),
        "locator_span_hash": supporting.span_hash,
        "chunk_locator_hash": chunk_locator.locator_hash,
        "eligibility_locator_hash": selected_candidate["locator_hash"],
        "eligibility_outcome": eligibility.outcome,
    }
    for predicate_id in (
        "locator_v2_identity",
        "locator_span_resolves",
        "document_chunk_snapshot_fk",
        "eligibility_separate",
    ):
        record_predicate_operands(
            requirement_id="C5-P0-12",
            predicate_id=predicate_id,
            subject_kind="source_locator_v2_contract",
            operands=locator_operands,
        )
    record_predicate_operands(
        requirement_id="C6-P0-13",
        predicate_id="legacy_trace_audit_only",
        subject_kind="citation_publication_origin_gate",
        operands={
            "legacy_trace_schema": "retrieval-trace.v2",
            "required_trace_schema": "retrieval-trace.v3",
            "legacy_publication_error": str(legacy_error.value),
            "legacy_publication_count": 0,
        },
    )
    record_predicate_operands(
        requirement_id="C6-P0-11",
        predicate_id="offline_publication_zero",
        subject_kind="citation_publication_origin_gate",
        operands={
            "offline_origin_kind": "offline",
            "required_origin_kind": "agent_read_tool",
            "offline_publication_error": str(offline_error.value),
            "offline_publication_count": 0,
        },
    )
