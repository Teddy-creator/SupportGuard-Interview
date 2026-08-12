from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import KnowledgeChunk, KnowledgeDocument, KnowledgeIngestRun
from supportguard.rag.temporal import build_temporal_selector
from supportguard.rag.types import (
    ParsedChunk,
    RetrievalFilter,
    RetrievalScopeSnapshot,
    SourceLocatorV2,
)


def _direct_repository_scope(plan: str | None, region: str | None) -> RetrievalScopeSnapshot:
    """Non-publishable scope used only by direct repository compatibility calls."""

    return RetrievalScopeSnapshot(
        tenant_id="repository-direct",
        customer_id="repository-direct",
        subscription_id="repository-direct",
        subscription_version=1,
        plan=plan or "unknown",
        region_trace_id=("repository-direct" if region is not None else None),
        region_trace_version=(1 if region is not None else None),
        region=region,
    )


def _effective_time(filters: RetrievalFilter) -> datetime | None:
    # A compare retrieval owns one physical, audited search transaction but
    # evaluates current and historical publication lanes independently in the
    # service. Applying one claim time in SQL would erase the deprecated lane.
    if filters.intent == "compare":
        return None
    return filters.temporal_selector.claim_effective_time


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    ingest_run_id: str
    index_version: str
    pipeline_fingerprint: str
    pipeline_identity: dict[str, object]


def _keyword_terms(query: str, exact_tokens: tuple[str, ...]) -> tuple[str, ...]:
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    # PostgreSQL's `simple` text-search configuration does not segment Chinese.
    # Use bounded 2-4 character terms so the keyword channel can distinguish an
    # exact answer dimension such as “到账周期” from adjacent workflow prose that
    # merely shares broad words such as “申请” or “审批”.  Longest terms come first
    # and the cap keeps the restricted MCP function's work bounded for the public
    # 2,000-character tool-input limit.
    chinese_ngrams = [
        run[index : index + width]
        for run in chinese_runs
        for width in range(min(4, len(run)), 1, -1)
        for index in range(len(run) - width + 1)
    ]
    latin = [part for part in query.lower().split() if len(part) >= 2]
    return tuple(dict.fromkeys([*exact_tokens, *chinese_ngrams, *latin]))[:512]


