from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestRun,
    new_id,
)
from supportguard.rag.chunking import chunk_markdown, indexed_text
from supportguard.rag.embeddings import (
    EmbeddingProvider,
    embedding_fingerprint,
    embedding_identity,
)
from supportguard.rag.manifest import corpus_version, load_manifest
from supportguard.rag.temporal import load_temporal_backfill
from supportguard.rag.types import SourceLocator, SourceLocatorV2

CANONICALIZATION_VERSION = "utf8-lf-nfc.v1"
CHUNKER_CONTRACT = {
    "chunker": "markdown-heading-window-v2",
    "target_tokens": 325,
    "overlap_tokens": 50,
    "max_embedding_tokens": 512,
}
CHUNKER_FINGERPRINT = hashlib.sha256(
    json.dumps(CHUNKER_CONTRACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


@dataclass(frozen=True)
class IngestResult:
    run_id: str
    index_version: str
    document_count: int
    chunk_count: int
    reused: bool


def _required_locator(locator: SourceLocator | None) -> SourceLocator:
    if locator is None:
        raise ValueError("ingested chunks require SourceLocatorV1")
    return locator


async def ingest_corpus(
    session: AsyncSession,
    *,
    root: Path,
    manifest_path: Path,
    embedding: EmbeddingProvider,
    temporal_manifest_path: Path | None = None,
) -> IngestResult:
    documents = load_manifest(manifest_path)
    temporal = load_temporal_backfill(
        temporal_manifest_path or manifest_path.with_name("temporal-backfill.v1.json")
    )
    temporal_entries = temporal.by_document_id()
    document_ids = {document.document_id for document in documents}
    if set(temporal_entries) != document_ids:
        missing = sorted(document_ids - set(temporal_entries))
        extra = sorted(set(temporal_entries) - document_ids)
        raise ValueError(f"temporal_backfill_manifest_mismatch:missing={missing}:extra={extra}")
    dialect = session.bind.dialect.name if session.bind is not None else "unknown"
    pipeline_identity = {
        **CHUNKER_CONTRACT,
        "embedding_model": getattr(embedding, "model_name", type(embedding).__name__),
        "embedding_revision": getattr(embedding, "revision", "unknown"),
        "tokenizer": getattr(embedding, "tokenizer", "unknown"),
        "pooling": getattr(embedding, "pooling", "unknown"),
        "normalization": getattr(embedding, "normalization", "unknown"),
        "embedding_dimensions": embedding.dimensions,
        "prefix": "passage: ",
        "indexed_text_schema": "title+section_path+content.v2",
        "keyword_analyzer": "simple+exact-substring.v2",
        "canonicalization": CANONICALIZATION_VERSION,
        "locator_schema": "source-locator.v2",
        "chunker_fingerprint": CHUNKER_FINGERPRINT,
        "temporal_backfill_schema": temporal.schema_version,
        "temporal_backfill_hash": temporal.content_hash,
    }
    current_embedding_identity = embedding_identity(embedding)
    current_embedding_fingerprint = embedding_fingerprint(embedding)
    version = corpus_version(documents, root, pipeline_identity=pipeline_identity)
    existing = await session.scalar(
        select(KnowledgeIngestRun).where(
            KnowledgeIngestRun.index_version == version,
            KnowledgeIngestRun.status == "succeeded",
            KnowledgeIngestRun.is_active.is_(True),
        )
    )
    if existing is not None:
        if existing.pipeline_identity is None:
            existing.pipeline_identity = {
                **pipeline_identity,
                "embedding_contract": current_embedding_identity,
            }
            existing.pipeline_fingerprint = current_embedding_fingerprint
            await session.flush()
        elif existing.pipeline_fingerprint != current_embedding_fingerprint:
            raise RuntimeError("active_index_embedding_contract_mismatch")
        return IngestResult(
            existing.id, version, existing.document_count, existing.chunk_count, True
        )

    run = KnowledgeIngestRun(
        id=new_id("ingest"),
        status="staging",
        index_version=version,
        is_active=False,
        pipeline_identity={
            **pipeline_identity,
            "embedding_contract": current_embedding_identity,
        },
        pipeline_fingerprint=current_embedding_fingerprint,
    )
    session.add(run)
    await session.flush()
    chunk_total = 0
    for metadata in documents:
        temporal_entry = temporal_entries[metadata.document_id]
        if (
            temporal_entry.applicable_plan != metadata.applicable_plan
            or temporal_entry.applicable_region != metadata.applicable_region
        ):
            raise ValueError(f"temporal_scope_mismatch:{metadata.document_id}")
        source = root / metadata.source_path
        markdown = unicodedata.normalize(
            "NFC",
            source.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n"),
        )
        canonical_blob = markdown.encode("utf-8")
        content_hash = hashlib.sha256(canonical_blob).hexdigest()
        document_id = new_id("doc")
        document = KnowledgeDocument(
            id=document_id,
            ingest_run_id=run.id,
            document_key=metadata.document_id,
            document_family_key=temporal_entry.document_family_key,
            title=metadata.title,
            document_type=metadata.document_type,
            version=metadata.version,
            status=metadata.status,
            effective_at=metadata.effective_at,
            effective_from=temporal_entry.effective_from,
            effective_until=temporal_entry.effective_until,
            applicability_scope_hash=temporal_entry.scope_hash,
            temporal_manifest_hash=temporal.content_hash,
            authority_level=metadata.authority_level,
            applicable_plan=metadata.applicable_plan,
            applicable_region=metadata.applicable_region,
            content_hash=content_hash,
            canonical_blob=canonical_blob,
            source_path=metadata.source_path,
            index_version=version,
            canonicalization_version=CANONICALIZATION_VERSION,
        )
        session.add(document)
        await session.flush()
        counter = getattr(embedding, "token_count", None)
        if counter is None:
            parsed = chunk_markdown(metadata, markdown)
        else:
            parsed = chunk_markdown(metadata, markdown, token_counter=counter)
        indexed = [indexed_text(item.title, item.section_path, item.content) for item in parsed]
        vectors = embedding.embed_documents(indexed)
        if any(len(vector) != 384 for vector in vectors):
            raise ValueError("embedding provider must return 384 dimensions")
        chunks: list[KnowledgeChunk] = []
        for item, vector in zip(parsed, vectors, strict=True):
            source_locator = _required_locator(item.source_locator)
            locator = SourceLocatorV2.build(
                document_key=metadata.document_id,
                document_internal_id=document.id,
                document_version=metadata.version,
                source_bytes=canonical_blob,
                corpus_snapshot_id=run.id,
                index_version=version,
                canonicalization_version=CANONICALIZATION_VERSION,
                section_path=item.section_path,
                byte_start=source_locator.byte_start,
                byte_end=source_locator.byte_end,
                chunker_fingerprint=CHUNKER_FINGERPRINT,
                embedding_fingerprint=current_embedding_fingerprint,
            )
            chunks.append(
                KnowledgeChunk(
                    document_id=document.id,
                    ingest_run_id=run.id,
                    chunk_key=item.chunk_id,
                    section_path=item.section_path,
                    sequence=item.sequence,
                    content=item.content,
                    token_count=item.token_count,
                    content_hash=item.content_hash,
                    byte_start=locator.byte_start,
                    byte_end=locator.byte_end,
                    span_hash=locator.span_hash,
                    locator_hash=locator.locator_hash,
                    locator_schema_version=locator.locator_schema,
                    canonicalization_version=CANONICALIZATION_VERSION,
                    chunker_fingerprint=CHUNKER_FINGERPRINT,
                    embedding_fingerprint=current_embedding_fingerprint,
                    index_version=version,
                    embedding=vector,
                    search_vector=indexed_text(item.title, item.section_path, item.content)
                    if dialect == "sqlite"
                    else None,
                )
            )
        session.add_all(chunks)
        chunk_total += len(parsed)
    await session.flush()
    if dialect == "postgresql":
        document_title = (
            select(KnowledgeDocument.title)
            .where(KnowledgeDocument.id == KnowledgeChunk.document_id)
            .scalar_subquery()
        )
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.index_version == version)
            .values(
                search_vector=func.to_tsvector(
                    "simple",
                    func.concat(
                        document_title,
                        " ",
                        KnowledgeChunk.section_path,
                        " ",
                        KnowledgeChunk.content,
                    ),
                )
            )
        )
    await session.execute(
        update(KnowledgeIngestRun)
        .where(KnowledgeIngestRun.is_active.is_(True))
        .values(is_active=False, deactivated_at=func.now())
    )
    run.status = "succeeded"
    run.is_active = True
    run.document_count = len(documents)
    run.chunk_count = chunk_total
    return IngestResult(run.id, version, len(documents), chunk_total, False)
