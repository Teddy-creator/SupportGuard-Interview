from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import (
    AgentCallAttempt,
    ApiRequestTrace,
    CitationBinding,
    ClaimRecord,
    ContextLedger,
    ContextMembership,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestRun,
    RetrievalTrace,
    Subscription,
    ToolInvocation,
    ToolObservation,
    ToolTransportAttempt,
)
from supportguard.rag.context_projection import (
    EVIDENCE_PROJECTION_V1,
    EVIDENCE_PROJECTION_V2,
    project_context_evidence,
)
from supportguard.rag.temporal import TEMPORAL_SELECTOR_ADAPTER
from supportguard.rag.types import (
    EligibilityEnvelope,
    RetrievalFilter,
    SourceLocator,
    SourceLocatorV2,
)

_LOCATOR_ADAPTER: TypeAdapter[SourceLocator] = TypeAdapter(SourceLocator)


class CitationPublicationConflict(RuntimeError):
    pass


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class CitationPublicationValidator:
    """Independent publication-time validation over durable RAG and claim facts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _group_filter_for_candidate(
        trace: RetrievalTrace, *, chunk_id: str, locator_hash: str
    ) -> tuple[str, RetrievalFilter]:
        matches: list[tuple[str, RetrievalFilter]] = []
        for raw_group in trace.evidence_groups:
            group = raw_group.get("group")
            raw_filter = raw_group.get("filter")
            selected = raw_group.get("selected_candidates")
            if group not in {"current", "historical"} or not isinstance(
                raw_filter, dict
            ) or not isinstance(selected, list):
                continue
            if not any(
                candidate.get("chunk_id") == chunk_id
                and candidate.get("locator_hash") == locator_hash
                and candidate.get("evidence_group") == group
                for candidate in selected
                if isinstance(candidate, dict)
            ):
                continue
            try:
                parsed_filter = RetrievalFilter.model_validate(raw_filter)
            except ValidationError as exc:
                raise CitationPublicationConflict("publication_filter_contract_invalid") from exc
            matches.append((group, parsed_filter))
        if len(matches) != 1:
            raise CitationPublicationConflict("publication_evidence_group_mismatch")
        return matches[0]

    @staticmethod
    def _pipeline_for_group(trace: RetrievalTrace, group: str) -> dict[str, Any]:
        contract = trace.pipeline_contract
        if contract.get("schema") == "compare-retrieval-pipeline.v1":
            nested = contract.get(group)
            if not isinstance(nested, dict):
                raise CitationPublicationConflict("publication_pipeline_contract_invalid")
            return nested
        return contract

    @staticmethod
    def _publication_temporal_contract(
        *,
        group: str,
        filters: RetrievalFilter,
        trace_logical_time: datetime,
        publication_checked_at: datetime,
    ) -> tuple[set[str], datetime | None]:
        selector = filters.temporal_selector
        if group == "current":
            if (
                selector.mode != "current"
                or selector.claim_effective_time != trace_logical_time
                or filters.intent != "current"
            ):
                raise CitationPublicationConflict("publication_temporal_selector_invalid")
            return {"active"}, publication_checked_at
        if group != "historical" or selector.mode == "current" or filters.intent != "historical":
            raise CitationPublicationConflict("publication_temporal_selector_invalid")
        return {"active", "deprecated"}, selector.claim_effective_time

    async def _validate_scope_origin(self, filters: RetrievalFilter) -> None:
        scope = filters.scope_snapshot
        subscription = await self.session.get(Subscription, scope.subscription_id)
        if (
            subscription is None
            or subscription.tenant_id != scope.tenant_id
            or subscription.customer_id != scope.customer_id
            or subscription.version != scope.subscription_version
            or subscription.status != "active"
            or subscription.plan != scope.plan
            or filters.plan != scope.plan
        ):
            raise CitationPublicationConflict("publication_scope_origin_changed")
        latest_region_trace = await self.session.scalar(
            select(ApiRequestTrace)
            .where(
                ApiRequestTrace.tenant_id == scope.tenant_id,
                ApiRequestTrace.customer_id == scope.customer_id,
            )
            .order_by(ApiRequestTrace.observed_at.desc(), ApiRequestTrace.id.desc())
            .limit(1)
        )
        if scope.region_trace_id is None:
            if filters.region is not None or latest_region_trace is not None:
                raise CitationPublicationConflict("publication_scope_origin_changed")
            return
        region_trace = await self.session.get(ApiRequestTrace, scope.region_trace_id)
        if (
            region_trace is None
            or latest_region_trace is None
            or latest_region_trace.id != region_trace.id
            or region_trace.tenant_id != scope.tenant_id
            or region_trace.customer_id != scope.customer_id
            or region_trace.version != scope.region_trace_version
            or region_trace.region != scope.region
            or filters.region != scope.region
        ):
            raise CitationPublicationConflict("publication_scope_origin_changed")

    async def validate(self, *, run_id: str, state: dict[str, Any]) -> None:
        final = dict(state.get("final", {}))
        if final.get("terminal_state") != "resolved":
            return
        publication_checked_at = await self.session.scalar(select(func.clock_timestamp()))
        if not isinstance(publication_checked_at, datetime):
            raise CitationPublicationConflict("publication_database_time_missing")
        if publication_checked_at.tzinfo is None:
            publication_checked_at = publication_checked_at.replace(tzinfo=UTC)
        claims = list(final.get("material_claims", []))
        cited_binding_ids = {
            str(binding_id)
            for claim in claims
            for binding_id in claim.get("citation_binding_ids", [])
        }
        binding_rows = (
            await self.session.execute(
                select(
                    CitationBinding,
                    ContextMembership,
                    RetrievalTrace,
                    ToolInvocation,
                    ToolObservation,
                )
                .join(ContextMembership, ContextMembership.id == CitationBinding.membership_id)
                .join(RetrievalTrace, RetrievalTrace.id == CitationBinding.retrieval_trace_id)
                .join(ToolInvocation, ToolInvocation.id == CitationBinding.tool_invocation_id)
                .join(ToolObservation, ToolObservation.id == CitationBinding.observation_id)
                .where(
                    CitationBinding.run_id == run_id,
                    CitationBinding.id.in_(cited_binding_ids or {"<none>"}),
                    RetrievalTrace.trace_status == "terminal_ok",
                )
            )
        ).all()
        if {row[0].id for row in binding_rows} != cited_binding_ids:
            raise CitationPublicationConflict("citation_binding_missing")
        bindings = {row[0].id: row for row in binding_rows}
        provider_attempt_ids = {row[0].provider_attempt_id for row in binding_rows}
        context_ledger_ids = {row[0].context_ledger_id for row in binding_rows}
        if cited_binding_ids and (
            len(provider_attempt_ids) != 1 or len(context_ledger_ids) != 1
        ):
            raise CitationPublicationConflict("citation_binding_incomplete")
        bound_chunks: dict[str, tuple[str, str]] = {}
        for binding, membership, trace, invocation, observation in binding_rows:
            terminal_transport = await self.session.get(
                ToolTransportAttempt, trace.terminal_transport_attempt_id or ""
            )
            terminal_attempt = (
                await self.session.get(
                    AgentCallAttempt, terminal_transport.agent_call_attempt_id
                )
                if terminal_transport is not None
                else None
            )
            terminal_success_count = await self.session.scalar(
                select(func.count(ToolTransportAttempt.id)).where(
                    ToolTransportAttempt.tenant_id == binding.tenant_id,
                    ToolTransportAttempt.run_id == run_id,
                    ToolTransportAttempt.invocation_id == invocation.id,
                    ToolTransportAttempt.status == "succeeded",
                )
            )
            if (
                binding.tool_invocation_id != invocation.id
                or binding.observation_id != observation.id
                or membership.logical_invocation_id != invocation.id
                or trace.logical_invocation_id != invocation.id
                or observation.invocation_id != invocation.id
                or observation.status != "ok"
                or invocation.run_id != run_id
                or observation.run_id != run_id
                or trace.run_id != run_id
                or trace.origin_kind != "agent_read_tool"
                or trace.trace_schema_version != "retrieval-trace.v3"
                or trace.trace_logical_time is None
                or trace.pipeline_fingerprint != _hash(trace.pipeline_contract)
                or binding.origin_job_id != membership.origin_job_id
                or binding.origin_job_id != trace.origin_job_id
                or binding.origin_job_id != invocation.job_id
                or binding.origin_job_id != observation.job_id
                or trace.origin_marker_id != membership.origin_marker_id
                or trace.origin_marker_id != invocation.segment_id
                or trace.origin_marker_id != observation.segment_id
                or trace.origin_fencing_token != membership.origin_fencing_token
                or trace.origin_fencing_token != invocation.fencing_token
                or trace.origin_fencing_token != observation.fencing_token
                or trace.origin_segment_ref != membership.origin_segment_ref
                or trace.job_id != trace.origin_job_id
                or trace.segment_id != trace.origin_marker_id
                or trace.fencing_token != trace.origin_fencing_token
                or terminal_transport is None
                or terminal_attempt is None
                or terminal_transport.invocation_id != invocation.id
                or terminal_transport.run_id != run_id
                or terminal_transport.tenant_id != binding.tenant_id
                or terminal_transport.status != "succeeded"
                or terminal_attempt.id != terminal_transport.agent_call_attempt_id
                or terminal_attempt.tenant_id != binding.tenant_id
                or terminal_attempt.run_id != run_id
                or terminal_attempt.job_id != terminal_transport.job_id
                or terminal_attempt.fencing_token != terminal_transport.fencing_token
                or terminal_attempt.logical_invocation_id != invocation.id
                or terminal_attempt.call_kind != "read_mcp"
                or terminal_attempt.status != "succeeded"
                or terminal_success_count != 1
                or membership.payload_json_pointer
                != f"/retrieved_evidence/{membership.payload_ordinal}"
                or membership.payload_ordinal < 0
                or binding.selected_candidate_ordinal < 0
                or binding.selected_candidate_ordinal >= len(trace.selected_candidates)
            ):
                raise CitationPublicationConflict("citation_binding_incomplete")
            selected = trace.selected_candidates[binding.selected_candidate_ordinal]
            chunk_id = str(selected.get("chunk_id") or "")
            chunk_locator_hash = str(selected.get("locator_hash") or "")
            if not chunk_id or len(chunk_locator_hash) != 64:
                raise CitationPublicationConflict("citation_binding_incomplete")
            if binding.id in bound_chunks:
                raise CitationPublicationConflict("citation_binding_incomplete")
            bound_chunks[binding.id] = (chunk_id, chunk_locator_hash)
        cited_chunk_ids = set(final.get("knowledge_chunk_ids", []))
        if cited_chunk_ids != {chunk_id for chunk_id, _ in bound_chunks.values()}:
            raise CitationPublicationConflict("publication_citation_not_in_context")
        all_evidence = list(state.get("evidence", []))
        if cited_binding_ids:
            context_ledger = await self.session.get(
                ContextLedger, next(iter(context_ledger_ids))
            )
            if context_ledger is None or context_ledger.run_id != run_id:
                raise CitationPublicationConflict("citation_binding_incomplete")
            projection_version = context_ledger.component_manifest.get(
                "evidence_projection_version", EVIDENCE_PROJECTION_V1
            )
            if projection_version not in {
                EVIDENCE_PROJECTION_V1,
                EVIDENCE_PROJECTION_V2,
            }:
                raise CitationPublicationConflict("publication_context_projection_unknown")
            payload_binding_rows = (
                await self.session.execute(
                    select(CitationBinding, ContextMembership)
                    .join(
                        ContextMembership,
                        ContextMembership.id == CitationBinding.membership_id,
                    )
                    .where(
                        CitationBinding.run_id == run_id,
                        CitationBinding.provider_attempt_id == next(iter(provider_attempt_ids)),
                        CitationBinding.context_ledger_id == next(iter(context_ledger_ids)),
                    )
                    .order_by(ContextMembership.payload_ordinal)
                )
            ).all()
            payload_ordinals = [
                membership.payload_ordinal for _, membership in payload_binding_rows
            ]
            if (
                len(payload_binding_rows) != len(all_evidence)
                or payload_ordinals != list(range(len(all_evidence)))
                or not cited_binding_ids <= {binding.id for binding, _ in payload_binding_rows}
            ):
                raise CitationPublicationConflict("publication_context_membership_changed")
            membership_root_inputs = []
            for binding, membership in payload_binding_rows:
                fragment_hash = _hash(
                    project_context_evidence(
                        all_evidence[membership.payload_ordinal],
                        citation_binding_id=binding.id,
                        projection_version=str(projection_version),
                    )
                )
                if membership.serialized_evidence_fragment_hash != fragment_hash:
                    raise CitationPublicationConflict("publication_context_membership_changed")
                membership_root_inputs.append(
                    {
                        "payload_ordinal": membership.payload_ordinal,
                        "citation_binding_id": binding.id,
                        "fragment_hash": fragment_hash,
                    }
                )
            ordered_membership_root_hash = _hash(membership_root_inputs)
            if any(
                membership.ordered_membership_root_hash != ordered_membership_root_hash
                for _, membership in payload_binding_rows
            ):
                raise CitationPublicationConflict("publication_context_membership_changed")
        # Select evidence by the cited ContextMembership identity, not merely by
        # chunk_id. A bounded replan may retrieve the same chunk in more than
        # one tool round. Only one of those immutable payload occurrences is
        # cited by the final claim; including every duplicate makes the later
        # membership identity check compare the right binding with the wrong
        # occurrence and incorrectly rolls a valid answer back.
        cited_evidence_rows = sorted(
            (
                (membership.payload_ordinal, binding, membership, trace)
                for binding, membership, trace, _invocation, _observation in binding_rows
            ),
            key=lambda row: row[0],
        )
        cited_evidence = [
            (all_evidence[payload_ordinal], binding, membership, trace)
            for payload_ordinal, binding, membership, trace in cited_evidence_rows
        ]
        evidence = [item for item, _binding, _membership, _trace in cited_evidence]
        if cited_chunk_ids != {item.get("chunk_id") for item in evidence}:
            raise CitationPublicationConflict("publication_citation_not_in_context")
        locator_map: dict[str, dict[str, Any]] = {}
        parsed_evidence: list[
            tuple[
                dict[str, Any],
                SourceLocatorV2,
                SourceLocatorV2,
                CitationBinding,
                ContextMembership,
                RetrievalTrace,
            ]
        ] = []
        for item, binding, membership, trace in cited_evidence:
            locator = _LOCATOR_ADAPTER.validate_python(item.get("source_locator"))
            chunk_locator = _LOCATOR_ADAPTER.validate_python(item.get("chunk_locator"))
            if not isinstance(locator, SourceLocatorV2) or not isinstance(
                chunk_locator, SourceLocatorV2
            ):
                raise CitationPublicationConflict("publication_locator_schema_outdated")
            if locator.document_internal_id != chunk_locator.document_internal_id:
                raise CitationPublicationConflict("publication_locator_document_mismatch")
            parsed_evidence.append(
                (item, locator, chunk_locator, binding, membership, trace)
            )

        source_rows = (
            await self.session.execute(
                select(KnowledgeChunk, KnowledgeDocument, KnowledgeIngestRun)
                .join(
                    KnowledgeDocument,
                    (KnowledgeDocument.id == KnowledgeChunk.document_id)
                    & (KnowledgeDocument.index_version == KnowledgeChunk.index_version)
                    & (KnowledgeDocument.ingest_run_id == KnowledgeChunk.ingest_run_id),
                )
                .join(
                    KnowledgeIngestRun,
                    (KnowledgeIngestRun.id == KnowledgeChunk.ingest_run_id)
                    & (KnowledgeIngestRun.index_version == KnowledgeChunk.index_version),
                )
                .where(
                    KnowledgeChunk.chunk_key.in_(cited_chunk_ids or {"<none>"}),
                    KnowledgeIngestRun.status == "succeeded",
                )
            )
        ).all()
        source_map = {
            (chunk.chunk_key, ingest.id, ingest.index_version, document.id): (
                chunk,
                document,
                ingest,
            )
            for chunk, document, ingest in source_rows
        }
        for item, locator, chunk_locator, binding, membership, trace in parsed_evidence:
            source = source_map.get(
                (
                    str(item.get("chunk_id")),
                    locator.corpus_snapshot_id,
                    locator.index_version,
                    locator.document_internal_id,
                )
            )
            if source is None:
                raise CitationPublicationConflict("publication_source_is_ineligible")
            chunk, document, ingest = source
            if (
                binding.locator_hash != locator.locator_hash
                or bound_chunks[binding.id]
                != (chunk.chunk_key, chunk_locator.locator_hash)
            ):
                raise CitationPublicationConflict("citation_binding_incomplete")
            group, trace_filter = self._group_filter_for_candidate(
                trace,
                chunk_id=chunk.chunk_key,
                locator_hash=chunk_locator.locator_hash,
            )
            if trace_filter.scope_snapshot.tenant_id != binding.tenant_id:
                raise CitationPublicationConflict("publication_scope_origin_changed")
            await self._validate_scope_origin(trace_filter)
            filter_payload = trace_filter.model_dump(mode="json")
            filter_hash = _hash(filter_payload)
            matching_envelopes: list[EligibilityEnvelope] = []
            for raw_envelope in trace.eligibility_envelopes:
                if (
                    raw_envelope.get("chunk_id") != chunk.chunk_key
                    or raw_envelope.get("filter_hash") != filter_hash
                ):
                    continue
                try:
                    matching_envelopes.append(
                        EligibilityEnvelope.model_validate(raw_envelope)
                    )
                except ValidationError as exc:
                    raise CitationPublicationConflict(
                        "publication_eligibility_trace_mismatch"
                    ) from exc
            if len(matching_envelopes) != 1:
                raise CitationPublicationConflict("publication_eligibility_trace_mismatch")
            eligibility = matching_envelopes[0]
            selector_payload = trace_filter.temporal_selector.model_dump(mode="json")
            group_pipeline = self._pipeline_for_group(trace, group)
            try:
                selector = TEMPORAL_SELECTOR_ADAPTER.validate_python(selector_payload)
            except ValidationError as exc:
                raise CitationPublicationConflict("publication_temporal_selector_invalid") from exc
            if (
                binding.temporal_selector != selector_payload
                or trace.trace_logical_time != trace_filter.logical_time
                or trace_filter.index_version != trace.index_version
                or trace_filter.corpus_snapshot_id != trace.corpus_snapshot_id
                or trace_filter.eligibility_policy_version
                != group_pipeline.get("eligibility")
                or trace_filter.pipeline_contract_hash != _hash(group_pipeline)
            ):
                raise CitationPublicationConflict("publication_filter_contract_invalid")
            allowed_status, claim_time = self._publication_temporal_contract(
                group=group,
                filters=trace_filter,
                trace_logical_time=trace.trace_logical_time,
                publication_checked_at=publication_checked_at,
            )
            if (
                document is None
                or chunk is None
                or ingest is None
                or document.id != locator.document_internal_id
                or document.document_key != locator.document_key
                or document.version != locator.document_version
                or document.ingest_run_id != locator.corpus_snapshot_id
                or chunk.ingest_run_id != locator.corpus_snapshot_id
                or ingest.id != locator.corpus_snapshot_id
                or document.index_version != locator.index_version
                or chunk.index_version != locator.index_version
                or ingest.index_version != locator.index_version
                or document.status not in allowed_status
                or document.content_hash != locator.source_hash
                or chunk.content_hash != item.get("content_hash")
                or chunk.locator_hash != chunk_locator.locator_hash
                or chunk.locator_schema_version != "source-locator.v2"
                or chunk.canonicalization_version != locator.canonicalization_version
                or chunk.chunker_fingerprint != locator.chunker_fingerprint
                or chunk.embedding_fingerprint != locator.embedding_fingerprint
            ):
                raise CitationPublicationConflict("publication_source_is_ineligible")
            try:
                chunk_locator.resolve(document.canonical_blob)
                locator.resolve(document.canonical_blob)
            except ValueError as exc:
                raise CitationPublicationConflict("publication_span_hash_mismatch") from exc
            if (
                locator.byte_start < chunk_locator.byte_start
                or locator.byte_end > chunk_locator.byte_end
            ):
                raise CitationPublicationConflict("publication_span_outside_chunk")
            if (
                membership.payload_ordinal >= len(all_evidence)
                or all_evidence[membership.payload_ordinal] is not item
                or trace.index_version != locator.index_version
            ):
                raise CitationPublicationConflict("publication_eligibility_trace_mismatch")
            logical_time = eligibility.logical_time
            effective_at = document.effective_from
            if effective_at.tzinfo is None:
                effective_at = effective_at.replace(tzinfo=logical_time.tzinfo)
            effective_until = document.effective_until
            if effective_until is not None and effective_until.tzinfo is None:
                effective_until = effective_until.replace(tzinfo=logical_time.tzinfo)
            interval_matches = claim_time is None or (
                effective_at <= claim_time
                and (effective_until is None or claim_time < effective_until)
            )
            if (
                eligibility.corpus_snapshot_id != ingest.id
                or eligibility.index_version != ingest.index_version
                or eligibility.document_internal_id != document.id
                or eligibility.chunk_id != chunk.chunk_key
                or eligibility.status != document.status
                or eligibility.authority_level != document.authority_level
                or eligibility.applicable_plan != document.applicable_plan
                or eligibility.applicable_region != document.applicable_region
                or eligibility.effective_from != effective_at
                or eligibility.effective_until != effective_until
                or eligibility.filter_hash != filter_hash
                or eligibility.outcome != "eligible"
                or not interval_matches
                or document.authority_level < trace_filter.minimum_authority
                or document.applicable_plan not in (None, trace_filter.plan)
                or document.applicable_region not in (None, trace_filter.region)
                or (
                    selector.mode in {"version", "version_as_of"}
                    and document.version != getattr(selector, "historical_version", None)
                )
            ):
                raise CitationPublicationConflict("publication_eligibility_changed")
            locator_map[locator.locator_hash] = item

        answer_hash = hashlib.sha256(str(final.get("answer", "")).encode()).hexdigest()
        observations = {
            source.get("source_id")
            for observation in state.get("tool_observations", [])
            for source in observation.get("source_refs", [])
        }
        claim_context_rows = (
            await self.session.execute(
                select(ClaimRecord, ContextLedger)
                .join(ContextLedger, ContextLedger.id == ClaimRecord.context_ledger_id)
                .where(ClaimRecord.run_id == run_id, ContextLedger.run_id == run_id)
            )
        ).all()
        for claim in claims:
            binding_refs = set(claim.get("citation_binding_ids", []))
            knowledge_refs = set(claim.get("knowledge_locator_hashes", []))
            observation_refs = set(claim.get("observation_source_ids", []))
            if not knowledge_refs <= set(locator_map) or not observation_refs <= observations:
                raise CitationPublicationConflict("publication_claim_support_changed")
            matching = [
                record
                for record, _context in claim_context_rows
                if record.claim_text == claim.get("text")
                and record.answer_hash == answer_hash
                and set(record.support_refs.get("knowledge_locator_hashes", [])) == knowledge_refs
                and set(record.support_refs.get("citation_binding_ids", [])) == binding_refs
                and set(record.support_refs.get("observation_source_ids", [])) == observation_refs
            ]
            if len(matching) != 1:
                raise CitationPublicationConflict("publication_claim_not_registered")
            contexts = [
                context for record, context in claim_context_rows if record.id == matching[0].id
            ]
            context = contexts[0] if len(contexts) == 1 else None
            if context is None or any(
                bindings[binding_id][0].provider_attempt_id != matching[0].provider_attempt_id
                or bindings[binding_id][0].context_ledger_id != context.id
                for binding_id in binding_refs
            ):
                raise CitationPublicationConflict("citation_binding_wrong_provider_attempt")
            sections = context.component_manifest.get("sections", []) if context is not None else []
            evidence_section = next(
                (item for item in sections if item.get("name") == "retrieved_evidence"),
                None,
            )
            if evidence_section is None or (
                evidence_section.get("canonical_lineage_hash")
                or evidence_section.get("content_hash")
            ) != _hash(state.get("evidence", [])):
                raise CitationPublicationConflict("publication_context_membership_changed")
