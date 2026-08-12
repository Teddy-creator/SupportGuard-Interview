from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import AgentRunStore
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.approvals.service import RefundRuntime
from supportguard.contracts.capability_decisions import ProposalCausalDecisionV2
from supportguard.contracts.context import WorkerExecutionContext, worker_execution_context
from supportguard.contracts.finalizer import canonical_hash
from supportguard.contracts.tools import ObservationEnvelope, SourceRef
from supportguard.db.models import (
    AgentRun,
    ApiKeyMetadata,
    ApiRequestTrace,
    ApprovalRequest,
    ApprovalSnapshot,
    BillingRecord,
    BusinessAction,
    CitationBinding,
    ClaimRecord,
    ContextLedger,
    ContextMembership,
    HumanDecision,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestRun,
    ProposalRecord,
    RetrievalTrace,
    Subscription,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.rag.context_projection import project_context_evidence
from supportguard.rag.types import EligibilityEnvelope, RetrievalFilter, SourceLocatorV2
from supportguard.services.actions import (
    RuntimeActionExecutor,
    execute_runtime_action_capability,
)
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.attempts import AttemptLedger
from supportguard.services.business import action_hash
from supportguard.services.capability_ledger import PolicyCapabilityLedger
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger
from test_postgres_finalizer_faults import _approver_scope, _seed_run

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


async def _seed_production_shaped_pending_approval_fixture(
    factory: async_sessionmaker,
    prefix: str,
    *,
    action_type: str,
    resource_id_override: str | None = None,
    resource_version_override: int | None = None,
    supporting_span_locator: bool = False,
) -> tuple[str, str, str]:
    """Seed a production-shaped lineage fixture with explicit fake provenance."""

    customer_id = "cust_demo"
    run_id = await _seed_run(factory, prefix, customer_id=customer_id)
    proposal_id = f"proposal_{prefix}"
    resource_version = resource_version_override or 2
    resource_id = resource_id_override or f"bill_{prefix}"
    resource_field = "billing_record_id"
    business_tool = "query_billing_record"
    payload: dict[str, object] = {
        "billing_record_id": resource_id,
        "customer_id": customer_id,
        "amount": "49.00",
        "currency": "USD",
        "refund_reason": "Duplicate charge verified by billing lineage.",
        "business_version": resource_version,
    }
    if action_type == "api_key_revocation":
        resource_id = resource_id_override or f"key_{prefix}"
        resource_field = "api_key_id"
        business_tool = "query_api_key_metadata"
        payload = {
            "api_key_id": resource_id,
            "customer_id": customer_id,
            "fingerprint": f"fp_{prefix}",
            "reason": "Customer reported credential exposure.",
            "business_version": resource_version,
        }
    elif action_type == "entitlement_change":
        resource_id = resource_id_override or "sub_demo"
        resource_field = "subscription_id"
        business_tool = "query_subscription"
        async with factory() as session:
            subscription = await session.get(Subscription, resource_id)
            assert subscription is not None
            if resource_version_override is None:
                resource_version = subscription.version
            target_concurrency = 60 if subscription.concurrency_limit != 60 else 40
            current_entitlement = {
                "plan": subscription.plan,
                "rpm_limit": subscription.rpm_limit,
                "concurrency_limit": subscription.concurrency_limit,
            }
        payload = {
            "subscription_id": resource_id,
            "customer_id": customer_id,
            "change_type": "quota_change",
            "current": current_entitlement,
            "target": {"concurrency_limit": target_concurrency},
            "reason": "Explicit target is within the active Plan Catalog.",
            "business_version": resource_version,
        }

    async with factory() as session, session.begin():
        if action_type == "refund":
            if resource_id_override is None:
                session.add(
                    BillingRecord(
                        id=resource_id,
                        tenant_id="tenant_demo",
                        customer_id=customer_id,
                        amount=Decimal("49.00"),
                        currency="USD",
                        status="charged",
                        duplicate_of=None,
                        version=resource_version,
                    )
                )
        elif action_type == "api_key_revocation":
            if resource_id_override is None:
                session.add(
                    ApiKeyMetadata(
                        id=f"keymeta_{prefix}",
                        tenant_id="tenant_demo",
                        customer_id=customer_id,
                        key_id=resource_id,
                        fingerprint=f"fp_{prefix}",
                        status="active",
                        version=resource_version,
                        last_used_summary={},
                    )
                )
        else:
            subscription = await session.get(Subscription, resource_id, with_for_update=True)
            assert subscription is not None
            assert subscription.version == resource_version

        jobs = RuntimeJobRepository(session)
        job = await jobs.create(tenant_id="tenant_demo", run_id=run_id, kind="agent_start")
        lease = await jobs.claim(job_id=job.id, owner=f"worker-{prefix}-origin")
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="agent_start",
            segment_input={"proposal_id": proposal_id},
        )
        corpus = (
            await session.execute(
                select(KnowledgeChunk, KnowledgeDocument, KnowledgeIngestRun)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .join(
                    KnowledgeIngestRun,
                    (KnowledgeIngestRun.id == KnowledgeChunk.ingest_run_id)
                    & (KnowledgeIngestRun.index_version == KnowledgeChunk.index_version),
                )
                .where(
                    KnowledgeIngestRun.is_active.is_(True),
                    KnowledgeDocument.status == "active",
                )
                .limit(1)
            )
        ).one()
        chunk, document, ingest = corpus
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.tenant_id == "tenant_demo",
                Subscription.customer_id == customer_id,
            )
        )
        assert subscription is not None
        region_trace = await session.scalar(
            select(ApiRequestTrace)
            .where(
                ApiRequestTrace.tenant_id == "tenant_demo",
                ApiRequestTrace.customer_id == customer_id,
            )
            .order_by(ApiRequestTrace.observed_at.desc(), ApiRequestTrace.id.desc())
            .limit(1)
        )
        locator = SourceLocatorV2.build(
            document_key=document.document_key,
            document_internal_id=document.id,
            document_version=document.version,
            source_bytes=document.canonical_blob,
            corpus_snapshot_id=ingest.id,
            index_version=ingest.index_version,
            canonicalization_version=document.canonicalization_version,
            section_path=(
                f"{chunk.section_path} > supporting span"
                if supporting_span_locator
                else chunk.section_path
            ),
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            chunker_fingerprint=chunk.chunker_fingerprint,
            embedding_fingerprint=chunk.embedding_fingerprint,
        )
        if supporting_span_locator:
            assert locator.locator_hash != chunk.locator_hash
        logical_time = datetime.now(UTC)
        pipeline_contract = {
            "schema": "retrieval-pipeline.v2",
            "eligibility": "evidence-eligibility.v1",
        }
        filter_contract = RetrievalFilter.model_validate(
            {
                "intent": "current",
                "statuses": ["active"],
                "version": None,
                "minimum_authority": 50,
                "plan": subscription.plan,
                "region": region_trace.region if region_trace is not None else None,
                "effective_at": logical_time.isoformat(),
                "logical_time": logical_time.isoformat(),
                "index_version": ingest.index_version,
                "corpus_snapshot_id": ingest.id,
                "scope_snapshot": {
                    "schema_version": "retrieval-scope-snapshot.v1",
                    "tenant_id": "tenant_demo",
                    "customer_id": customer_id,
                    "subscription_id": subscription.id,
                    "subscription_version": subscription.version,
                    "plan": subscription.plan,
                    "region_trace_id": (region_trace.id if region_trace is not None else None),
                    "region_trace_version": (
                        region_trace.version if region_trace is not None else None
                    ),
                    "region": region_trace.region if region_trace is not None else None,
                },
                "eligibility_policy_version": "evidence-eligibility.v1",
                "pipeline_contract_hash": canonical_hash(pipeline_contract),
                "schema_version": "filter-contract.v2",
                "temporal_selector": {
                    "mode": "current",
                    "claim_effective_time": logical_time.isoformat(),
                },
            }
        ).model_dump(mode="json")
        eligibility = EligibilityEnvelope(
            corpus_snapshot_id=ingest.id,
            index_version=ingest.index_version,
            document_internal_id=document.id,
            chunk_id=chunk.chunk_key,
            status=document.status,
            authority_level=document.authority_level,
            applicable_plan=document.applicable_plan,
            applicable_region=document.applicable_region,
            effective_from=document.effective_from,
            effective_until=document.effective_until,
            logical_time=logical_time,
            filter_hash=canonical_hash(filter_contract),
            outcome="eligible",
            reason_code="eligible_hybrid_support",
        )
        evidence = [
            {
                "chunk_id": chunk.chunk_key,
                "document_id": document.document_key,
                "version": document.version,
                "index_version": ingest.index_version,
                "content_hash": chunk.content_hash,
                "source_locator": locator.model_dump(mode="json"),
                "chunk_locator": locator.model_dump(mode="json"),
                "eligibility_envelope": eligibility.model_dump(mode="json"),
                "evidence_group": "current",
            }
        ]
        tool_ledger = ToolLedger(session)
        turn, invocations = await tool_ledger.open_turn(
            lease,
            segment_id=marker.id,
            tool_round=1,
            decision={"decision_type": "tool_calls"},
            context_manifest={"fixture": "v1512-canonical-lineage-fake-provider"},
            calls=[
                InvocationSpec("business_call", business_tool, {}, 0),
                InvocationSpec("knowledge_call", "search_knowledge", {}, 1),
            ],
        )
        business_invocation, knowledge_invocation = invocations
        await tool_ledger.mark_executing(lease, business_invocation.id)
        business_observation = await tool_ledger.terminalize(
            lease,
            business_invocation.id,
            outcome="succeeded",
            observation=ObservationEnvelope(
                tool_name=business_tool,
                tool_call_id=business_invocation.provider_tool_call_id,
                ticket_id=f"ticket_{prefix}",
                run_id=run_id,
                attempt_index=1,
                status="ok",
                retryable=False,
                observed_at=logical_time,
                duration_ms=1,
                source_refs=[
                    SourceRef(
                        source_type="business_record",
                        source_id=f"business_record:{resource_id}",
                        observed_at=logical_time,
                    )
                ],
                data={resource_field: resource_id, "version": resource_version},
            ),
        )
        await tool_ledger.mark_executing(lease, knowledge_invocation.id)
        read_attempt = await AttemptLedger(session).reserve(
            lease,
            kind="read_mcp",
            logical_invocation_id=knowledge_invocation.id,
            transport_ordinal=1,
        )
        await AttemptLedger(session).finish(lease, read_attempt, status="succeeded")
        knowledge_observation = await tool_ledger.terminalize(
            lease,
            knowledge_invocation.id,
            outcome="succeeded",
            observation=ObservationEnvelope(
                tool_name="search_knowledge",
                tool_call_id=knowledge_invocation.provider_tool_call_id,
                ticket_id=f"ticket_{prefix}",
                run_id=run_id,
                attempt_index=1,
                status="ok",
                retryable=False,
                observed_at=logical_time,
                duration_ms=1,
                source_refs=[
                    SourceRef(
                        source_type="knowledge_chunk",
                        source_id=chunk.chunk_key,
                        observed_at=logical_time,
                    )
                ],
                data={"evidence": evidence},
            ),
        )
        await tool_ledger.close_turn(lease, turn.id)
        selected_candidate = {
            "chunk_id": chunk.chunk_key,
            "locator_hash": locator.locator_hash,
            "evidence_group": "current",
        }
        trace = RetrievalTrace(
            tenant_id="tenant_demo",
            run_id=run_id,
            job_id=job.id,
            segment_id=marker.id,
            origin_kind="agent_read_tool",
            logical_invocation_id=knowledge_invocation.id,
            tool_call_id=knowledge_invocation.provider_tool_call_id,
            fencing_token=lease.fencing_token,
            delivery_generation=1,
            origin_job_id=job.id,
            origin_marker_id=marker.id,
            origin_fencing_token=lease.fencing_token,
            origin_segment_ref=marker.id,
            terminal_transport_attempt_id=None,
            trace_status="started",
            result_digest=None,
            trace_logical_time=logical_time,
            temporal_selector=filter_contract["temporal_selector"],
            query_hash="2" * 64,
            filter_contract=filter_contract,
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
            index_version=ingest.index_version,
            corpus_snapshot_id=ingest.id,
            runtime_provenance={"provider_mode": "fixture"},
        )
        session.add(trace)
        await session.flush()
        trace.terminal_transport_attempt_id = read_attempt.transport_attempt_id
        trace.trace_status = "terminal_ok"
        trace.result_digest = canonical_hash(evidence)
        trace.selected_candidates = [selected_candidate]
        trace.evidence_groups = [
            {
                "group": "current",
                "filter": filter_contract,
                "selected_candidates": [selected_candidate],
            }
        ]
        trace.eligibility_envelopes = [eligibility.model_dump(mode="json")]
        trace.pipeline_contract = pipeline_contract
        trace.embedding_fingerprint = chunk.embedding_fingerprint
        trace.pipeline_fingerprint = canonical_hash(pipeline_contract)
        llm_attempt = await AttemptLedger(session).reserve(lease, kind="llm")
        await AttemptLedger(session).finish(lease, llm_attempt, status="succeeded")
        context = ContextLedger(
            tenant_id="tenant_demo",
            run_id=run_id,
            job_id=job.id,
            provider_attempt_id=llm_attempt.id,
            serializer_version="canonical-json.v1",
            canonical_request_hash="1" * 64,
            canonical_request_bytes=None,
            request_storage_mode="hash_only",
            sensitivity_manifest={},
            component_manifest={
                "evidence_projection_version": "context-evidence-projection.v1",
                "sections": [
                    {
                        "name": "retrieved_evidence",
                        "content_hash": canonical_hash(evidence),
                    }
                ],
            },
            token_preflight={},
            runtime_provenance={"provider_mode": "fixture", "model": "fake"},
        )
        session.add(context)
        await session.flush()
        binding_id = f"citation_{uuid4().hex}"
        fragment_hash = canonical_hash(
            project_context_evidence(evidence[0], citation_binding_id=binding_id)
        )
        membership = ContextMembership(
            tenant_id="tenant_demo",
            run_id=run_id,
            origin_job_id=job.id,
            origin_marker_id=marker.id,
            origin_fencing_token=lease.fencing_token,
            origin_segment_ref=marker.id,
            logical_invocation_id=knowledge_invocation.id,
            executor_job_id=job.id,
            executor_marker_id=marker.id,
            executor_fencing_token=lease.fencing_token,
            provider_attempt_id=llm_attempt.id,
            context_ledger_id=context.id,
            payload_ordinal=0,
            payload_json_pointer="/retrieved_evidence/0",
            serialized_evidence_fragment_hash=fragment_hash,
            ordered_membership_root_hash=canonical_hash(
                [
                    {
                        "payload_ordinal": 0,
                        "citation_binding_id": binding_id,
                        "fragment_hash": fragment_hash,
                    }
                ]
            ),
        )
        session.add(membership)
        await session.flush()
        session.add(
            CitationBinding(
                id=binding_id,
                tenant_id="tenant_demo",
                run_id=run_id,
                origin_job_id=job.id,
                membership_id=membership.id,
                observation_id=knowledge_observation.id,
                tool_invocation_id=knowledge_invocation.id,
                retrieval_trace_id=trace.id,
                provider_attempt_id=llm_attempt.id,
                context_ledger_id=context.id,
                selected_candidate_ordinal=0,
                locator_hash=locator.locator_hash,
                temporal_selector=filter_contract["temporal_selector"],
                binding_hash=canonical_hash(
                    {
                        "membership_id": membership.id,
                        "trace_id": trace.id,
                        "locator_hash": locator.locator_hash,
                    }
                ),
            )
        )
        answer = f"{action_type} policy and current resource facts permit review."
        session.add(
            ClaimRecord(
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job.id,
                provider_attempt_id=llm_attempt.id,
                context_ledger_id=context.id,
                claim_hash=canonical_hash({"text": answer, "provider_attempt_id": llm_attempt.id}),
                answer_hash=hashlib.sha256(answer.encode()).hexdigest(),
                claim_text=answer,
                support_refs={
                    "knowledge_locator_hashes": [locator.locator_hash],
                    "citation_binding_ids": [binding_id],
                    "observation_source_ids": [f"business_record:{resource_id}"],
                },
                status="validated",
            )
        )
        observation_binding = [
            {
                "tool_name": business_tool,
                "tool_call_id": business_invocation.provider_tool_call_id,
                "invocation_id": business_invocation.id,
                "observation_id": business_observation.id,
                "observation_content_hash": business_observation.content_hash,
                "turn_group_id": turn.id,
                "status": "ok",
                "source_refs": [{"source_id": f"business_record:{resource_id}"}],
                "resource_field": resource_field,
                "resource_id": resource_id,
                "resource_version": resource_version,
            },
            {
                "tool_name": "search_knowledge",
                "tool_call_id": knowledge_invocation.provider_tool_call_id,
                "invocation_id": knowledge_invocation.id,
                "observation_id": knowledge_observation.id,
                "observation_content_hash": knowledge_observation.content_hash,
                "turn_group_id": turn.id,
                "status": "ok",
                "source_refs": [{"source_id": chunk.chunk_key}],
                "citation_binding_ids": [binding_id],
            },
        ]
        proposal = ProposalRecord(
            id=proposal_id,
            tenant_id="tenant_demo",
            run_id=run_id,
            proposal_identity=f"identity:{prefix}",
            action_type=action_type,
            resource_id=resource_id,
            resource_version=resource_version,
            action_payload=payload,
            observation_binding=observation_binding,
            action_hash=action_hash(payload),
            status="draft",
        )
        session.add(proposal)
        await session.flush()
        capability_name = {
            "refund": "propose_refund",
            "api_key_revocation": "propose_api_key_revocation",
            "entitlement_change": "propose_entitlement_change",
        }[action_type]
        policy_ledger = PolicyCapabilityLedger(session)
        reserved = await policy_ledger.reserve(
            lease,
            segment_id=marker.id,
            capability_name=capability_name,
            causal_decision=ProposalCausalDecisionV2(
                capability_name=capability_name,  # type: ignore[arg-type]
                action_type=action_type,  # type: ignore[arg-type]
                resource_id=resource_id,
                resource_version=resource_version,
                model_arguments=payload,
                observation_binding_hash=canonical_hash(observation_binding),
                policy_version="supportguard-policy-gate.v1",
            ),
            observation_binding=observation_binding,
        )
        await policy_ledger.finish(
            lease,
            reserved,
            status="succeeded",
            payload={"proposal_id": proposal.id},
        )
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_{prefix}",
            checkpoint_hash="c" * 64,
            outcome="interrupted",
            state={"segment_events": []},
            proposal_id=proposal.id,
        )
        marker_id = marker.id

    raw_url = _database_url()
    assert raw_url is not None
    worker = create_async_engine(
        make_url(raw_url)
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        async with worker_factory() as session, session.begin():
            await session.execute(select(func.set_config("app.tenant_id", "tenant_demo", True)))
            approval = await SegmentRepository(session).finalize_interrupt(
                lease,
                marker_id=marker_id,
                proposal_id=proposal_id,
            )
            return approval.id, approval.ticket_id, answer
    finally:
        await worker.dispose()


async def _prepare(
    action_type: str,
    scenario: str,
    *,
    resource_id_override: str | None = None,
    resource_version_override: int | None = None,
):
    url = _database_url()
    assert url is not None
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    # Keep every derived fixture identity below the frozen VARCHAR(64) boundary.
    prefix = f"v12_{scenario[:8]}_{action_type[:6]}_{uuid4().hex[:8]}"
    (
        approval_id,
        ticket_id,
        validated_answer,
    ) = await _seed_production_shaped_pending_approval_fixture(
        admin_factory,
        prefix,
        action_type=action_type,
        resource_id_override=resource_id_override,
        resource_version_override=resource_version_override,
    )
    async with api_factory.request(_approver_scope(prefix)) as session:
        accepted = await ApprovalCommandCoordinator(session).decide(
            tenant_id="tenant_demo",
            approval_id=approval_id,
            decision="approve",
            actor_id="user_approver_demo",
            idempotency_key=f"decision-{prefix}",
            reason="v1.5.12 Runtime Action binding proof.",
            approver_note="Bound worker execution fixture.",
            trace_id=f"trace-{prefix}",
        )
        await session.commit()
    async with admin_factory() as session, session.begin():
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.run_id is not None
        decision = await session.scalar(
            select(HumanDecision).where(HumanDecision.approval_id == approval.id)
        )
        assert decision is not None
        lease = await RuntimeJobRepository(session).claim(
            job_id=str(accepted.job_id),
            owner=f"worker-{prefix}",
        )
        context = WorkerExecutionContext(
            tenant_id=approval.tenant_id,
            actor_principal_id=approval.customer_id,
            executor_service_principal=lease.owner,
            customer_id=approval.customer_id,
            ticket_id=ticket_id,
            run_id=approval.run_id,
            job_id=lease.job_id,
            segment_id=f"segment-{prefix}",
            delivery_generation=1,
            fencing_token=lease.fencing_token,
            trace_id=f"execute-{prefix}",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        marker = await SegmentRepository(session).prepare(
            lease,
            delivery_generation=1,
            segment_kind="approval_resume",
            segment_input={"approval_id": approval.id, "decision_id": decision.id},
        )
        frozen_snapshot = await session.scalar(
            select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval.id)
        )
        assert frozen_snapshot is not None
        claims = [
            {
                "text": validated_answer,
                "knowledge_locator_hashes": [
                    str(item["locator_hash"])
                    for item in frozen_snapshot.policy_binding["citation_lineage"]
                ],
                "citation_binding_ids": list(frozen_snapshot.citation_binding_refs),
                "observation_source_ids": [f"business_record:{approval.resource_id}"],
            }
        ]
        await SegmentRepository(session).checkpoint_written(
            lease,
            marker_id=marker.id,
            checkpoint_id=f"checkpoint_resume_{prefix}",
            checkpoint_hash="d" * 64,
            outcome="completed",
            state={
                "ticket_id": approval.ticket_id,
                "customer_id": approval.customer_id,
                "run_id": approval.run_id,
                "trace_id": f"finalizer-{prefix}",
                "classification": {
                    "issue_type": approval.action_type,
                    "risk": "high",
                },
                "validated_answer": validated_answer,
                "human_decision": {
                    "approval_id": approval.id,
                    "action": "approve",
                },
                "execution_result": {
                    "approval_id": approval.id,
                    "action_type": approval.action_type,
                    "resource_id": approval.resource_id,
                    "action_hash": approval.action_hash,
                    "idempotency_key": approval.idempotency_key,
                    "status": "execution_pending",
                    "execution_intent": "execute_runtime_action",
                    "expected_approval_status": "approved",
                },
                "final": {
                    "answer": "Execution pending.",
                    "terminal_state": "verification_pending",
                    "knowledge_chunk_ids": [],
                    "business_source_ids": [],
                    "material_claims": claims,
                    "policy_route": "await_approval",
                },
                "tool_observations": [],
                "evidence": [],
                "segment_events": [],
            },
            approval_id=approval.id,
        )
        resource_id = approval.resource_id
        idempotency_key = approval.idempotency_key
    await api.dispose()
    return admin, admin_factory, approval_id, resource_id, idempotency_key, lease, context


async def test_approval_projection_resolves_supporting_span_through_trace_candidate() -> None:
    """A frozen supporting span is not the enclosing indexed chunk identity."""

    url = _database_url()
    if url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    admin = create_async_engine(url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        make_url(url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    api_factory = create_scoped_session_factory(api)
    prefix = f"v12_span_{uuid4().hex[:8]}"
    try:
        approval_id, _, _ = await _seed_production_shaped_pending_approval_fixture(
            admin_factory,
            prefix,
            action_type="refund",
            supporting_span_locator=True,
        )
        async with api_factory.request(_approver_scope(prefix)) as session:
            projected = await session.scalar(
                text("SELECT supportguard_api_get_approval(:approval_id)"),
                {"approval_id": approval_id},
            )
        assert isinstance(projected, dict)
        assert projected["actionable"] is True
        assert projected["snapshot_summary"]["citation_count"] == 1
        assert len(projected["evidence_summaries"]) == 1
        assert set(projected["evidence_summaries"][0]) == {
            "title",
            "section_path",
            "version",
            "freshness",
        }
        assert projected["evidence_summaries"][0]["freshness"] in {
            "current",
            "unavailable",
        }
    finally:
        await api.dispose()
        await admin.dispose()


@pytest.mark.parametrize(
    "action_type",
    ("refund", "api_key_revocation", "entitlement_change"),
)
async def test_runtime_action_resource_drift_stales_in_database(
    action_type: str,
) -> None:
    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        context,
    ) = await _prepare(action_type, "resource_drift")
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session, session.begin():
            if action_type == "refund":
                resource = await session.get(BillingRecord, resource_id)
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(ApiKeyMetadata.key_id == resource_id)
                )
            else:
                resource = await session.get(Subscription, resource_id)
            assert resource is not None
            resource.version += 1
        async with worker_factory.worker(context) as session:
            result = await RuntimeActionExecutor(session).execute(
                lease,
                approval_id=approval_id,
            )
            await session.commit()
        assert result.status == "stale"
        # The entitlement Subscription is both the action target and the
        # retrieval scope snapshot.  Its drift therefore invalidates
        # publication lineage before the later resource-specific CAS check.
        assert result.reason == (
            "publication_binding_stale"
            if action_type == "entitlement_change"
            else "resource_snapshot_stale"
        )
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status == "stale"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_runtime_action_rejects_job_from_another_approval() -> None:
    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    first = await _prepare("api_key_revocation", "job_a")
    second = await _prepare("refund", "approval_b")
    admin_a, _, approval_a, _, _, lease_a, context_a = first
    admin_b, factory_b, approval_b, _, _, _, _ = second
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with factory_b() as session:
            decision_b = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_b)
            )
            assert decision_b is not None
        with pytest.raises(DBAPIError, match="action_binding_invalid"):
            async with worker_factory.worker(context_a) as session:
                await execute_runtime_action_capability(
                    session,
                    approval_id=approval_b,
                    human_decision_id=decision_b.id,
                    lease=lease_a,
                )
        async with factory_b() as session:
            rows = (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.id.in_([approval_a, approval_b]))
                )
            ).all()
            assert {row.status for row in rows} == {"approved"}
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id.in_([approval_a, approval_b])
                    )
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin_a.dispose()
        await admin_b.dispose()


