"""Explicit in-process Tool transport owned by one deterministic test app.

This module is capability-gated and must never be selected by production startup.
It intentionally does not initialize stdio MCP children or inspect process env.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from supportguard.contracts.canonical_json import canonical_decimal_string
from supportguard.contracts.context import ReadMcpCallContext, WorkerExecutionContext
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.contracts.tools import (
    ApiKeyMetadataInput,
    ApiKeyRevocationProposalInput,
    BillingRecordInput,
    EntitlementChangeProposalInput,
    IncidentImpactInput,
    KnowledgeEvidence,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    NoArguments,
    RefundProposalInput,
    RequestTraceInput,
    ServiceStatusInput,
    SourceRef,
    ToolCallContext,
    UsageInput,
)
from supportguard.db.models import ApiRequestTrace, Subscription
from supportguard.db.session import ScopedSessionFactory
from supportguard.mcp.runtime import MCPCallResult, ServerName
from supportguard.rag.embeddings import EmbeddingProvider
from supportguard.rag.intent import resolve_retrieval_intent
from supportguard.rag.repository import KnowledgeRepository
from supportguard.rag.service import RetrievalService
from supportguard.rag.spans import select_supporting_span
from supportguard.rag.types import RetrievalScopeSnapshot
from supportguard.services.business import BusinessService
from supportguard.services.errors import DomainError, observation_status_for_error


class InProcessTestToolTransport:
    """A test-owned semantic transport; not a fake production MCP process."""

    def __init__(
        self,
        factory: ScopedSessionFactory,
        embedding: EmbeddingProvider,
        capability: TestRuntimeCapability,
    ) -> None:
        self._factory = factory
        self._embedding = embedding
        self._capability = capability
        self.calls: list[tuple[ServerName, str]] = []

    def health(self) -> dict[str, dict[str, str]]:
        return {
            "read": {"process": "in-process-test", "session": "test-owned", "schema": "test"},
            "action": {
                "process": "in-process-test",
                "session": "test-owned",
                "schema": "test",
            },
        }

    @staticmethod
    def _execution(context: ToolCallContext) -> WorkerExecutionContext:
        return WorkerExecutionContext(
            tenant_id=context.tenant_id,
            actor_principal_id=context.customer_id,
            executor_service_principal="test-tool-transport",
            customer_id=context.customer_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            job_id=context.job_id,
            segment_id=context.segment_id,
            delivery_generation=context.delivery_generation,
            fencing_token=context.fencing_token,
            trace_id=context.trace_id,
            deadline=datetime.now(UTC) + timedelta(seconds=10),
        )

    async def call(
        self,
        server_name: ServerName,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reconnect_once: bool,
    ) -> MCPCallResult:
        del reconnect_once
        self.calls.append((server_name, tool_name))
        try:
            if server_name == "read":
                payload = await self._call_read(tool_name, arguments)
            else:
                payload = await self._call_action(tool_name, arguments)
        except DomainError as exc:
            payload = {
                "domain_error": True,
                "status": observation_status_for_error(exc.code),
                "error_code": exc.code.value,
                "safe_error_summary": exc.message,
            }
        return MCPCallResult(value=payload, attempts=1)

    async def rehandshake(
        self,
        server_name: ServerName,
        *,
        failed_generation: int | None = None,
    ) -> int:
        del failed_generation
        if server_name != "read":
            raise RuntimeError("test transport rehandshake is read-only")
        return 1

    async def _call_read(self, tool_name: str, transport: dict[str, Any]) -> dict[str, object]:
        context = ToolCallContext.model_validate(transport["trusted_context"])
        raw_arguments = transport.get("arguments", {})
        execution = self._execution(context)
        async with self._factory.worker(execution) as session:
            service = BusinessService(session, test_capability=self._capability)
            result: BaseModel
            if tool_name == "query_account":
                NoArguments.model_validate(raw_arguments)
                result = await service.query_account(context)
            elif tool_name == "query_subscription":
                NoArguments.model_validate(raw_arguments)
                result = await service.query_subscription(context)
            elif tool_name == "query_api_usage":
                result = await service.query_api_usage(
                    context, UsageInput.model_validate(raw_arguments)
                )
            elif tool_name == "check_service_status":
                result = await service.check_service_status(
                    context, ServiceStatusInput.model_validate(raw_arguments)
                )
            elif tool_name == "query_billing_record":
                result = await service.query_billing_record(
                    context, BillingRecordInput.model_validate(raw_arguments)
                )
            elif tool_name == "query_request_trace":
                result = await service.query_request_trace(
                    context, RequestTraceInput.model_validate(raw_arguments)
                )
            elif tool_name == "query_api_key_metadata":
                result = await service.query_api_key_metadata(
                    context, ApiKeyMetadataInput.model_validate(raw_arguments)
                )
            elif tool_name == "query_incident_impact":
                result = await service.query_incident_impact(
                    context, IncidentImpactInput.model_validate(raw_arguments)
                )
            elif tool_name == "search_knowledge":
                result = await self._search_knowledge(
                    service,
                    context,
                    KnowledgeSearchInput.model_validate(raw_arguments),
                )
            else:
                raise ValueError(f"unsupported in-process read tool: {tool_name}")
            await session.commit()
            return result.model_dump(mode="json")

    async def _search_knowledge(
        self,
        service: BusinessService,
        context: ToolCallContext,
        arguments: KnowledgeSearchInput,
    ) -> KnowledgeSearchResult:
        # The session is a ScopedAsyncSession; keeping the annotation local avoids
        # coupling the transport contract to SQLAlchemy's concrete session class.
        subscription = await service.session.scalar(
            select(Subscription).where(
                Subscription.tenant_id == context.tenant_id,
                Subscription.customer_id == context.customer_id,
                Subscription.status == "active",
            )
        )
        region_trace = await service.session.scalar(
            select(ApiRequestTrace)
            .where(
                ApiRequestTrace.tenant_id == context.tenant_id,
                ApiRequestTrace.customer_id == context.customer_id,
            )
            .order_by(ApiRequestTrace.observed_at.desc(), ApiRequestTrace.id.desc())
            .limit(1)
        )
        if subscription is None:
            raise RuntimeError("test retrieval subscription scope is missing")
        scope = RetrievalScopeSnapshot(
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            subscription_id=subscription.id,
            subscription_version=subscription.version,
            plan=subscription.plan,
            region_trace_id=region_trace.id if region_trace is not None else None,
            region_trace_version=region_trace.version if region_trace is not None else None,
            region=region_trace.region if region_trace is not None else None,
        )
        intent = (
            context.mcp_context.retrieval_intent
            if isinstance(context.mcp_context, ReadMcpCallContext)
            else None
        ) or resolve_retrieval_intent(arguments.query)
        repository = KnowledgeRepository(service.session)
        snapshot = await repository.pin_active_snapshot()
        normalized, evidence, provenance = await RetrievalService(
            repository, self._embedding
        ).retrieve_with_trace(
            arguments.query,
            plan=subscription.plan,
            region=region_trace.region if region_trace is not None else None,
            intent=intent.intent,
            logical_time=datetime.now(UTC),
            explicit_as_of=intent.as_of,
            historical_version=intent.historical_version,
            index_version=snapshot.index_version,
            scope_snapshot=scope,
        )
        items: list[KnowledgeEvidence] = []
        for selected in evidence.chunks:
            span = select_supporting_span(selected.chunk, normalized.normalized)
            if selected.chunk.source_locator is None or selected.chunk.eligibility_envelope is None:
                raise RuntimeError("test retrieval selected incomplete provenance")
            items.append(
                KnowledgeEvidence(
                    evidence_id=selected.chunk.chunk_id,
                    document_id=selected.chunk.document_id,
                    document_type=selected.chunk.document_type,
                    chunk_id=selected.chunk.chunk_id,
                    title=selected.chunk.title,
                    section_path=selected.chunk.section_path,
                    version=selected.chunk.version,
                    effective_at=selected.chunk.effective_at,
                    content_hash=selected.chunk.content_hash,
                    source_locator=span.locator,
                    chunk_locator=selected.chunk.source_locator,
                    eligibility_envelope=selected.chunk.eligibility_envelope,
                    supporting_span=span.text,
                    supporting_span_eligible=span.material_claim_eligible,
                    supporting_span_reason=span.reason_code,
                    token_count=selected.chunk.token_count,
                    retrieval_score=canonical_decimal_string(selected.rrf_score),
                    evidence_group=selected.chunk.evidence_group,
                )
            )
        return KnowledgeSearchResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            normalized_query=normalized.normalized,
            evidence=items,
            conflict=evidence.conflict,
            refusal_reason=evidence.refusal_reason,
            index_version=provenance.index_version,
            source_refs=[
                SourceRef(
                    source_type="knowledge_chunk",
                    source_id=selected.chunk.chunk_id,
                    observed_at=datetime.now(UTC),
                )
                for selected in evidence.chunks
            ],
        )

    async def _call_action(self, tool_name: str, raw: dict[str, Any]) -> dict[str, object]:
        context_fields = set(ToolCallContext.model_fields)
        context = ToolCallContext.model_validate(
            {key: value for key, value in raw.items() if key in context_fields}
        )

        def arguments_for(model: type[BaseModel]) -> dict[str, Any]:
            argument_fields = set(model.model_fields)
            unexpected = set(raw) - context_fields - argument_fields
            if unexpected:
                raise ValueError(f"unexpected action tool fields: {','.join(sorted(unexpected))}")
            return {key: value for key, value in raw.items() if key in argument_fields}

        execution = self._execution(context)
        async with self._factory.worker(execution) as session:
            service = BusinessService(session, test_capability=self._capability)
            result: BaseModel
            if tool_name == "propose_refund":
                result = await service.propose_refund(
                    context, RefundProposalInput.model_validate(arguments_for(RefundProposalInput))
                )
            elif tool_name == "propose_api_key_revocation":
                result = await service.propose_api_key_revocation_draft(
                    context,
                    ApiKeyRevocationProposalInput.model_validate(
                        arguments_for(ApiKeyRevocationProposalInput)
                    ),
                )
            elif tool_name == "propose_entitlement_change":
                result = await service.propose_entitlement_change_draft(
                    context,
                    EntitlementChangeProposalInput.model_validate(
                        arguments_for(EntitlementChangeProposalInput)
                    ),
                )
            else:
                raise ValueError(f"unsupported in-process action tool: {tool_name}")
            await session.commit()
            return result.model_dump(mode="json")
