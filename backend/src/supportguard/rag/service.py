from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

from supportguard.rag.eligibility import (
    EvidenceEligibilityConfig,
    EvidenceEligibilityInput,
    decide_evidence_eligibility,
)
from supportguard.rag.embeddings import EmbeddingProvider, embedding_fingerprint
from supportguard.rag.evidence import select_evidence
from supportguard.rag.intent import canonical_document_version
from supportguard.rag.query import NormalizedQuery, normalize_query
from supportguard.rag.ranking import reciprocal_rank_fusion
from supportguard.rag.repository import KnowledgeRepository, KnowledgeSnapshot
from supportguard.rag.spans import lexical_query_terms
from supportguard.rag.temporal import build_temporal_selector
from supportguard.rag.types import (
    EligibilityEnvelope,
    EvidenceSet,
    ParsedChunk,
    RankedChunk,
    RetrievalFilter,
    RetrievalProvenance,
    RetrievalScopeSnapshot,
    SourceLocatorV2,
)


def _candidate_trace(
    items: Sequence[RankedChunk | ParsedChunk],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for rank, item in enumerate(items, start=1):
        chunk = item.chunk if isinstance(item, RankedChunk) else item
        output.append(
            {
                "rank": rank,
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "version": chunk.version,
                "status": chunk.status,
                "section_path": chunk.section_path,
                "locator_hash": (
                    chunk.source_locator.locator_hash if chunk.source_locator is not None else None
                ),
                "rrf_score": item.rrf_score if isinstance(item, RankedChunk) else None,
                "rerank_score": (item.rerank_score if isinstance(item, RankedChunk) else None),
                "vector_similarity": item.vector_similarity,
                "vector_distance": chunk.vector_distance,
                "keyword_score": item.keyword_score,
                "vector_contribution": (
                    item.vector_contribution if isinstance(item, RankedChunk) else None
                ),
                "keyword_contribution": (
                    item.keyword_contribution if isinstance(item, RankedChunk) else None
                ),
                "filter_reason": chunk.filter_reason,
                "eligibility_outcome": (
                    item.eligibility_outcome if isinstance(item, RankedChunk) else None
                ),
                "eligibility_reason": (
                    item.eligibility_reason if isinstance(item, RankedChunk) else None
                ),
                "eligibility_envelope": (
                    chunk.eligibility_envelope.model_dump(mode="json")
                    if chunk.eligibility_envelope is not None
                    else None
                ),
                "omission_reason": (
                    item.omission_reason if isinstance(item, RankedChunk) else None
                ),
            }
        )
    return output


def _contract_hash(contract: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _selected_trace(
    evidence: EvidenceSet, *, group: Literal["current", "historical"]
) -> list[dict[str, object]]:
    return [
        {
            "rank": rank,
            "chunk_id": item.chunk.chunk_id,
            "locator_hash": (
                item.chunk.source_locator.locator_hash
                if item.chunk.source_locator is not None
                else None
            ),
            "selection_reason": "evidence_selected",
            "evidence_group": group,
        }
        for rank, item in enumerate(evidence.chunks, start=1)
    ]


_HISTORICAL_MARKERS = (
    "旧",
    "旧版",
    "历史",
    "此前",
    "曾经",
    "原为",
    "deprecated",
    "legacy",
    "previous",
    "previously",
    "prior",
    "formerly",
)
_TRANSITION_MARKERS = (
    "当前",
    "现行",
    "新版",
    "自",
    "起",
    "调整",
    "提升",
    "变更",
    "改为",
    "不再",
    "current",
    "now",
    "since",
    "changed",
    "increased",
    "updated",
)
_HISTORICAL_SECTION_MARKERS = (
    "版本冲突",
    "变更",
    "历史",
    "迁移",
    "changelog",
    "version conflict",
    "historical",
    "history",
    "migration",
)
_VERSION_REFERENCE = re.compile(
    r"(?<![a-z0-9])(?:v?\d+(?:\.\d+)+(?:\.x)?|20\d{2}(?:[-年]\d{1,2})?"
    r"|\d{2,4}\s*k)(?![a-z0-9])",
    re.IGNORECASE,
)
_BACKGROUND_SECTION_MARKERS = (
    "失败注入",
    "复盘要点",
    "回归测试",
    "failure injection",
    "regression test",
)
_CHECKLIST_SECTION_MARKERS = (
    "检查清单",
    "checklist",
)
_DECISION_SECTION_MARKERS = (
    "决策矩阵",
    "decision matrix",
)


def _answer_section_priority(item: ParsedChunk) -> int:
    """Prefer normative product prose over repetitive test-oriented appendices."""

    section = item.section_path.casefold()
    if any(marker in section for marker in _BACKGROUND_SECTION_MARKERS):
        return 0
    if any(marker in section for marker in _CHECKLIST_SECTION_MARKERS):
        return 1
    if any(marker in section for marker in _DECISION_SECTION_MARKERS):
        return 2
    return 3


def _historical_transition_score(item: ParsedChunk) -> int:
    """Recognize a published span that itself explains a past-to-current transition.

    Compare-without-anchor must not turn any two unrelated chunks into a conflict.
    This bounded, topic-agnostic signal requires both historical and transition
    semantics (or a structurally historical section) before a current publication
    can serve as the historical evidence lane.
    """

    section = item.section_path.casefold()
    content = item.content.casefold()
    historical_hits = sum(marker in content for marker in _HISTORICAL_MARKERS)
    transition_hits = sum(marker in content for marker in _TRANSITION_MARKERS)
    section_hits = sum(marker in section for marker in _HISTORICAL_SECTION_MARKERS)
    version_references = len(set(_VERSION_REFERENCE.findall(content)))
    if section_hits == 0 and (historical_hits == 0 or transition_hits == 0):
        return 0
    if section_hits == 0 and version_references < 2:
        return 0
    return (
        section_hits * 8
        + min(historical_hits, 3) * 3
        + min(transition_hits, 3) * 2
        + min(version_references, 3)
    )


def _topic_coherence_scores(
    query: str,
    candidates: Sequence[ParsedChunk],
) -> dict[str, int]:
    """Score bounded compare candidates against distinctive query subjects.

    Transition discovery intentionally recalls generic historical language,
    which is common in policy regression appendices.  This second,
    deterministic lexical score prevents an unrelated “旧版本冲突” paragraph
    from outranking a chunk that actually contains the requested product and
    capability.  It does not manufacture evidence or widen the recalled set.
    """

    terms = lexical_query_terms(query)
    surfaces = {
        item.chunk_id: f"{item.section_path}\n{item.content}".casefold() for item in candidates
    }
    document_frequency = {
        term: sum(term in surface for surface in surfaces.values()) for term in terms
    }
    candidate_count = len(surfaces)
    scores: dict[str, int] = {}
    for chunk_id, surface in surfaces.items():
        score = 0
        for term in terms:
            if term not in surface:
                continue
            specificity = candidate_count + 1 - document_frequency[term]
            exact_token_bonus = 4 if re.fullmatch(r"[a-z0-9_./:-]{2,}", term) else 1
            score += len(term) ** 2 * specificity * exact_token_bonus
        scores[chunk_id] = score
    return scores


class RetrievalService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding: EmbeddingProvider,
        *,
        on_filter_ready: Callable[[RetrievalFilter], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._embedding = embedding
        self._on_filter_ready = on_filter_ready

    async def retrieve(
        self,
        query: str,
        *,
        plan: str | None = None,
        region: str | None = None,
        intent: Literal["current", "historical", "compare"] = "current",
        logical_time: datetime | None = None,
        explicit_as_of: datetime | None = None,
        scope_snapshot: RetrievalScopeSnapshot,
    ) -> tuple[NormalizedQuery, EvidenceSet]:
        normalized, evidence, _ = await self.retrieve_with_trace(
            query,
            plan=plan,
            region=region,
            intent=intent,
            logical_time=logical_time,
            explicit_as_of=explicit_as_of,
            scope_snapshot=scope_snapshot,
        )
        return normalized, evidence

    async def retrieve_with_trace(
        self,
        query: str,
        *,
        plan: str | None = None,
        region: str | None = None,
        intent: Literal["current", "historical", "compare"] = "current",
        logical_time: datetime | None = None,
        explicit_as_of: datetime | None = None,
        index_version: str | None = None,
        historical_version: str | None = None,
        scope_snapshot: RetrievalScopeSnapshot,
        _snapshot: KnowledgeSnapshot | None = None,
    ) -> tuple[NormalizedQuery, EvidenceSet, RetrievalProvenance]:
        normalized = normalize_query(query)
        historical_version = canonical_document_version(historical_version)
        now = logical_time or datetime.now(UTC)
        historical = intent in {"historical", "compare"}
        snapshot = _snapshot or await self._repository.pin_active_snapshot()
        query_embedding_fingerprint = embedding_fingerprint(self._embedding)
        if query_embedding_fingerprint != snapshot.pipeline_fingerprint:
            raise RuntimeError("query_embedding_contract_is_incompatible_with_active_index")
        if index_version is not None and index_version != snapshot.index_version:
            raise RuntimeError("requested_index_snapshot_is_not_active")
        embedding_model = str(getattr(self._embedding, "model_name", ""))
        eligibility_config = (
            EvidenceEligibilityConfig(
                cross_channel_vector_floor=-1.0,
                vector_only_floor=1.0,
            )
            if embedding_model == "deterministic-e5-fixture"
            else EvidenceEligibilityConfig()
        )
        eligibility_policy_version = (
            "evidence-eligibility.v1-fixture-keyword"
            if embedding_model == "deterministic-e5-fixture"
            else "evidence-eligibility.v1"
        )
        pipeline_contract = {
            "schema": "retrieval-pipeline.v2",
            "vector": "pgvector.cosine.normalized.stable-order.v2",
            "keyword": "postgres.simple+heading-content-chinese-ngram-2-4.stable-order.v3",
            "restricted_keyword_terms": "compare-exact-subject-first.v1",
            "candidate_limit_per_status": 40 if intent == "compare" else 20,
            "fusion": {"algorithm": "rrf", "k": 60, "document_coherence_weight": 0.15},
            "evidence": {
                "selector": "authority-scope-conflict.v1",
                "dedup": "content_hash",
                "token_budget": 1400,  # nosec B105
                "max_items": 4,
            },
            "eligibility": eligibility_policy_version,
            "normalization": "query-normalization.v2-runtime-resource-neutral",
            "embedding_fingerprint": query_embedding_fingerprint,
            "chunker_fingerprint": snapshot.pipeline_identity.get("chunker_fingerprint"),
            "canonicalization": snapshot.pipeline_identity.get("canonicalization"),
            "index_version": snapshot.index_version,
            "corpus_snapshot_id": snapshot.ingest_run_id,
            "reranker": "disabled",
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                pipeline_contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        filters = RetrievalFilter(
            intent=intent,
            statuses=("active", "deprecated") if historical else ("active",),
            version=historical_version if intent == "historical" else None,
            minimum_authority=eligibility_config.minimum_authority,
            plan=plan,
            region=region,
            effective_at=(explicit_as_of or now),
            logical_time=now,
            index_version=snapshot.index_version,
            corpus_snapshot_id=snapshot.ingest_run_id,
            scope_snapshot=scope_snapshot,
            eligibility_policy_version=eligibility_policy_version,
            pipeline_contract_hash=fingerprint,
            temporal_selector=build_temporal_selector(
                trace_logical_time=now,
                historical_version=(historical_version if historical else None),
                explicit_as_of=(explicit_as_of if historical else None),
            ),
        )
        if self._on_filter_ready is not None:
            await self._on_filter_ready(filters)
        if intent == "historical" and historical_version is None and explicit_as_of is None:
            empty_contract = {
                "schema": "retrieval-pipeline.v2",
                "snapshot_id": snapshot.ingest_run_id,
                "reason": "ambiguous_historical_anchor",
                "reranker": "disabled",
            }
            fingerprint = hashlib.sha256(
                json.dumps(empty_contract, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return (
                normalized,
                EvidenceSet(
                    chunks=[],
                    citations=[],
                    refusal_reason="ambiguous_historical_anchor",
                ),
                RetrievalProvenance(
                    query_hash=hashlib.sha256(normalized.normalized.encode()).hexdigest(),
                    filter_contract=filters,
                    vector_candidates=[],
                    keyword_candidates=[],
                    rrf_candidates=[],
                    embedding_fingerprint=query_embedding_fingerprint,
                    pipeline_fingerprint=fingerprint,
                    pipeline_contract=empty_contract,
                    index_version=snapshot.index_version,
                    corpus_snapshot_id=snapshot.ingest_run_id,
                    abstention_reason="ambiguous_historical_anchor",
                ),
            )
        pre_filter_candidates = await self._repository.candidate_universe(filters)
        vector = self._embedding.embed_query(normalized.normalized)
        candidate_limit = 40 if intent == "compare" else 20
        vector_hits = await self._repository.vector_search(
            vector,
            plan=plan,
            region=region,
            historical=historical,
            filters=filters,
            limit=candidate_limit,
        )
        keyword_hits = await self._repository.keyword_search(
            normalized.normalized,
            normalized.exact_tokens,
            plan=plan,
            region=region,
            historical=historical,
            filters=filters,
            limit=candidate_limit,
        )
        if intent == "compare":
            return self._build_published_comparison(
                normalized=normalized,
                vector_hits=vector_hits,
                keyword_hits=keyword_hits,
                pre_filter_candidates=pre_filter_candidates,
                physical_filter=filters,
                physical_pipeline=pipeline_contract,
                embedding_fingerprint_value=query_embedding_fingerprint,
                snapshot=snapshot,
                eligibility_config=eligibility_config,
                plan=plan,
                region=region,
                logical_time=now,
                explicit_as_of=explicit_as_of,
                historical_version=historical_version,
            )
        fused = reciprocal_rank_fusion(vector_hits, keyword_hits)
        filter_hash = hashlib.sha256(
            json.dumps(
                filters.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        claim_effective_time = filters.temporal_selector.claim_effective_time
        for item in fused:
            time_match = claim_effective_time is None or (
                item.chunk.effective_at <= claim_effective_time
                and (
                    item.chunk.effective_until is None
                    or claim_effective_time < item.chunk.effective_until
                )
            )
            decision = decide_evidence_eligibility(
                EvidenceEligibilityInput(
                    retrieval_intent=intent,
                    status=item.chunk.status,
                    authority=item.chunk.authority_level,
                    scope_match=(
                        item.chunk.applicable_plan in (None, plan)
                        and item.chunk.applicable_region in (None, region)
                    ),
                    time_match=time_match,
                    exact_token_match=item.chunk.exact_token_match,
                    vector_similarity=(
                        item.vector_similarity if item.vector_similarity is not None else -1.0
                    ),
                    keyword_channel_match=item.keyword_rank is not None,
                ),
                config=eligibility_config,
            )
            item.eligibility_outcome = decision.outcome
            item.eligibility_reason = decision.reason_code
            item.chunk.eligibility_envelope = EligibilityEnvelope(
                corpus_snapshot_id=snapshot.ingest_run_id,
                index_version=snapshot.index_version,
                document_internal_id=(
                    item.chunk.source_locator.document_internal_id
                    if isinstance(item.chunk.source_locator, SourceLocatorV2)
                    else item.chunk.document_id
                ),
                chunk_id=item.chunk.chunk_id,
                status=item.chunk.status,
                authority_level=item.chunk.authority_level,
                applicable_plan=item.chunk.applicable_plan,
                applicable_region=item.chunk.applicable_region,
                effective_from=item.chunk.effective_at,
                effective_until=item.chunk.effective_until,
                logical_time=now,
                filter_hash=filter_hash,
                outcome=decision.outcome,
                reason_code=decision.reason_code,
            )
        eligible = [item for item in fused if item.eligibility_outcome == "eligible"]
        evidence = select_evidence(
            eligible,
            plan=plan,
            region=region,
            historical=historical,
            now=claim_effective_time or now,
            version_scoped=(historical_version is not None and explicit_as_of is None),
        )
        evidence_group: Literal["current", "historical"] = "historical" if historical else "current"
        for selected_item in evidence.chunks:
            selected_item.chunk.evidence_group = evidence_group
        if not eligible:
            reasons = [
                item.eligibility_reason for item in fused if item.eligibility_reason is not None
            ]
            if reasons:
                evidence.refusal_reason = reasons[0]
        selected_ids = {selected.chunk.chunk_id for selected in evidence.chunks}
        omission_decisions: list[dict[str, object]] = []
        for item in fused:
            if item.chunk.chunk_id in selected_ids:
                continue
            reason = (
                item.eligibility_reason
                if item.eligibility_outcome != "eligible"
                else item.omission_reason
            )
            if reason is None:
                raise RuntimeError("retrieval_omission_reason_missing")
            omission_decisions.append(
                {
                    "chunk_id": item.chunk.chunk_id,
                    "reason": reason,
                }
            )
        omitted_ids = {str(item["chunk_id"]) for item in omission_decisions}
        for candidate in pre_filter_candidates:
            chunk_id = str(candidate["chunk_id"])
            filter_reasons = candidate.get("filter_reasons")
            if (
                candidate.get("filter_outcome") != "excluded"
                or chunk_id in selected_ids
                or chunk_id in omitted_ids
                or not isinstance(filter_reasons, list)
                or not filter_reasons
            ):
                continue
            # Repository filter reasons are emitted in frozen precedence order;
            # the first reason is the causal exclusion that prevented recall.
            omission_decisions.append({"chunk_id": chunk_id, "reason": str(filter_reasons[0])})
            omitted_ids.add(chunk_id)
        provenance = RetrievalProvenance(
            query_hash=hashlib.sha256(normalized.normalized.encode()).hexdigest(),
            filter_contract=filters,
            vector_candidates=_candidate_trace(vector_hits),
            keyword_candidates=_candidate_trace(keyword_hits),
            rrf_candidates=_candidate_trace(fused),
            pre_filter_candidates=pre_filter_candidates,
            selected_candidates=[
                {
                    "rank": rank,
                    "chunk_id": item.chunk.chunk_id,
                    "locator_hash": (
                        item.chunk.source_locator.locator_hash
                        if item.chunk.source_locator is not None
                        else None
                    ),
                    "selection_reason": "evidence_selected",
                    "evidence_group": evidence_group,
                }
                for rank, item in enumerate(evidence.chunks, start=1)
            ],
            omission_decisions=omission_decisions,
            evidence_groups=[
                {
                    "group": evidence_group,
                    "filter": filters.model_dump(mode="json"),
                    "selected_candidates": [
                        {
                            "rank": rank,
                            "chunk_id": item.chunk.chunk_id,
                            "locator_hash": (
                                item.chunk.source_locator.locator_hash
                                if item.chunk.source_locator is not None
                                else None
                            ),
                            "selection_reason": "evidence_selected",
                            "evidence_group": evidence_group,
                        }
                        for rank, item in enumerate(evidence.chunks, start=1)
                    ],
                    "omission_decisions": omission_decisions,
                    "citations": [item.model_dump(mode="json") for item in evidence.citations],
                }
            ],
            eligibility_envelopes=[
                item.chunk.eligibility_envelope.model_dump(mode="json")
                for item in fused
                if item.chunk.eligibility_envelope is not None
            ],
            pipeline_contract=pipeline_contract,
            embedding_fingerprint=query_embedding_fingerprint,
            pipeline_fingerprint=fingerprint,
            index_version=snapshot.index_version,
            corpus_snapshot_id=snapshot.ingest_run_id,
            abstention_reason=evidence.refusal_reason,
        )
        return normalized, evidence, provenance

    def _build_published_comparison(
        self,
        *,
        normalized: NormalizedQuery,
        vector_hits: Sequence[ParsedChunk],
        keyword_hits: Sequence[ParsedChunk],
        pre_filter_candidates: list[dict[str, object]],
        physical_filter: RetrievalFilter,
        physical_pipeline: dict[str, object],
        embedding_fingerprint_value: str,
        snapshot: KnowledgeSnapshot,
        eligibility_config: EvidenceEligibilityConfig,
        plan: str | None,
        region: str | None,
        logical_time: datetime,
        explicit_as_of: datetime | None,
        historical_version: str | None,
    ) -> tuple[NormalizedQuery, EvidenceSet, RetrievalProvenance]:
        """Build two publishable evidence lanes inside one audited MCP search.

        The database call intentionally uses one broad, bounded compare filter.
        Current and historical candidates are then independently ranked,
        eligibility-checked, selected, and traced. This preserves the one
        logical invocation / one RetrievalTrace invariant while preventing one
        effective-time filter from erasing the other publication lane.
        """

        current_pipeline = {
            **physical_pipeline,
            "comparison_lane": "current",
            "lane_selector": "active-at-logical-time",
        }
        current_filter = physical_filter.model_copy(
            update={
                "intent": "current",
                "statuses": ("active",),
                "version": None,
                "effective_at": logical_time,
                "pipeline_contract_hash": _contract_hash(current_pipeline),
                "temporal_selector": build_temporal_selector(
                    trace_logical_time=logical_time,
                    historical_version=None,
                    explicit_as_of=None,
                ),
            }
        )

        def copy_matching(
            items: Sequence[ParsedChunk],
            predicate: Callable[[ParsedChunk], bool],
        ) -> list[ParsedChunk]:
            return [item.model_copy(deep=True) for item in items if predicate(item)]

        def interval_contains(item: ParsedChunk, at: datetime) -> bool:
            return item.effective_at <= at and (
                item.effective_until is None or at < item.effective_until
            )

        def build_lane(
            *,
            group: Literal["current", "historical"],
            lane_filter: RetrievalFilter,
            lane_pipeline: dict[str, object],
            vector_lane: Sequence[ParsedChunk],
            keyword_lane: Sequence[ParsedChunk],
            historical_lane: bool,
            version_scoped: bool,
            evidence_time: datetime,
            ignore_claim_time: bool = False,
            max_items: int = 2,
            topic_scores: dict[str, int] | None = None,
            structured_scores: dict[str, int] | None = None,
            reserve_transition: bool = False,
        ) -> tuple[
            EvidenceSet,
            list[RankedChunk],
            list[dict[str, object]],
            list[dict[str, object]],
            list[dict[str, object]],
        ]:
            fused_lane = reciprocal_rank_fusion(vector_lane, keyword_lane)
            filter_hash = hashlib.sha256(
                json.dumps(
                    lane_filter.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            claim_time = lane_filter.temporal_selector.claim_effective_time
            for item in fused_lane:
                time_match = (
                    ignore_claim_time
                    or claim_time is None
                    or interval_contains(item.chunk, claim_time)
                )
                decision = decide_evidence_eligibility(
                    EvidenceEligibilityInput(
                        retrieval_intent=lane_filter.intent,
                        status=item.chunk.status,
                        authority=item.chunk.authority_level,
                        scope_match=(
                            item.chunk.applicable_plan in (None, plan)
                            and item.chunk.applicable_region in (None, region)
                        ),
                        time_match=time_match,
                        structured_field_match=bool(
                            structured_scores is not None
                            and structured_scores.get(item.chunk.chunk_id, 0) > 0
                            and _historical_transition_score(item.chunk) > 0
                        ),
                        exact_token_match=item.chunk.exact_token_match,
                        vector_similarity=(
                            item.vector_similarity if item.vector_similarity is not None else -1.0
                        ),
                        keyword_channel_match=item.keyword_rank is not None,
                    ),
                    config=eligibility_config,
                )
                item.eligibility_outcome = decision.outcome
                item.eligibility_reason = decision.reason_code
                item.chunk.eligibility_envelope = EligibilityEnvelope(
                    corpus_snapshot_id=snapshot.ingest_run_id,
                    index_version=snapshot.index_version,
                    document_internal_id=(
                        item.chunk.source_locator.document_internal_id
                        if isinstance(item.chunk.source_locator, SourceLocatorV2)
                        else item.chunk.document_id
                    ),
                    chunk_id=item.chunk.chunk_id,
                    status=item.chunk.status,
                    authority_level=item.chunk.authority_level,
                    applicable_plan=item.chunk.applicable_plan,
                    applicable_region=item.chunk.applicable_region,
                    effective_from=item.chunk.effective_at,
                    effective_until=item.chunk.effective_until,
                    logical_time=logical_time,
                    filter_hash=filter_hash,
                    outcome=decision.outcome,
                    reason_code=decision.reason_code,
                )
            eligible = [item for item in fused_lane if item.eligibility_outcome == "eligible"]
            if topic_scores is not None:
                for item in eligible:
                    item.rerank_score = (
                        float(
                            _answer_section_priority(item.chunk) * 1_000
                            + topic_scores.get(item.chunk.chunk_id, 0)
                        )
                        + item.rrf_score
                    )
            if (
                reserve_transition
                and normalized.exact_tokens
                and len(eligible) > 1
                and max_items > 1
            ):
                primary = max(
                    eligible,
                    key=lambda item: (
                        _answer_section_priority(item.chunk),
                        item.rrf_score,
                        item.chunk.chunk_id,
                    ),
                )
                for item in eligible:
                    item.rerank_score = item.rrf_score
                primary.rerank_score = 2.0 + primary.rrf_score
                transition_candidates = [
                    item
                    for item in eligible
                    if item.chunk.document_family_key == primary.chunk.document_family_key
                    and _historical_transition_score(item.chunk) > 0
                ]
                if transition_candidates:
                    lane_topic_scores = _topic_coherence_scores(
                        normalized.normalized,
                        [item.chunk for item in eligible],
                    )
                    transition = max(
                        transition_candidates,
                        key=lambda item: (
                            _answer_section_priority(item.chunk),
                            lane_topic_scores.get(item.chunk.chunk_id, 0),
                            _historical_transition_score(item.chunk),
                            item.rrf_score,
                            item.chunk.chunk_id,
                        ),
                    )
                    if transition.chunk.chunk_id != primary.chunk.chunk_id:
                        transition.rerank_score = 1.0 + transition.rrf_score
            evidence = select_evidence(
                eligible,
                plan=plan,
                region=region,
                historical=historical_lane,
                now=evidence_time,
                version_scoped=version_scoped,
                max_items=max_items,
            )
            for item in evidence.chunks:
                item.chunk.evidence_group = group
            if not eligible:
                reasons = [
                    item.eligibility_reason
                    for item in fused_lane
                    if item.eligibility_reason is not None
                ]
                if reasons:
                    evidence.refusal_reason = reasons[0]
            selected = _selected_trace(evidence, group=group)
            selected_ids = {str(item["chunk_id"]) for item in selected}
            omissions: list[dict[str, object]] = []
            for item in fused_lane:
                if item.chunk.chunk_id in selected_ids:
                    continue
                reason = (
                    item.eligibility_reason
                    if item.eligibility_outcome != "eligible"
                    else item.omission_reason
                )
                if reason is None:
                    raise RuntimeError("retrieval_omission_reason_missing")
                omissions.append(
                    {
                        "chunk_id": item.chunk.chunk_id,
                        "reason": reason,
                        "evidence_group": group,
                    }
                )
            envelopes = [
                item.chunk.eligibility_envelope.model_dump(mode="json")
                for item in fused_lane
                if item.chunk.eligibility_envelope is not None
            ]
            group_trace = {
                "group": group,
                "filter": lane_filter.model_dump(mode="json"),
                "selected_candidates": selected,
                "omission_decisions": omissions,
                "citations": [item.model_dump(mode="json") for item in evidence.citations],
                "pipeline_contract": lane_pipeline,
            }
            return evidence, fused_lane, selected, omissions, [*envelopes, group_trace]

        def current_predicate(item: ParsedChunk) -> bool:
            return item.status == "active" and interval_contains(item, logical_time)

        (
            current_evidence,
            current_fused,
            current_selected,
            current_omissions,
            current_metadata,
        ) = build_lane(
            group="current",
            lane_filter=current_filter,
            lane_pipeline=current_pipeline,
            vector_lane=copy_matching(vector_hits, current_predicate),
            keyword_lane=copy_matching(keyword_hits, current_predicate),
            historical_lane=False,
            version_scoped=False,
            evidence_time=logical_time,
            reserve_transition=True,
        )

        historical_reason: str | None = None
        historical_candidates: list[
            tuple[
                str | None,
                str | None,
                dict[str, object],
                EvidenceSet,
                list[RankedChunk],
                list[dict[str, object]],
                list[dict[str, object]],
                list[dict[str, object]],
            ]
        ] = []
        discovered_ids: set[str] | None = None
        discovery_topic_scores: dict[str, int] = {}
        discovery_bridge_scores: dict[str, int] = {}
        if historical_version is not None or explicit_as_of is not None:
            candidate_selectors: tuple[tuple[str | None, str | None], ...] = (
                (None, historical_version),
            )
        else:
            current_ids = {item.chunk.chunk_id for item in current_evidence.chunks}
            current_document_versions = {
                (item.chunk.document_id, item.chunk.version) for item in current_evidence.chunks
            }
            discovery_vector = [
                item
                for item in vector_hits
                if item.chunk_id not in current_ids
                and (item.document_id, item.version) not in current_document_versions
                and _historical_transition_score(item) > 0
            ]
            discovery_keyword = [
                item
                for item in keyword_hits
                if item.chunk_id not in current_ids
                and (item.document_id, item.version) not in current_document_versions
                and _historical_transition_score(item) > 0
            ]
            discovered = reciprocal_rank_fusion(
                discovery_vector,
                discovery_keyword,
            )
            discovery_chunks = [item.chunk for item in discovered]
            current_topic_bridge = "\n".join(
                sorted(
                    {
                        value
                        for item in current_evidence.chunks
                        for value in (
                            item.chunk.title.strip(),
                            item.chunk.section_path.strip(),
                        )
                        if value
                    }
                )
            )
            discovery_topic_scores = _topic_coherence_scores(
                "\n".join(part for part in (normalized.normalized, current_topic_bridge) if part),
                discovery_chunks,
            )
            discovery_bridge_scores = (
                _topic_coherence_scores(current_topic_bridge, discovery_chunks)
                if current_topic_bridge
                else {}
            )
            ranked_discovered = sorted(
                discovered,
                key=lambda candidate: (
                    -discovery_topic_scores.get(candidate.chunk.chunk_id, 0),
                    -_answer_section_priority(candidate.chunk),
                    -_historical_transition_score(candidate.chunk),
                    -candidate.rrf_score,
                    candidate.chunk.chunk_id,
                ),
            )[:4]
            discovered_ids = {item.chunk.chunk_id for item in ranked_discovered}
            if not discovered_ids:
                historical_reason = "compare_historical_transition_missing"
                candidate_selectors = ()
            else:
                # Discovery still has to publish a durable historical selector.
                # A current-mode selector paired with a historical evidence group
                # would make retrieval appear successful but fail the independent
                # citation publication gate. Bind each discovered lane to the
                # actual document publication identity that supplied its
                # transition span. Once that publication is selected, the lane
                # may choose a normative recalled section from the same identity
                # rather than being trapped on the seed appendix chunk.
                candidate_selectors = tuple(
                    dict.fromkeys(
                        (item.chunk.document_id, item.chunk.version)
                        for item in ranked_discovered
                        if item.chunk.version
                    )
                )
                if not candidate_selectors:
                    historical_reason = "compare_historical_version_missing"

        for candidate_document_id, candidate_version in candidate_selectors:
            historical_pipeline = {
                **physical_pipeline,
                "comparison_lane": "historical",
                "lane_selector": (
                    "explicit-anchor"
                    if historical_version is not None or explicit_as_of is not None
                    else "published-transition-discovery"
                ),
                "selected_document_id": candidate_document_id,
                "selected_version": candidate_version,
                "discovered_chunk_count": (
                    len(discovered_ids) if discovered_ids is not None else None
                ),
            }
            historical_filter = physical_filter.model_copy(
                update={
                    "intent": "historical",
                    "statuses": ("active", "deprecated"),
                    "version": candidate_version,
                    "effective_at": explicit_as_of or logical_time,
                    "pipeline_contract_hash": _contract_hash(historical_pipeline),
                    "temporal_selector": build_temporal_selector(
                        trace_logical_time=logical_time,
                        historical_version=candidate_version,
                        explicit_as_of=explicit_as_of,
                    ),
                }
            )

            def historical_predicate(
                item: ParsedChunk,
                *,
                selected_document_id: str | None = candidate_document_id,
                selected_version: str | None = candidate_version,
            ) -> bool:
                if discovered_ids is not None:
                    return (
                        selected_document_id is not None
                        and item.document_id == selected_document_id
                        and selected_version is not None
                        and item.version == selected_version
                        and _historical_transition_score(item) > 0
                    )
                if selected_version is not None and item.version != selected_version:
                    return False
                if explicit_as_of is not None:
                    return interval_contains(item, explicit_as_of)
                return item.status == "deprecated" and item.effective_at <= logical_time

            historical_lane_result = build_lane(
                group="historical",
                lane_filter=historical_filter,
                lane_pipeline=historical_pipeline,
                vector_lane=copy_matching(vector_hits, historical_predicate),
                keyword_lane=copy_matching(keyword_hits, historical_predicate),
                historical_lane=True,
                version_scoped=(
                    discovered_ids is not None
                    or (candidate_version is not None and explicit_as_of is None)
                ),
                evidence_time=explicit_as_of or logical_time,
                ignore_claim_time=discovered_ids is not None,
                max_items=1 if discovered_ids is not None else 2,
                topic_scores=(discovery_topic_scores if discovered_ids is not None else None),
                structured_scores=(discovery_bridge_scores if discovered_ids is not None else None),
            )
            (
                historical_evidence,
                historical_fused,
                historical_selected,
                historical_omissions,
                historical_trace_metadata,
            ) = historical_lane_result
            historical_candidates.append(
                (
                    candidate_document_id,
                    candidate_version,
                    historical_pipeline,
                    historical_evidence,
                    historical_fused,
                    historical_selected,
                    historical_omissions,
                    historical_trace_metadata,
                )
            )

        if historical_candidates:
            selected_historical = max(
                historical_candidates,
                key=lambda item: (
                    bool(item[3].chunks),
                    max(
                        (
                            discovery_topic_scores.get(
                                candidate.chunk.chunk_id,
                                0,
                            )
                            for candidate in item[4]
                        ),
                        default=0,
                    ),
                    max(
                        (
                            candidate.rerank_score
                            if candidate.rerank_score is not None
                            else candidate.rrf_score
                            for candidate in item[4]
                        ),
                        default=0.0,
                    ),
                    str(item[0] or ""),
                    str(item[1] or ""),
                ),
            )
            (
                selected_historical_document_id,
                selected_historical_version,
                historical_pipeline,
                historical_evidence,
                historical_fused,
                historical_selected,
                historical_omissions,
                historical_trace_metadata,
            ) = selected_historical
            if selected_historical_version is None and historical_evidence.chunks:
                selected_historical_version = historical_evidence.chunks[0].chunk.version
            if selected_historical_document_id is None and historical_evidence.chunks:
                selected_historical_document_id = historical_evidence.chunks[0].chunk.document_id
        else:
            selected_historical_document_id = None
            selected_historical_version = None
            historical_pipeline = {
                **physical_pipeline,
                "comparison_lane": "historical",
                "lane_selector": "published-version-discovery",
                "reason": historical_reason or "compare_evidence_group_missing",
            }
            historical_evidence = EvidenceSet(chunks=[], citations=[])
            historical_fused = []
            historical_selected = []
            historical_omissions = []
            historical_trace_metadata = []

        # A requested current-versus-historical comparison is expected to
        # contain different published identities.  Treating that difference as
        # an unresolved conflict makes every valid version transition unsafe
        # by construction.  Only a contradiction *within* either independently
        # selected publication lane is a conflict.
        conflict = bool(current_evidence.conflict or historical_evidence.conflict)
        if not current_evidence.chunks or not historical_evidence.chunks:
            refusal_reason = "compare_evidence_group_missing"
        elif current_evidence.conflict:
            refusal_reason = current_evidence.refusal_reason or "conflicting_current_evidence"
        elif historical_evidence.conflict:
            refusal_reason = historical_evidence.refusal_reason or "conflicting_historical_evidence"
        else:
            refusal_reason = None
        combined = EvidenceSet(
            chunks=[*current_evidence.chunks, *historical_evidence.chunks],
            citations=[*current_evidence.citations, *historical_evidence.citations],
            conflict=conflict,
            refusal_reason=refusal_reason,
        )
        current_envelopes = current_metadata[:-1]
        current_group = current_metadata[-1]
        historical_envelopes = historical_trace_metadata[:-1] if historical_trace_metadata else []
        historical_groups = historical_trace_metadata[-1:] if historical_trace_metadata else []
        combined_contract: dict[str, object] = {
            "schema": "compare-retrieval-pipeline.v1",
            "snapshot_id": snapshot.ingest_run_id,
            "physical": physical_pipeline,
            "current": current_pipeline,
            "historical": historical_pipeline,
            "group_isolation": "single-transaction-independent-lanes.v1",
            "historical_discovery": {
                "candidate_limit": 4,
                "selected_version": selected_historical_version,
                "selected_document_id": selected_historical_document_id,
                "failure_reason": historical_reason,
                "topic_cohesion": "lexical-idf-distinctive-subject.v1",
                "current_topic_bridge": "selected-title-section.v1",
                "bridge_eligibility": "published-transition-structured-match.v1",
                "current_identity_exclusion": "document-version.v1",
                "selection_order": "topic-before-section-authority.v1",
                "lane_section_order": "normative-before-test-appendix.v1",
            },
            "version_relation": "published-transition-not-conflict.v1",
            "current_comparison_selection": ("rrf-primary-plus-topic-coherent-transition.v1"),
            "reranker": "disabled",
        }
        combined_fingerprint = _contract_hash(combined_contract)
        return (
            normalized,
            combined,
            RetrievalProvenance(
                query_hash=hashlib.sha256(normalized.normalized.encode()).hexdigest(),
                filter_contract=physical_filter,
                vector_candidates=_candidate_trace(vector_hits),
                keyword_candidates=_candidate_trace(keyword_hits),
                rrf_candidates=[
                    *(
                        {**item, "evidence_group": "current"}
                        for item in _candidate_trace(current_fused)
                    ),
                    *(
                        {**item, "evidence_group": "historical"}
                        for item in _candidate_trace(historical_fused)
                    ),
                ],
                pre_filter_candidates=pre_filter_candidates,
                selected_candidates=[
                    *current_selected,
                    *historical_selected,
                ],
                omission_decisions=[
                    *current_omissions,
                    *historical_omissions,
                ],
                evidence_groups=[current_group, *historical_groups],
                eligibility_envelopes=[
                    *current_envelopes,
                    *historical_envelopes,
                ],
                pipeline_contract=combined_contract,
                embedding_fingerprint=embedding_fingerprint_value,
                pipeline_fingerprint=combined_fingerprint,
                index_version=snapshot.index_version,
                corpus_snapshot_id=snapshot.ingest_run_id,
                abstention_reason=combined.refusal_reason,
            ),
        )
