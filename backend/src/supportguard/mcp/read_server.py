from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select
from sqlalchemy.engine import make_url

from supportguard.agent.contracts import CONTEXT_VERSION, runtime_provenance
from supportguard.config import get_settings
from supportguard.contracts.canonical_json import canonical_decimal_string, canonical_json_hash
from supportguard.contracts.context import McpCallContext, ReadMcpCallContext
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import (
    AccountResult,
    ApiKeyMetadataInput,
    ApiKeyMetadataResult,
    BillingRecordInput,
    BillingRecordResult,
    IncidentImpactInput,
    IncidentImpactResult,
    KnowledgeEvidence,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    NoArguments,
    RequestTraceInput,
    RequestTraceResult,
    ServiceStatusInput,
    ServiceStatusResult,
    SourceRef,
    SubscriptionResult,
    ToolCallContext,
    UsageInput,
    UsageResult,
)
from supportguard.db.models import (
    AgentRun,
    ApiRequestTrace,
    RetrievalTrace,
    Subscription,
    ToolInvocation,
    new_id,
)
from supportguard.db.session import (
    ScopedSessionFactory,
    create_engine,
    create_scoped_session_factory,
    create_session_factory,
)
from supportguard.mcp.process import register_managed_process
from supportguard.mcp.trusted import mcp_worker_context
from supportguard.rag.embeddings import EmbeddingProvider, build_embedding_provider
from supportguard.rag.intent import resolve_retrieval_intent
from supportguard.rag.query import normalize_query
from supportguard.rag.repository import (
    KnowledgeRepository,
    KnowledgeSnapshot,
    RestrictedKnowledgeRepository,
)
from supportguard.rag.service import RetrievalService
from supportguard.rag.spans import select_supporting_span
from supportguard.rag.types import RetrievalFilter, RetrievalScopeSnapshot
from supportguard.services.business import BusinessService
from supportguard.services.errors import DomainError, observation_status_for_error
from supportguard.services.schema_rollout import require_current_runtime_schema

_factory: ScopedSessionFactory | None = None
_embedding: EmbeddingProvider | None = None
_embedding_lock: asyncio.Lock | None = None
_MCP_CONTEXT_ADAPTER: TypeAdapter[McpCallContext] = TypeAdapter(McpCallContext)
_RESTRICTED_LOAD_SCOPE_LOGICAL_TIME = datetime(1970, 1, 1, tzinfo=UTC)


def _restricted_trace_id(logical_invocation_id: str) -> str:
    """Bind physical transport retries to one canonical RetrievalTrace."""

    digest = hashlib.sha256(logical_invocation_id.encode()).hexdigest()
    return f"retrieval_{digest[:54]}"


def _terminal_trace_payload(trace: RetrievalTrace) -> dict[str, object]:
    return {
        "trace_id": trace.id,
        "trace_status": trace.trace_status,
        "result_digest": trace.result_digest,
        "error_digest": trace.error_digest,
        "temporal_selector": trace.temporal_selector,
        "filter_contract": trace.filter_contract,
        "vector_candidates": trace.vector_candidates,
        "keyword_candidates": trace.keyword_candidates,
        "rrf_candidates": trace.rrf_candidates,
        "pre_filter_candidates": trace.pre_filter_candidates,
        "selected_candidates": trace.selected_candidates,
        "omission_decisions": trace.omission_decisions,
        "evidence_groups": trace.evidence_groups,
        "eligibility_envelopes": trace.eligibility_envelopes,
        "pipeline_contract": trace.pipeline_contract,
        "embedding_fingerprint": trace.embedding_fingerprint,
        "pipeline_fingerprint": trace.pipeline_fingerprint,
        "abstention_reason": trace.abstention_reason,
    }