async def test_runtime_action_old_fence_and_exact_replay_are_effect_once() -> None:
    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        idempotency_key,
        lease,
        context,
    ) = await _prepare("refund", "fence_replay")
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        old_lease = replace(lease, fencing_token=lease.fencing_token + 1)
        old_context = replace(context, fencing_token=old_lease.fencing_token)
        with pytest.raises((DBAPIError, RuntimeConflict), match="stale_fencing_token"):
            async with worker_factory.worker(old_context) as session:
                await RuntimeActionExecutor(session).execute(
                    old_lease,
                    approval_id=approval_id,
                )
        async with worker_factory.worker(context) as session:
            first = await RefundRuntime(session).execute_refund(
                approval_id,
                idempotency_key=idempotency_key,
                trace_id=context.trace_id,
                lease=lease,
            )
            await session.commit()
        async with worker_factory.worker(context) as session:
            replay = await RefundRuntime(session).execute_refund(
                approval_id,
                idempotency_key=idempotency_key,
                trace_id=context.trace_id,
                lease=lease,
            )
            await session.commit()
        assert first.status == replay.status == "succeeded"
        assert first.business_action_id == replay.business_action_id
        assert first.reused is False and replay.reused is True
        async with admin_factory() as session:
            billing = await session.get(BillingRecord, resource_id)
            assert billing is not None
            assert billing.status == "refunded" and billing.version == 3
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 1
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def _append_competing_ticket_event(
    factory: async_sessionmaker,
    *,
    approval_id: str,
    label: str,
) -> None:
    """Advance the canonical head after the accepted HumanDecision."""

    async with factory() as session, session.begin():
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.run_id is not None
        run = await session.get(AgentRun, approval.run_id)
        assert run is not None
        await AgentRunStore(session).append_event(
            run,
            event_type="runtime_action_competing_head_fixture",
            payload={"approval_id": approval.id, "label": label},
            visibility="internal",
        )