def _restricted_keyword_terms(
    query: str,
    exact_tokens: tuple[str, ...],
    *,
    intent: str,
) -> tuple[str, ...]:
    """Keep exact-subject comparisons focused inside the bounded SQL capability."""

    if intent != "compare" or not exact_tokens:
        return _keyword_terms(query, exact_tokens)
    latin = re.findall(r"[a-z0-9][a-z0-9_./:-]*", query.lower())
    return tuple(dict.fromkeys([*exact_tokens, *latin]))[:64]


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pin_active_snapshot(self) -> KnowledgeSnapshot:
        statement = (
            select(KnowledgeIngestRun)
            .where(
                KnowledgeIngestRun.is_active.is_(True),
                KnowledgeIngestRun.status == "succeeded",
            )
            .order_by(KnowledgeIngestRun.created_at.desc())
        )
        # The immutable ingest-run id is the query snapshot. A read-only MCP role must
        # not acquire a table lock (PostgreSQL FOR SHARE requires UPDATE privilege).
        # Every downstream query is bound to the selected id + index version, so a
        # concurrent activation cannot mix snapshots inside this retrieval.
        runs = list((await self._session.scalars(statement)).all())
        if len(runs) != 1:
            raise RuntimeError("active_knowledge_snapshot_not_unique")
        if not runs[0].pipeline_fingerprint:
            raise RuntimeError("active_index_embedding_provenance_missing")
        return KnowledgeSnapshot(
            runs[0].id,
            runs[0].index_version,
            runs[0].pipeline_fingerprint,
            dict(runs[0].pipeline_identity or {}),
        )

    def _base(self, filters: RetrievalFilter) -> Select[tuple[KnowledgeChunk, KnowledgeDocument]]:
        statement = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .join(
                KnowledgeIngestRun,
                (KnowledgeIngestRun.id == KnowledgeChunk.ingest_run_id)
                & (KnowledgeIngestRun.index_version == KnowledgeChunk.index_version),
            )
            .where(
                KnowledgeIngestRun.status == "succeeded",
                KnowledgeIngestRun.id == filters.corpus_snapshot_id,
                KnowledgeIngestRun.index_version == filters.index_version,
                KnowledgeChunk.index_version == filters.index_version,
                KnowledgeChunk.ingest_run_id == filters.corpus_snapshot_id,
                KnowledgeDocument.index_version == filters.index_version,
                KnowledgeDocument.ingest_run_id == filters.corpus_snapshot_id,
                KnowledgeDocument.status.in_(filters.statuses),
                KnowledgeDocument.authority_level >= filters.minimum_authority,
                or_(
                    KnowledgeDocument.applicable_plan.is_(None),
                    KnowledgeDocument.applicable_plan == filters.plan,
                ),
                or_(
                    KnowledgeDocument.applicable_region.is_(None),
                    KnowledgeDocument.applicable_region == filters.region,
                ),
            )
        )
        if filters.version is not None:
            statement = statement.where(KnowledgeDocument.version == filters.version)
        effective_time = _effective_time(filters)
        if effective_time is not None:
            statement = statement.where(
                KnowledgeDocument.effective_from <= effective_time,
                or_(
                    KnowledgeDocument.effective_until.is_(None),
                    effective_time < KnowledgeDocument.effective_until,
                ),
            )
        return statement

    async def candidate_universe(
        self, filters: RetrievalFilter
    ) -> list[dict[str, object]]:
        """Record the snapshot candidate universe and every SQL eligibility decision."""
        rows = (
            await self._session.execute(
                select(KnowledgeChunk, KnowledgeDocument)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .join(
                    KnowledgeIngestRun,
                    (KnowledgeIngestRun.id == KnowledgeChunk.ingest_run_id)
                    & (KnowledgeIngestRun.index_version == KnowledgeChunk.index_version),
                )
                .where(
                    KnowledgeIngestRun.id == filters.corpus_snapshot_id,
                    KnowledgeIngestRun.status == "succeeded",
                    KnowledgeChunk.index_version == filters.index_version,
                    KnowledgeChunk.ingest_run_id == filters.corpus_snapshot_id,
                    KnowledgeDocument.index_version == filters.index_version,
                    KnowledgeDocument.ingest_run_id == filters.corpus_snapshot_id,
                )
                .order_by(KnowledgeDocument.document_key, KnowledgeChunk.sequence)
            )
        ).all()
        output: list[dict[str, object]] = []
        for chunk, document in rows:
            reasons: list[str] = []
            if document.status not in filters.statuses:
                reasons.append("status_filtered")
            if filters.version is not None and document.version != filters.version:
                reasons.append("version_filtered")
            effective_from = document.effective_from
            if effective_from.tzinfo is None:
                effective_from = effective_from.replace(tzinfo=UTC)
            effective_until = document.effective_until
            if effective_until is not None and effective_until.tzinfo is None:
                effective_until = effective_until.replace(tzinfo=UTC)
            effective_time = _effective_time(filters)
            if effective_time is not None and not (
                effective_from <= effective_time
                and (effective_until is None or effective_time < effective_until)
            ):
                reasons.append("effective_interval_filtered")
            if document.authority_level < filters.minimum_authority:
                reasons.append("authority_filtered")
            if document.applicable_plan not in (None, filters.plan):
                reasons.append("plan_scope_filtered")
            if document.applicable_region not in (None, filters.region):
                reasons.append("region_scope_filtered")
            output.append(
                {
                    "chunk_id": chunk.chunk_key,
                    "document_id": document.document_key,
                    "version": document.version,
                    "status": document.status,
                    "locator_hash": chunk.locator_hash,
                    "filter_outcome": "excluded" if reasons else "eligible",
                    "filter_reasons": reasons,
                }
            )
        return output

    @staticmethod
    def _parsed(chunk: KnowledgeChunk, document: KnowledgeDocument) -> ParsedChunk:
        effective_at = document.effective_from
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=UTC)
        effective_until = document.effective_until
        if effective_until is not None and effective_until.tzinfo is None:
            effective_until = effective_until.replace(tzinfo=UTC)
        return ParsedChunk(
            chunk_id=chunk.chunk_key,
            document_id=document.document_key,
            document_type=document.document_type,
            title=document.title,
            section_path=chunk.section_path,
            sequence=chunk.sequence,
            content=chunk.content,
            token_count=chunk.token_count,
            content_hash=chunk.content_hash,
            version=document.version,
            status=document.status,
            effective_at=effective_at,
            effective_until=effective_until,
            document_family_key=document.document_family_key,
            applicability_scope_hash=document.applicability_scope_hash,
            authority_level=document.authority_level,
            applicable_plan=document.applicable_plan,
            applicable_region=document.applicable_region,
            source_locator=SourceLocatorV2(
                document_key=document.document_key,
                document_internal_id=document.id,
                document_version=document.version,
                source_hash=document.content_hash,
                corpus_snapshot_id=chunk.ingest_run_id,
                index_version=chunk.index_version,
                canonicalization_version=chunk.canonicalization_version,
                section_path=chunk.section_path,
                byte_start=chunk.byte_start,
                byte_end=chunk.byte_end,
                span_hash=chunk.span_hash,
                chunker_fingerprint=chunk.chunker_fingerprint,
                embedding_fingerprint=chunk.embedding_fingerprint,
                locator_hash=chunk.locator_hash,
            ),
        )

    async def vector_search(
        self,
        vector: list[float],
        *,
        plan: str | None,
        region: str | None,
        historical: bool = False,
        filters: RetrievalFilter | None = None,
        limit: int = 20,
    ) -> list[ParsedChunk]:
        now = datetime.now(UTC)
        filters = filters or RetrievalFilter(
            intent="historical" if historical else "current",
            statuses=("active", "deprecated") if historical else ("active",),
            plan=plan,
            region=region,
            effective_at=now,
            logical_time=now,
            index_version=(snapshot := await self.pin_active_snapshot()).index_version,
            corpus_snapshot_id=snapshot.ingest_run_id,
            scope_snapshot=_direct_repository_scope(plan, region),
            eligibility_policy_version="evidence-eligibility.v1",
            pipeline_contract_hash="0" * 64,
            temporal_selector=build_temporal_selector(
                trace_logical_time=now,
                historical_version=None,
                explicit_as_of=None,
            ),
        )
        dialect = self._session.bind.dialect.name if self._session.bind is not None else "unknown"
        if dialect != "postgresql":
            rows = (await self._session.execute(self._base(filters))).all()
            scored_all = sorted(
                rows,
                key=lambda row: (
                    -sum(
                        left * right
                        for left, right in zip(row[0].embedding or [], vector, strict=False)
                    ),
                    -row[1].authority_level,
                    row[1].document_key,
                    row[0].sequence,
                    row[0].chunk_key,
                ),
            )
            if filters.intent == "compare":
                scored = [
                    row
                    for status in filters.statuses
                    for row in [
                        item for item in scored_all if item[1].status == status
                    ][:limit]
                ]
                scored.sort(
                    key=lambda row: (
                        -sum(
                            left * right
                            for left, right in zip(
                                row[0].embedding or [], vector, strict=False
                            )
                        ),
                        -row[1].authority_level,
                        row[1].document_key,
                        row[0].sequence,
                        row[0].chunk_key,
                    )
                )
            else:
                scored = scored_all[:limit]
            return [
                self._parsed(chunk, document).model_copy(
                    update={
                        "vector_similarity": sum(
                            left * right
                            for left, right in zip(chunk.embedding or [], vector, strict=False)
                        ),
                        "vector_distance": 1
                        - sum(
                            left * right
                            for left, right in zip(chunk.embedding or [], vector, strict=False)
                        ),
                        "channel_rank": rank,
                    }
                )
                for rank, (chunk, document) in enumerate(scored, start=1)
            ]
        distance = KnowledgeChunk.embedding.cosine_distance(vector).label("cosine_distance")
        if filters.intent == "compare":
            rows = []
            for status in filters.statuses:
                lane_filter = filters.model_copy(update={"statuses": (status,)})
                statement = (
                    self._base(lane_filter)
                    .add_columns(distance)
                    .order_by(
                        distance,
                        KnowledgeDocument.authority_level.desc(),
                        KnowledgeDocument.document_key,
                        KnowledgeChunk.sequence,
                        KnowledgeChunk.chunk_key,
                    )
                    .limit(limit)
                )
                rows.extend((await self._session.execute(statement)).all())
            rows.sort(
                key=lambda row: (
                    float(row[2]),
                    -row[1].authority_level,
                    row[1].document_key,
                    row[0].sequence,
                    row[0].chunk_key,
                )
            )
        else:
            statement = (
                self._base(filters)
                .add_columns(distance)
                .order_by(
                    distance,
                    KnowledgeDocument.authority_level.desc(),
                )
                .limit(limit)
            )
            rows = (await self._session.execute(statement)).all()
        return [
            self._parsed(chunk, document).model_copy(
                update={
                    "vector_distance": float(raw_distance),
                    "vector_similarity": 1.0 - float(raw_distance),
                    "channel_rank": rank,
                }
            )
            for rank, (chunk, document, raw_distance) in enumerate(rows, start=1)
        ]

    async def keyword_search(
        self,
        query: str,
        exact_tokens: tuple[str, ...],
        *,
        plan: str | None,
        region: str | None,
        historical: bool = False,
        filters: RetrievalFilter | None = None,
        limit: int = 20,
    ) -> list[ParsedChunk]:
        now = datetime.now(UTC)
        filters = filters or RetrievalFilter(
            intent="historical" if historical else "current",
            statuses=("active", "deprecated") if historical else ("active",),
            plan=plan,
            region=region,
            effective_at=now,
            logical_time=now,
            index_version=(snapshot := await self.pin_active_snapshot()).index_version,
            corpus_snapshot_id=snapshot.ingest_run_id,
            scope_snapshot=_direct_repository_scope(plan, region),
            eligibility_policy_version="evidence-eligibility.v1",
            pipeline_contract_hash="0" * 64,
            temporal_selector=build_temporal_selector(
                trace_logical_time=now,
                historical_version=None,
                explicit_as_of=None,
            ),
        )
        dialect = self._session.bind.dialect.name if self._session.bind is not None else "unknown"
        if dialect != "postgresql":
            tokens = _keyword_terms(query, exact_tokens)
            rows = (await self._session.execute(self._base(filters))).all()

            def searchable(row: object) -> str:
                chunk = row[0]  # type: ignore[index]
                return f"{chunk.section_path}\n{chunk.content}".lower()

            ranked = sorted(
                rows,
                key=lambda row: (
                    -sum(1 for token in tokens if token.lower() in searchable(row)),
                    -row[1].authority_level,
                    row[1].document_key,
                    row[0].sequence,
                    row[0].chunk_key,
                ),
            )
            matched_all = [
                row
                for row in ranked
                if any(token.lower() in searchable(row) for token in tokens)
            ]
            if filters.intent == "compare":
                matched = [
                    row
                    for status in filters.statuses
                    for row in [
                        item for item in matched_all if item[1].status == status
                    ][:limit]
                ]
                matched.sort(
                    key=lambda row: (
                        -sum(
                            1
                            for token in tokens
                            if token.lower() in searchable(row)
                        ),
                        -row[1].authority_level,
                        row[1].document_key,
                        row[0].sequence,
                        row[0].chunk_key,
                    )
                )
            else:
                matched = matched_all[:limit]
            return [
                self._parsed(chunk, document).model_copy(
                    update={
                        "keyword_score": float(
                            sum(
                                1
                                for token in tokens
                                if token.lower()
                                in f"{chunk.section_path}\n{chunk.content}".lower()
                            )
                        ),
                        "exact_token_match": any(
                            token.lower() in chunk.content.lower() for token in exact_tokens
                        ),
                        "channel_rank": rank,
                    }
                )
                for rank, (chunk, document) in enumerate(matched, start=1)
            ]
        tokens = _keyword_terms(query, exact_tokens)
        tsquery = func.websearch_to_tsquery("simple", query)
        searchable_text = func.concat(
            KnowledgeChunk.section_path, "\n", KnowledgeChunk.content
        )
        conditions = [KnowledgeChunk.search_vector.op("@@")(tsquery)]
        conditions.extend(
            searchable_text.ilike(f"%{token}%")
            for token in tokens
        )
        lexical_score = sum(
            (
                case(
                    (searchable_text.ilike(f"%{token}%"), 1.0),
                    else_=0.0,
                )
                for token in tokens
            ),
            start=literal(0.0),
        )
        keyword_score = (
            func.ts_rank_cd(KnowledgeChunk.search_vector, tsquery) + lexical_score
        ).label("keyword_score")
        if filters.intent == "compare":
            rows = []
            for status in filters.statuses:
                lane_filter = filters.model_copy(update={"statuses": (status,)})
                statement = (
                    self._base(lane_filter)
                    .add_columns(keyword_score)
                    .where(or_(*conditions))
                    .order_by(
                        keyword_score.desc(),
                        KnowledgeDocument.authority_level.desc(),
                        KnowledgeDocument.document_key,
                        KnowledgeChunk.sequence,
                        KnowledgeChunk.chunk_key,
                    )
                    .limit(limit)
                )
                rows.extend((await self._session.execute(statement)).all())
            rows.sort(
                key=lambda row: (
                    -float(row[2]),
                    -row[1].authority_level,
                    row[1].document_key,
                    row[0].sequence,
                    row[0].chunk_key,
                )
            )
        else:
            statement = (
                self._base(filters)
                .add_columns(keyword_score)
                .where(or_(*conditions))
                .order_by(
                    keyword_score.desc(),
                    KnowledgeDocument.authority_level.desc(),
                )
                .limit(limit)
            )
            rows = (await self._session.execute(statement)).all()
        return [
            self._parsed(chunk, document).model_copy(
                update={
                    "keyword_score": float(raw_score),
                    "exact_token_match": any(
                        token.lower() in chunk.content.lower() for token in exact_tokens
                    ),
                    "channel_rank": rank,
                }
            )
            for rank, (chunk, document, raw_score) in enumerate(rows, start=1)
        ]