@asynccontextmanager
async def server_lifespan(_: FastMCP) -> AsyncIterator[None]:
    global _factory, _embedding, _embedding_lock
    settings = get_settings()
    engine = create_engine(
        settings.model_copy(
            update={"database_url": settings.mcp_read_database_url or settings.database_url}
        )
    )
    try:
        await require_current_runtime_schema(
            create_session_factory(engine, settings=settings),
            service="read_mcp",
            current_metadata_fixture=settings.app_env == "test",
        )
        _factory = create_scoped_session_factory(engine)
        _embedding = None
        _embedding_lock = asyncio.Lock()
        yield
    finally:
        _factory = None
        _embedding = None
        _embedding_lock = None
        await engine.dispose()


async def _embedding_provider() -> EmbeddingProvider:
    """Load the real query model once, only when retrieval first needs it.

    Each worker owns an isolated stdio Read MCP process. Loading both model
    copies during a simultaneous two-worker startup creates an avoidable
    resource spike even when the runtime is only proving MCP discovery and
    readiness. The lock preserves one real provider instance per process while
    concurrent first searches share the same initialization.
    """

    global _embedding
    if _embedding is not None:
        return _embedding
    lock = _embedding_lock
    if lock is None:
        raise RuntimeError("embedding provider lifespan is not initialized")
    async with lock:
        if _embedding is None:
            _embedding = await asyncio.to_thread(build_embedding_provider, get_settings())
        return _embedding


mcp = FastMCP(
    "support-read-mcp",
    instructions="Read-only scoped AtlasCloud business facts. No write capabilities.",
    log_level="ERROR",
    lifespan=server_lifespan,
)


def _context(value: ToolCallContext) -> ToolCallContext:
    parsed = (
        _MCP_CONTEXT_ADAPTER.validate_python(value.mcp_context)
        if value.mcp_context is not None
        else None
    )
    if parsed is not None and not isinstance(parsed, ReadMcpCallContext):
        raise ValueError("read MCP requires a read call context")
    if parsed is None and get_settings().app_env != "test":
        raise ValueError("read MCP requires a typed call context")
    return value.model_copy(update={"mcp_context": parsed})