@pytest.mark.parametrize("outcome", ("success", "stale"))
async def test_runtime_action_event_head_conflict_rolls_back_every_effect(
    outcome: str,
) -> None:
    """Canonical publication failure must undo the owner capability's writes."""

    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        _,
        lease,
        context,
    ) = await _prepare("refund", f"head_{outcome}")
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    try:
        async with admin_factory() as session, session.begin():
            if outcome == "stale":
                billing = await session.get(BillingRecord, resource_id)
                assert billing is not None
                billing.version += 1
        await _append_competing_ticket_event(
            admin_factory,
            approval_id=approval_id,
            label=outcome,
        )
        with pytest.raises(RuntimeConflict, match="action_expected_head_conflict"):
            async with worker_factory.worker(context) as session:
                await RuntimeActionExecutor(session).execute(
                    lease,
                    approval_id=approval_id,
                )
                await session.commit()
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            billing = await session.get(BillingRecord, resource_id)
            proposal = await session.get(
                ProposalRecord,
                approval.proposal_id if approval is not None else "",
            )
            assert approval is not None and approval.status == "approved"
            assert billing is not None and billing.status == "charged"
            assert billing.version == (3 if outcome == "stale" else 2)
            assert proposal is not None and proposal.status == "bound"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_production_coordinator_cannot_bypass_database_owned_stale() -> None:
    """A Python publication preflight cannot replace the DB stale transition."""

    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    (
        admin,
        admin_factory,
        approval_id,
        resource_id,
        idempotency_key,
        lease,
        context,
    ) = await _prepare("refund", "db_stale")
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    worker_factory = create_scoped_session_factory(worker)
    sibling_proposal_id = "proposal_db_stale_sibling"
    try:
        with worker_execution_context.bind(context):
            intent = await ApprovalCoordinator(worker_factory).handle(
                approval_id=approval_id,
                idempotency_key=idempotency_key,
                decision={
                    "action": "approve",
                    "approver_id": "user_approver_demo",
                    "job_id": lease.job_id,
                    "fencing_token": lease.fencing_token,
                },
                trace_id=context.trace_id,
                # Deliberately invalid as an application publication document:
                # production must ignore it and let b183 derive durable truth.
                publication_state={},
            )
        assert intent["status"] == "execution_pending"
        async with admin_factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.proposal_id is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            assert proposal is not None
            session.add(
                ProposalRecord(
                    id=sibling_proposal_id,
                    tenant_id=proposal.tenant_id,
                    run_id=proposal.run_id,
                    proposal_identity=f"{proposal.proposal_identity}:sibling",
                    action_type=proposal.action_type,
                    resource_id=proposal.resource_id,
                    resource_version=proposal.resource_version,
                    action_payload=proposal.action_payload,
                    observation_binding=proposal.observation_binding,
                    action_hash=proposal.action_hash,
                    status="draft",
                    status_version=1,
                )
            )
            billing = await session.get(BillingRecord, resource_id)
            assert billing is not None
            billing.version += 1
        async with worker_factory.worker(context) as session:
            result = await RuntimeActionExecutor(session).execute(
                lease,
                approval_id=approval_id,
            )
            await session.commit()
        assert result.status == "stale"
        assert result.reason == "resource_snapshot_stale"
        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            proposal = await session.get(
                ProposalRecord,
                approval.proposal_id if approval is not None else "",
            )
            sibling = await session.get(ProposalRecord, sibling_proposal_id)
            assert approval is not None and approval.status == "stale"
            assert proposal is not None and proposal.status == "stale"
            assert sibling is not None and sibling.status == "stale"
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                == 0
            )
    finally:
        await worker.dispose()
        await admin.dispose()


