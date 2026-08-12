import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import KnowledgeChunk, KnowledgeIngestRun
from supportguard.rag.embeddings import DeterministicEmbedding
from supportguard.rag.ingest import ingest_corpus


def write_corpus(root: Path, body: str) -> Path:
    source = root / "knowledge" / "source_docs" / "guide.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(body, encoding="utf-8")
    manifest = root / "knowledge" / "manifests" / "documents.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            [
                {
                    "document_id": "guide-v1",
                    "title": "Guide",
                    "document_type": "official_guide",
                    "version": "1.0",
                    "status": "active",
                    "effective_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    "updated_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    "authority_level": 90,
                    "applicable_plan": None,
                    "applicable_region": None,
                    "source_path": "knowledge/source_docs/guide.md",
                }
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
                        "document_id": "guide-v1",
                        "document_family_key": "guide",
                        "effective_from": "2026-01-01T00:00:00+00:00",
                        "effective_until": None,
                        "applicable_plan": None,
                        "applicable_region": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.asyncio
async def test_ingest_is_idempotent_and_activates_complete_version(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = write_corpus(tmp_path, "# Errors\n\n## 429\n\n429 is a limit, not balance.")
    first = await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=DeterministicEmbedding(),
    )
    await db_session.commit()
    second = await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=DeterministicEmbedding(),
    )
    assert first.reused is False
    assert second.reused is True
    assert second.run_id == first.run_id
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 1
    active = await db_session.scalars(
        select(KnowledgeIngestRun).where(KnowledgeIngestRun.is_active.is_(True))
    )
    assert [run.index_version for run in active] == [first.index_version]


class FailingEmbedding(DeterministicEmbedding):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("injected embedding failure")


class RevisedEmbedding(DeterministicEmbedding):
    revision = "v2"


@pytest.mark.asyncio
async def test_pipeline_identity_change_builds_a_new_index(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = write_corpus(tmp_path, "# Stable\n\nSame corpus, different embedding revision.")
    first = await ingest_corpus(
        db_session, root=tmp_path, manifest_path=manifest, embedding=DeterministicEmbedding()
    )
    await db_session.commit()
    second = await ingest_corpus(
        db_session, root=tmp_path, manifest_path=manifest, embedding=RevisedEmbedding()
    )
    assert second.reused is False
    assert second.index_version != first.index_version


@pytest.mark.asyncio
async def test_failed_rebuild_does_not_replace_active_index(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    manifest = write_corpus(tmp_path, "# Stable\n\nThe active policy is stable.")
    active = await ingest_corpus(
        db_session,
        root=tmp_path,
        manifest_path=manifest,
        embedding=DeterministicEmbedding(),
    )
    await db_session.commit()
    write_corpus(tmp_path, "# Broken\n\nThis version must never become active.")
    with pytest.raises(RuntimeError, match="injected embedding failure"):
        await ingest_corpus(
            db_session,
            root=tmp_path,
            manifest_path=manifest,
            embedding=FailingEmbedding(),
        )
    await db_session.rollback()
    runs = (
        await db_session.scalars(
            select(KnowledgeIngestRun).where(KnowledgeIngestRun.is_active.is_(True))
        )
    ).all()
    assert [run.index_version for run in runs] == [active.index_version]