async def _invoke(
    method: str, context: ToolCallContext, arguments: object | None = None
) -> dict[str, object]:
    if _factory is None:
        raise RuntimeError("read MCP lifespan is not initialized")
    execution = mcp_worker_context(context, executor_service_principal="support-read-mcp")
    async with _factory.worker(execution) as session:
        try:
            settings = get_settings()
            database_url = settings.mcp_read_database_url or settings.database_url
            restricted_login = make_url(database_url).username == "supportguard_read_mcp"
            test_capability = (
                issue_test_runtime_capability(testing=True)
                if settings.app_env == "test" and not restricted_login
                else None
            )
            service = BusinessService(session, test_capability=test_capability)
            await service.consume_mcp_reservation(
                context,
                method=method,
                model_arguments=(
                    arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else {}
                ),
            )
            if test_capability is None:
                await session.commit()
            result: BaseModel
            sql_result_types: dict[str, type[BaseModel]] = {
                "query_account": AccountResult,
                "query_subscription": SubscriptionResult,
                "query_api_usage": UsageResult,
                "check_service_status": ServiceStatusResult,
                "query_billing_record": BillingRecordResult,
                "query_request_trace": RequestTraceResult,
                "query_api_key_metadata": ApiKeyMetadataResult,
                "query_incident_impact": IncidentImpactResult,
            }
            if restricted_login and method in sql_result_types:
                payload = await service.execute_mcp_tool(
                    context,
                    method=method,
                    model_arguments=(
                        arguments.model_dump(mode="json")
                        if isinstance(arguments, BaseModel)
                        else {}
                    ),
                )
                result = sql_result_types[method].model_validate(payload)
            elif method == "query_account":
                await service.assert_fenced_context(context)
                result = await service.query_account(context)
            elif method == "query_subscription":
                await service.assert_fenced_context(context)
                result = await service.query_subscription(context)
            elif method == "query_api_usage" and isinstance(arguments, UsageInput):
                await service.assert_fenced_context(context)
                result = await service.query_api_usage(context, arguments)
            elif method == "check_service_status" and isinstance(arguments, ServiceStatusInput):
                await service.assert_fenced_context(context)
                result = await service.check_service_status(context, arguments)
            elif method == "query_billing_record" and isinstance(arguments, BillingRecordInput):
                await service.assert_fenced_context(context)
                result = await service.query_billing_record(context, arguments)
            elif method == "query_request_trace" and isinstance(arguments, RequestTraceInput):
                await service.assert_fenced_context(context)
                result = await service.query_request_trace(context, arguments)
            elif method == "query_api_key_metadata" and isinstance(arguments, ApiKeyMetadataInput):
                await service.assert_fenced_context(context)
                result = await service.query_api_key_metadata(context, arguments)
            elif method == "query_incident_impact" and isinstance(arguments, IncidentImpactInput):
                await service.assert_fenced_context(context)
                result = await service.query_incident_impact(context, arguments)
            elif method == "search_knowledge" and isinstance(arguments, KnowledgeSearchInput):
                await service.assert_fenced_context(context)
                embedding = await _embedding_provider()
                model_arguments = arguments.model_dump(mode="json")
                mcp_context = context.mcp_context
                restricted_logical_time = (
                    _RESTRICTED_LOAD_SCOPE_LOGICAL_TIME
                    if isinstance(mcp_context, ReadMcpCallContext)
                    and mcp_context.trace_origin == "agent_read_tool"
                    else datetime.now(UTC)
                )
                restricted_trace_id = (
                    _restricted_trace_id(mcp_context.logical_invocation_id)
                    if isinstance(mcp_context, ReadMcpCallContext)
                    and mcp_context.trace_origin == "agent_read_tool"
                    else new_id("retrieval")
                )
                restricted_binding: dict[str, object] = {
                    "trace_id": restricted_trace_id,
                    "query_hash": hashlib.sha256(
                        normalize_query(arguments.query).normalized.encode()
                    ).hexdigest(),
                    "trace_logical_time": restricted_logical_time.isoformat(),
                }

                async def restricted_operation(
                    operation: str, payload: dict[str, object]
                ) -> dict[str, object]:
                    return await service.execute_mcp_tool(
                        context,
                        method="search_knowledge",
                        model_arguments=model_arguments,
                        execution_payload={
                            "operation": operation,
                            "binding": dict(restricted_binding),
                            **payload,
                        },
                    )

                restricted_scope = (
                    await restricted_operation("load_scope", {}) if restricted_login else None
                )
                if restricted_scope is not None:
                    subscription_payload = restricted_scope.get("subscription")
                    region_payload = restricted_scope.get("region_trace")
                    snapshot_payload = restricted_scope.get("snapshot")
                    run_payload = restricted_scope.get("run_provenance")
                    if not isinstance(subscription_payload, dict):
                        raise RuntimeError("trusted retrieval subscription scope is missing")
                    if not isinstance(snapshot_payload, dict) or not isinstance(run_payload, dict):
                        raise RuntimeError("trusted retrieval provenance is malformed")
                    typed_snapshot_payload = cast(dict[str, Any], snapshot_payload)
                    typed_run_payload = cast(dict[str, Any], run_payload)
                    raw_logical_time = restricted_scope.get("trace_logical_time")
                    if not isinstance(raw_logical_time, str):
                        raise RuntimeError("trusted retrieval logical time is missing")
                    restricted_logical_time = datetime.fromisoformat(raw_logical_time)
                    if restricted_logical_time.tzinfo is None:
                        raise RuntimeError("trusted retrieval logical time is not timezone-aware")
                    restricted_binding["trace_logical_time"] = restricted_logical_time.isoformat()
                    plan = str(subscription_payload["plan"])
                    region = (
                        str(region_payload["region"]) if isinstance(region_payload, dict) else None
                    )
                    subscription_id = str(subscription_payload["id"])
                    subscription_version = int(subscription_payload["version"])
                    region_trace_id = (
                        str(region_payload["id"]) if isinstance(region_payload, dict) else None
                    )
                    region_trace_version = (
                        int(region_payload["version"]) if isinstance(region_payload, dict) else None
                    )
                else:
                    subscription = await session.scalar(
                        select(Subscription).where(
                            Subscription.tenant_id == context.tenant_id,
                            Subscription.customer_id == context.customer_id,
                            Subscription.status == "active",
                        )
                    )
                    region_trace = await session.scalar(
                        select(ApiRequestTrace)
                        .where(
                            ApiRequestTrace.tenant_id == context.tenant_id,
                            ApiRequestTrace.customer_id == context.customer_id,
                        )
                        .order_by(ApiRequestTrace.observed_at.desc(), ApiRequestTrace.id.desc())
                        .limit(1)
                    )
                    if subscription is None:
                        raise RuntimeError("trusted retrieval subscription scope is missing")
                    plan = subscription.plan
                    region = region_trace.region if region_trace is not None else None
                    subscription_id = subscription.id
                    subscription_version = subscription.version
                    region_trace_id = region_trace.id if region_trace is not None else None
                    region_trace_version = (
                        region_trace.version if region_trace is not None else None
                    )
                scope_snapshot = RetrievalScopeSnapshot(
                    tenant_id=context.tenant_id,
                    customer_id=context.customer_id,
                    subscription_id=subscription_id,
                    subscription_version=subscription_version,
                    plan=plan,
                    region_trace_id=region_trace_id,
                    region_trace_version=region_trace_version,
                    region=region,
                )
                intent_envelope = (
                    context.mcp_context.retrieval_intent
                    if isinstance(context.mcp_context, ReadMcpCallContext)
                    else None
                )
                if intent_envelope is None:
                    if get_settings().app_env != "test":
                        raise RuntimeError("search_knowledge requires trusted retrieval intent")
                    intent_envelope = resolve_retrieval_intent(arguments.query)
                settings = get_settings()
                trace_origin = (
                    mcp_context.trace_origin
                    if isinstance(mcp_context, ReadMcpCallContext)
                    else "agent_read_tool"
                )
                non_agent_origin = trace_origin != "agent_read_tool"
                invocation = None
                invocation_id = None
                if restricted_scope is not None:
                    raw_invocation_id = restricted_scope.get("invocation_internal_id")
                    invocation_id = (
                        str(raw_invocation_id) if raw_invocation_id is not None else None
                    )
                elif not non_agent_origin:
                    invocation = await session.scalar(
                        select(ToolInvocation).where(
                            ToolInvocation.tenant_id == context.tenant_id,
                            ToolInvocation.run_id == context.run_id,
                            ToolInvocation.job_id == context.job_id,
                            ToolInvocation.segment_id == context.segment_id,
                            ToolInvocation.provider_tool_call_id == context.tool_call_id,
                            ToolInvocation.logical_invocation_id
                            == (
                                mcp_context.logical_invocation_id
                                if isinstance(mcp_context, ReadMcpCallContext)
                                else ""
                            ),
                        )
                    )
                    invocation_id = invocation.id if invocation is not None else None
                fixture_origin = (
                    invocation_id is None
                    and settings.app_env == "test"
                    and trace_origin == "agent_read_tool"
                )
                if invocation_id is None and not fixture_origin and not non_agent_origin:
                    raise RuntimeError("retrieval logical invocation lineage is missing")
                if restricted_scope is None:
                    run = await session.get(AgentRun, context.run_id)
                    if run is None:
                        raise RuntimeError("retrieval run provenance is missing")
                    run_payload = {
                        "model": run.model,
                        "provider_mode": run.provider_mode,
                        "tool_call_mode": run.tool_call_mode,
                        "context_version": run.context_version,
                    }
                    typed_run_payload = run_payload
                logical_time = (
                    restricted_logical_time if restricted_scope is not None else datetime.now(UTC)
                )
                repository: KnowledgeRepository
                if restricted_scope is not None:
                    snapshot = KnowledgeSnapshot(
                        ingest_run_id=str(typed_snapshot_payload["ingest_run_id"]),
                        index_version=str(typed_snapshot_payload["index_version"]),
                        pipeline_fingerprint=str(typed_snapshot_payload["pipeline_fingerprint"]),
                        pipeline_identity=dict(
                            typed_snapshot_payload.get("pipeline_identity") or {}
                        ),
                    )
                    repository = RestrictedKnowledgeRepository(restricted_operation, snapshot)
                else:
                    repository = KnowledgeRepository(session)
                    snapshot = await repository.pin_active_snapshot()
                snapshot_index_version = snapshot.index_version
                snapshot_ingest_run_id = snapshot.ingest_run_id
                trace = RetrievalTrace(
                    id=(
                        restricted_trace_id if restricted_scope is not None else new_id("retrieval")
                    ),
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    job_id=(None if fixture_origin or non_agent_origin else context.job_id),
                    segment_id=(None if fixture_origin or non_agent_origin else context.segment_id),
                    origin_kind=("maintenance" if fixture_origin else trace_origin),
                    logical_invocation_id=(
                        invocation_id
                        if invocation_id is not None and not non_agent_origin
                        else None
                    ),
                    tool_call_id=(
                        None if fixture_origin or non_agent_origin else context.tool_call_id
                    ),
                    fencing_token=(
                        None if fixture_origin or non_agent_origin else context.fencing_token
                    ),
                    delivery_generation=(
                        None if fixture_origin or non_agent_origin else context.delivery_generation
                    ),
                    origin_job_id=(None if fixture_origin or non_agent_origin else context.job_id),
                    origin_marker_id=(
                        None if fixture_origin or non_agent_origin else context.segment_id
                    ),
                    origin_fencing_token=(
                        None if fixture_origin or non_agent_origin else context.fencing_token
                    ),
                    origin_segment_ref=(
                        None if fixture_origin or non_agent_origin else context.segment_id
                    ),
                    terminal_transport_attempt_id=None,
                    trace_status="started",
                    trace_logical_time=logical_time,
                    temporal_selector=intent_envelope.model_dump(mode="json"),
                    query_hash=hashlib.sha256(
                        normalize_query(arguments.query).normalized.encode()
                    ).hexdigest(),
                    filter_contract={
                        "request_intent": intent_envelope.intent,
                        "plan": plan,
                        "region": region,
                    },
                    vector_candidates=[],
                    keyword_candidates=[],
                    rrf_candidates=[],
                    pre_filter_candidates=[],
                    selected_candidates=[],
                    omission_decisions=[],
                    evidence_groups=[],
                    eligibility_envelopes=[],
                    pipeline_contract={"state": "started"},
                    embedding_fingerprint=None,
                    pipeline_fingerprint="0" * 64,
                    index_version=snapshot_index_version,
                    corpus_snapshot_id=snapshot_ingest_run_id,
                    abstention_reason=None,
                    runtime_provenance=runtime_provenance(
                        model=str(typed_run_payload["model"]),
                        provider_mode=str(typed_run_payload["provider_mode"]),
                        tool_call_mode=str(typed_run_payload["tool_call_mode"]),
                        context_version=CONTEXT_VERSION,
                        code_version=settings.code_version,
                        settings=settings,
                    ),
                )
                trace_id = trace.id
                if restricted_scope is None:
                    session.add(trace)
                    await session.flush()
                await session.commit()

                async def start_restricted_trace(filters: RetrievalFilter) -> None:
                    if restricted_scope is None:
                        return
                    trace.temporal_selector = filters.temporal_selector.model_dump(mode="json")
                    trace.filter_contract = filters.model_dump(mode="json")
                    trace.pipeline_fingerprint = filters.pipeline_contract_hash
                    restricted_binding.update(
                        {
                            "index_version": trace.index_version,
                            "corpus_snapshot_id": trace.corpus_snapshot_id,
                            "filter_hash": canonical_json_hash(trace.filter_contract),
                        }
                    )
                    await restricted_operation(
                        "trace_start",
                        {
                            "trace": {
                                "trace_id": trace.id,
                                "trace_logical_time": logical_time.isoformat(),
                                "temporal_selector": trace.temporal_selector,
                                "query_hash": trace.query_hash,
                                "filter_contract": trace.filter_contract,
                                "pipeline_contract": trace.pipeline_contract,
                                "pipeline_fingerprint": trace.pipeline_fingerprint,
                                "index_version": trace.index_version,
                                "corpus_snapshot_id": trace.corpus_snapshot_id,
                                "runtime_provenance": trace.runtime_provenance,
                            }
                        },
                    )

                try:
                    normalized, evidence, provenance = await RetrievalService(
                        repository,
                        embedding,
                        on_filter_ready=(
                            start_restricted_trace if restricted_scope is not None else None
                        ),
                    ).retrieve_with_trace(
                        arguments.query,
                        plan=plan,
                        region=region,
                        intent=intent_envelope.intent,
                        logical_time=logical_time,
                        explicit_as_of=intent_envelope.as_of,
                        historical_version=intent_envelope.historical_version,
                        index_version=snapshot_index_version,
                        scope_snapshot=scope_snapshot,
                    )
                except Exception as exc:
                    await session.rollback()
                    if restricted_scope is not None:
                        trace.trace_status = "terminal_error"
                        trace.error_digest = hashlib.sha256(type(exc).__name__.encode()).hexdigest()
                        await restricted_operation(
                            "trace_terminal", {"trace": _terminal_trace_payload(trace)}
                        )
                        await session.commit()
                    else:
                        failed_trace = await session.get(
                            RetrievalTrace, trace_id, with_for_update=True
                        )
                        if failed_trace is not None and failed_trace.trace_status == "started":
                            failed_trace.trace_status = "terminal_error"
                            failed_trace.error_digest = hashlib.sha256(
                                type(exc).__name__.encode()
                            ).hexdigest()
                            await session.commit()
                    raise
                if restricted_scope is None:
                    terminal_trace = await session.get(
                        RetrievalTrace, trace_id, with_for_update=True
                    )
                    if terminal_trace is None or terminal_trace.trace_status != "started":
                        raise RuntimeError("retrieval trace changed before terminalization")
                    trace = terminal_trace
                elif trace.trace_status != "started":
                    raise RuntimeError("retrieval trace changed before terminalization")
                trace.terminal_transport_attempt_id = (
                    mcp_context.transport_attempt_id
                    if isinstance(mcp_context, ReadMcpCallContext)
                    and not fixture_origin
                    and not non_agent_origin
                    else None
                )
                trace.trace_status = "terminal_ok"
                trace.temporal_selector = provenance.filter_contract.temporal_selector.model_dump(
                    mode="json"
                )
                trace.query_hash = provenance.query_hash
                trace.filter_contract = provenance.filter_contract.model_dump(mode="json")
                trace.vector_candidates = provenance.vector_candidates
                trace.keyword_candidates = provenance.keyword_candidates
                trace.rrf_candidates = provenance.rrf_candidates
                trace.pre_filter_candidates = provenance.pre_filter_candidates
                trace.selected_candidates = provenance.selected_candidates
                trace.omission_decisions = provenance.omission_decisions
                trace.evidence_groups = provenance.evidence_groups
                trace.eligibility_envelopes = provenance.eligibility_envelopes
                trace.pipeline_contract = provenance.pipeline_contract
                trace.embedding_fingerprint = provenance.embedding_fingerprint
                trace.pipeline_fingerprint = provenance.pipeline_fingerprint
                trace.abstention_reason = provenance.abstention_reason
                await service.assert_fenced_context(context)
                knowledge_evidence: list[KnowledgeEvidence] = []
                for item in evidence.chunks:
                    span = select_supporting_span(item.chunk, normalized.normalized)
                    if item.chunk.source_locator is None:
                        raise RuntimeError("selected evidence is missing its chunk locator")
                    if item.chunk.eligibility_envelope is None:
                        raise RuntimeError("selected evidence is missing eligibility provenance")
                    knowledge_evidence.append(
                        KnowledgeEvidence(
                            evidence_id=item.chunk.chunk_id,
                            document_id=item.chunk.document_id,
                            document_type=item.chunk.document_type,
                            chunk_id=item.chunk.chunk_id,
                            title=item.chunk.title,
                            section_path=item.chunk.section_path,
                            version=item.chunk.version,
                            effective_at=item.chunk.effective_at,
                            content_hash=item.chunk.content_hash,
                            source_locator=span.locator,
                            chunk_locator=item.chunk.source_locator,
                            eligibility_envelope=item.chunk.eligibility_envelope,
                            supporting_span=span.text,
                            supporting_span_eligible=span.material_claim_eligible,
                            supporting_span_reason=span.reason_code,
                            token_count=item.chunk.token_count,
                            retrieval_score=canonical_decimal_string(item.rrf_score),
                            evidence_group=item.chunk.evidence_group,
                        )
                    )
                result = KnowledgeSearchResult(
                    tool_call_id=context.tool_call_id,
                    ticket_id=context.ticket_id,
                    normalized_query=normalized.normalized,
                    evidence=knowledge_evidence,
                    conflict=evidence.conflict,
                    refusal_reason=evidence.refusal_reason,
                    index_version=provenance.index_version,
                    source_refs=[
                        SourceRef(
                            source_type="knowledge_chunk",
                            source_id=item.chunk.chunk_id,
                            observed_at=datetime.now(UTC),
                        )
                        for item in evidence.chunks
                    ],
                )
                trace.result_digest = hashlib.sha256(
                    json.dumps(
                        result.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                if restricted_scope is not None:
                    await restricted_operation(
                        "trace_terminal", {"trace": _terminal_trace_payload(trace)}
                    )
                else:
                    session.add(trace)
                    await session.flush()
                # Retrieval provenance is part of the durable tool result contract. The
                # scoped MCP transaction must commit it before the Observation returns.
                await session.commit()
            else:
                raise ValueError("unsupported read method")
        except DomainError as exc:
            error_payload: dict[str, object] = {
                "domain_error": True,
                "status": observation_status_for_error(exc.code),
                "error_code": exc.code.value,
                "safe_error_summary": exc.message,
            }
            boundary_reason = exc.details.get("boundary_reason")
            if boundary_reason and exc.details.get("sqlstate") in {
                "22023",
                "42501",
                "55000",
            }:
                error_payload["internal_boundary_reason"] = boundary_reason
            return error_payload
    return result.model_dump(mode="json")


@mcp.tool()
async def query_account(
    arguments: NoArguments,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return the trusted customer's account and subscription facts."""
    return await _invoke("query_account", _context(trusted_context))


@mcp.tool()
async def query_subscription(
    arguments: NoArguments,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return current subscription, entitlements, limits, and version."""
    return await _invoke("query_subscription", _context(trusted_context))


@mcp.tool()
async def query_api_usage(
    arguments: UsageInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return the trusted customer's latest API usage snapshot."""
    return await _invoke(
        "query_api_usage",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def check_service_status(
    arguments: ServiceStatusInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return current incident status for one model and region."""
    return await _invoke(
        "check_service_status",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def query_billing_record(
    arguments: BillingRecordInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return one opaque, current-customer billing record view."""
    return await _invoke(
        "query_billing_record",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def query_request_trace(
    arguments: RequestTraceInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return one tenant-scoped redacted request trace."""
    return await _invoke(
        "query_request_trace",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def query_api_key_metadata(
    arguments: ApiKeyMetadataInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return non-secret metadata for one scoped API Key."""
    return await _invoke(
        "query_api_key_metadata",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def query_incident_impact(
    arguments: IncidentImpactInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Return the scoped incident impact relation for one request."""
    return await _invoke(
        "query_incident_impact",
        _context(trusted_context),
        arguments,
    )


@mcp.tool()
async def search_knowledge(
    arguments: KnowledgeSearchInput,
    trusted_context: ToolCallContext,
) -> dict[str, object]:
    """Search active, versioned AtlasCloud knowledge and return grounded evidence."""
    return await _invoke(
        "search_knowledge",
        _context(trusted_context),
        arguments,
    )


def main() -> None:
    register_managed_process()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