async def test_same_resource_approvals_lock_full_proposal_set_without_deadlock() -> None:
    """Executed replay and a later Approval cannot deadlock on sibling Proposals."""

    if _database_url() is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    first = await _prepare("refund", "same_a")
    shared_resource_id = first[3]
    admin_a, factory_a, approval_a, _, _, lease_a, context_a = first
    worker = create_async_engine(
        make_url(_database_url())
        .set(username="supportguard_worker", password="supportguard_worker")  # noqa: S106
        .render_as_string(hide_password=False),
        pool_size=2,
        max_overflow=0,
    )
    worker_factory = create_scoped_session_factory(worker)
    admin_b = None
    try:
        # Consume the first Approval so the unique-active-resource invariant
        # permits a later request for the resource's new version.  Its leased
        # Job remains a valid exact-replay contender.
        async with worker_factory.worker(context_a) as session:
            first_result = await RuntimeActionExecutor(session).execute(
                lease_a,
                approval_id=approval_a,
            )
            await session.commit()
        assert first_result.status == "succeeded" and first_result.reused is False
        second = await _prepare(
            "refund",
            "same_b",
            resource_id_override=shared_resource_id,
            resource_version_override=3,
        )
        admin_b, _, approval_b, _, _, lease_b, context_b = second
        barrier = asyncio.Barrier(2)

        async def execute(
            *,
            approval_id: str,
            lease,
            context: WorkerExecutionContext,
        ):
            await barrier.wait()
            async with worker_factory.worker(context) as session:
                result = await RuntimeActionExecutor(session).execute(
                    lease,
                    approval_id=approval_id,
                )
                await session.commit()
                return result

        results = await asyncio.wait_for(
            asyncio.gather(
                execute(approval_id=approval_a, lease=lease_a, context=context_a),
                execute(approval_id=approval_b, lease=lease_b, context=context_b),
            ),
            timeout=15,
        )
        assert sorted(item.status for item in results) == ["stale", "succeeded"]
        replay = next(item for item in results if item.status == "succeeded")
        stale = next(item for item in results if item.status == "stale")
        assert replay.reused is True
        assert stale.reason == "resource_snapshot_stale"
        async with factory_a() as session:
            billing = await session.get(BillingRecord, shared_resource_id)
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequest).where(
                            ApprovalRequest.id.in_([approval_a, approval_b])
                        )
                    )
                ).all()
            )
            proposals = list(
                (
                    await session.scalars(
                        select(ProposalRecord).where(
                            ProposalRecord.id.in_(
                                [
                                    item.proposal_id
                                    for item in approvals
                                    if item.proposal_id is not None
                                ]
                            )
                        )
                    )
                ).all()
            )
            assert billing is not None
            assert billing.status == "refunded" and billing.version == 3
            assert sorted(item.status for item in approvals) == ["executed", "stale"]
            assert {item.status for item in proposals} == {"stale"}
            assert (
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.resource_id == shared_resource_id
                    )
                )
                == 1
            )
    finally:
        await worker.dispose()
        await admin_a.dispose()
        if admin_b is not None:
            await admin_b.dispose()
