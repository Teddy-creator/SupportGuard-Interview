from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.rag.temporal import TemporalSelector


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    document_type: str
    version: str
    status: str
    effective_at: datetime
    updated_at: datetime
    authority_level: int = Field(ge=0, le=100)
    applicable_plan: str | None = None
    applicable_region: str | None = None
    source_path: str


class SourceLocatorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locator_schema: Literal["source-locator.v1"] = "source-locator.v1"
    document_id: str
    version: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    span_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        document_id: str,
        version: str,
        source_bytes: bytes,
        byte_start: int,
        byte_end: int,
    ) -> SourceLocatorV1:
        if byte_end <= byte_start or byte_end > len(source_bytes):
            raise ValueError("SourceLocatorV1 requires a valid half-open byte range")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        span_hash = hashlib.sha256(source_bytes[byte_start:byte_end]).hexdigest()
        identity = {
            "locator_schema": "source-locator.v1",
            "document_id": document_id,
            "version": version,
            "source_hash": source_hash,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "span_hash": span_hash,
        }
        locator_hash = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(**identity, locator_hash=locator_hash)

    def resolve(self, source_bytes: bytes) -> bytes:
        if hashlib.sha256(source_bytes).hexdigest() != self.source_hash:
            raise ValueError("source_hash_mismatch")
        span = source_bytes[self.byte_start : self.byte_end]
        if hashlib.sha256(span).hexdigest() != self.span_hash:
            raise ValueError("span_hash_mismatch")
        rebuilt = self.model_copy(update={"locator_hash": "0" * 64})
        expected = self.build(
            document_id=rebuilt.document_id,
            version=rebuilt.version,
            source_bytes=source_bytes,
            byte_start=rebuilt.byte_start,
            byte_end=rebuilt.byte_end,
        )
        if expected.locator_hash != self.locator_hash:
            raise ValueError("locator_hash_mismatch")
        return span

    def subspan(
        self, *, parent_span: bytes, relative_start: int, relative_end: int
    ) -> SourceLocatorV1:
        if hashlib.sha256(parent_span).hexdigest() != self.span_hash:
            raise ValueError("parent_span_hash_mismatch")
        if relative_start < 0 or relative_end <= relative_start or relative_end > len(parent_span):
            raise ValueError("invalid_relative_span")
        byte_start = self.byte_start + relative_start
        byte_end = self.byte_start + relative_end
        span_hash = hashlib.sha256(parent_span[relative_start:relative_end]).hexdigest()
        identity = {
            "locator_schema": "source-locator.v1",
            "document_id": self.document_id,
            "version": self.version,
            "source_hash": self.source_hash,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "span_hash": span_hash,
        }
        locator_hash = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return SourceLocatorV1(**identity, locator_hash=locator_hash)


class SourceLocatorV2(BaseModel):
    """Immutable source identity across corpus rebuilds and processing revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    locator_schema: Literal["source-locator.v2"] = "source-locator.v2"
    document_key: str
    document_internal_id: str
    document_version: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_snapshot_id: str
    index_version: str
    canonicalization_version: str
    section_path: str
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    span_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunker_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        document_key: str,
        document_internal_id: str,
        document_version: str,
        source_bytes: bytes,
        corpus_snapshot_id: str,
        index_version: str,
        canonicalization_version: str,
        section_path: str,
        byte_start: int,
        byte_end: int,
        chunker_fingerprint: str,
        embedding_fingerprint: str,
    ) -> SourceLocatorV2:
        if byte_end <= byte_start or byte_end > len(source_bytes):
            raise ValueError("SourceLocatorV2 requires a valid half-open byte range")
        identity = {
            "locator_schema": "source-locator.v2",
            "document_key": document_key,
            "document_internal_id": document_internal_id,
            "document_version": document_version,
            "source_hash": hashlib.sha256(source_bytes).hexdigest(),
            "corpus_snapshot_id": corpus_snapshot_id,
            "index_version": index_version,
            "canonicalization_version": canonicalization_version,
            "section_path": section_path,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "span_hash": hashlib.sha256(source_bytes[byte_start:byte_end]).hexdigest(),
            "chunker_fingerprint": chunker_fingerprint,
            "embedding_fingerprint": embedding_fingerprint,
        }
        locator_hash = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(**identity, locator_hash=locator_hash)

    def resolve(self, source_bytes: bytes) -> bytes:
        if hashlib.sha256(source_bytes).hexdigest() != self.source_hash:
            raise ValueError("source_hash_mismatch")
        span = source_bytes[self.byte_start : self.byte_end]
        if hashlib.sha256(span).hexdigest() != self.span_hash:
            raise ValueError("span_hash_mismatch")
        expected = self.build(
            document_key=self.document_key,
            document_internal_id=self.document_internal_id,
            document_version=self.document_version,
            source_bytes=source_bytes,
            corpus_snapshot_id=self.corpus_snapshot_id,
            index_version=self.index_version,
            canonicalization_version=self.canonicalization_version,
            section_path=self.section_path,
            byte_start=self.byte_start,
            byte_end=self.byte_end,
            chunker_fingerprint=self.chunker_fingerprint,
            embedding_fingerprint=self.embedding_fingerprint,
        )
        if expected.locator_hash != self.locator_hash:
            raise ValueError("locator_hash_mismatch")
        return span

    def subspan(
        self, *, parent_span: bytes, relative_start: int, relative_end: int
    ) -> SourceLocatorV2:
        if hashlib.sha256(parent_span).hexdigest() != self.span_hash:
            raise ValueError("parent_span_hash_mismatch")
        if relative_start < 0 or relative_end <= relative_start or relative_end > len(parent_span):
            raise ValueError("invalid_relative_span")
        identity = self.model_dump(exclude={"locator_hash"})
        identity.update(
            {
                "byte_start": self.byte_start + relative_start,
                "byte_end": self.byte_start + relative_end,
                "span_hash": hashlib.sha256(
                    parent_span[relative_start:relative_end]
                ).hexdigest(),
            }
        )
        locator_hash = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return SourceLocatorV2(**identity, locator_hash=locator_hash)


SourceLocator = SourceLocatorV1 | SourceLocatorV2


class EligibilityEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["eligibility-envelope.v1"] = "eligibility-envelope.v1"
    corpus_snapshot_id: str
    index_version: str
    document_internal_id: str
    chunk_id: str
    status: str
    authority_level: int
    applicable_plan: str | None
    applicable_region: str | None
    effective_from: datetime
    effective_until: datetime | None = None
    logical_time: datetime
    filter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: str
    reason_code: str


class RetrievalScopeSnapshot(BaseModel):
    """Durable references for the trusted business facts used to build RAG scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["retrieval-scope-snapshot.v1"] = "retrieval-scope-snapshot.v1"
    tenant_id: str
    customer_id: str
    subscription_id: str
    subscription_version: int = Field(ge=1)
    plan: str
    region_trace_id: str | None = None
    region_trace_version: int | None = Field(default=None, ge=1)
    region: str | None = None

    @model_validator(mode="after")
    def complete_region_origin(self) -> RetrievalScopeSnapshot:
        values = (self.region_trace_id, self.region_trace_version, self.region)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("region scope origin must be complete")
        return self


class RetrievalFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Literal["current", "historical", "compare"] = "current"
    statuses: tuple[str, ...] = ("active",)
    version: str | None = None
    minimum_authority: int = Field(default=0, ge=0, le=100)
    plan: str | None = None
    region: str | None = None
    effective_at: datetime
    logical_time: datetime
    index_version: str
    corpus_snapshot_id: str
    scope_snapshot: RetrievalScopeSnapshot
    eligibility_policy_version: Literal[
        "evidence-eligibility.v1", "evidence-eligibility.v1-fixture-keyword"
    ]
    pipeline_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["filter-contract.v2"] = "filter-contract.v2"
    temporal_selector: TemporalSelector


class RetrievalProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["retrieval-provenance.v1"] = "retrieval-provenance.v1"
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    filter_contract: RetrievalFilter
    vector_candidates: list[dict[str, object]]
    keyword_candidates: list[dict[str, object]]
    rrf_candidates: list[dict[str, object]]
    pre_filter_candidates: list[dict[str, object]] = Field(default_factory=list)
    selected_candidates: list[dict[str, object]] = Field(default_factory=list)
    omission_decisions: list[dict[str, object]] = Field(default_factory=list)
    evidence_groups: list[dict[str, object]] = Field(default_factory=list)
    eligibility_envelopes: list[dict[str, object]] = Field(default_factory=list)
    pipeline_contract: dict[str, object] = Field(default_factory=dict)
    embedding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_version: str
    corpus_snapshot_id: str
    abstention_reason: str | None = None


class ParsedChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_type: str
    title: str
    section_path: str
    sequence: int
    content: str
    token_count: int
    content_hash: str
    version: str
    status: str
    effective_at: datetime
    effective_until: datetime | None = None
    document_family_key: str
    applicability_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_level: int
    applicable_plan: str | None = None
    applicable_region: str | None = None
    source_locator: SourceLocator | None = None
    eligibility_envelope: EligibilityEnvelope | None = None
    vector_similarity: float | None = None
    vector_distance: float | None = None
    keyword_score: float | None = None
    exact_token_match: bool = False
    channel_rank: int | None = None
    filter_reason: str = "eligible"
    evidence_group: Literal["current", "historical"] | None = None


class RankedChunk(BaseModel):
    chunk: ParsedChunk
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None
    vector_similarity: float | None = None
    keyword_score: float | None = None
    vector_contribution: float = 0.0
    keyword_contribution: float = 0.0
    eligibility_outcome: str | None = None
    eligibility_reason: str | None = None
    omission_reason: str | None = None


class KnowledgeCitation(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    section_path: str
    version: str
    effective_at: datetime
    excerpt: str
    content_hash: str = ""
    source_locator: SourceLocator


class EvidenceSet(BaseModel):
    chunks: list[RankedChunk]
    citations: list[KnowledgeCitation]
    conflict: bool = False
    refusal_reason: str | None = None