class RestrictedKnowledgeRepository(KnowledgeRepository):
    """Knowledge repository backed only by the fixed Read MCP SQL capability."""

    def __init__(
        self,
        operation: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]],
        snapshot: KnowledgeSnapshot,
    ) -> None:
        self._operation = operation
        self._snapshot = snapshot

    async def pin_active_snapshot(self) -> KnowledgeSnapshot:
        return self._snapshot

    @staticmethod
    def _filter_payload(filters: RetrievalFilter) -> dict[str, object]:
        return filters.model_dump(mode="json")

    async def candidate_universe(
        self, filters: RetrievalFilter
    ) -> list[dict[str, object]]:
        payload = await self._operation(
            "candidate_universe", {"filters": self._filter_payload(filters)}
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("restricted candidate universe is malformed")
        return [dict(row) for row in rows if isinstance(row, dict)]

    async def vector_search(
        self,
        vector: list[float],
        *,
        plan: str | None,
        region: str | None,
        historical: bool = False,
        filters: RetrievalFilter | None = None,
        limit: int = 20,
    ) -> list[ParsedChunk]:
        del plan, region, historical
        if filters is None:
            raise RuntimeError("restricted vector search requires a frozen filter")
        payload = await self._operation(
            "vector_search",
            {"filters": self._filter_payload(filters), "vector": vector, "limit": limit},
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("restricted vector result is malformed")
        return [ParsedChunk.model_validate(row) for row in rows]

    async def keyword_search(
        self,
        query: str,
        exact_tokens: tuple[str, ...],
        *,
        plan: str | None,
        region: str | None,
        historical: bool = False,
        filters: RetrievalFilter | None = None,
        limit: int = 20,
    ) -> list[ParsedChunk]:
        del plan, region, historical
        if filters is None:
            raise RuntimeError("restricted keyword search requires a frozen filter")
        payload = await self._operation(
            "keyword_search",
            {
                "filters": self._filter_payload(filters),
                "query": query,
                "exact_tokens": list(exact_tokens),
                "keyword_terms": list(
                    _restricted_keyword_terms(
                        query,
                        exact_tokens,
                        intent=filters.intent,
                    )
                ),
                "limit": limit,
            },
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("restricted keyword result is malformed")
        return [ParsedChunk.model_validate(row) for row in rows]
