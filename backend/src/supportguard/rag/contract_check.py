"""Fail-closed active-index/query Embedding contract verification."""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from supportguard.config import get_settings
from supportguard.db.models import KnowledgeIngestRun
from supportguard.db.session import create_engine, create_session_factory
from supportguard.rag.embeddings import configured_embedding_fingerprint


async def verify_active_index_contract() -> dict[str, str]:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runs = (
                await session.scalars(
                    select(KnowledgeIngestRun).where(
                        KnowledgeIngestRun.is_active.is_(True),
                        KnowledgeIngestRun.status == "succeeded",
                    )
                )
            ).all()
        if len(runs) != 1:
            raise RuntimeError(f"expected one active knowledge index, found {len(runs)}")
        active = runs[0]
        expected = configured_embedding_fingerprint(settings)
        if active.pipeline_fingerprint != expected:
            raise RuntimeError("active knowledge index is incompatible with query embeddings")
        return {
            "status": "passed",
            "index_version": active.index_version,
            "pipeline_fingerprint": expected,
        }
    finally:
        await engine.dispose()


def main() -> int:
    try:
        print(json.dumps(asyncio.run(verify_active_index_contract()), sort_keys=True))
        return 0
    except (OSError, RuntimeError, SQLAlchemyError) as exc:
        print(f"knowledge index contract verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
