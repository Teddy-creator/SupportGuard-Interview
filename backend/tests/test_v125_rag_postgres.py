from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from current_predicate_facts import record_predicate_operands

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_fts_gin_is_valid_and_representative_plan_is_natural() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        enable_seqscan = await connection.scalar(text("SHOW enable_seqscan"))
        assert enable_seqscan == "on"
        index = (
            await connection.execute(
                text(
                    "SELECT i.indisvalid, am.amname FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid "
                    "JOIN pg_am am ON am.oid=c.relam "
                    "WHERE c.relname='ix_knowledge_chunk_search_vector_gin'"
                )
            )
        ).one()
        assert index == (True, "gin")
        plan = "\n".join(
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "EXPLAIN (COSTS true) SELECT chunk_key FROM knowledge_chunks "
                        "WHERE search_vector @@ plainto_tsquery('simple','refund policy')"
                    )
                )
            ).all()
        )
        assert "knowledge_chunks" in plan
        assert "Seq Scan" in plan or "Bitmap Index Scan" in plan or "Index Scan" in plan
        operands = {
            "enable_seqscan": enable_seqscan,
            "index_valid": index[0],
            "index_method": index[1],
            "plan": plan,
            "plan_mentions_knowledge_chunks": "knowledge_chunks" in plan,
            "natural_scan_operator_present": any(
                item in plan for item in ("Seq Scan", "Bitmap Index Scan", "Index Scan")
            ),
        }
        for predicate_id in ("gin_present_valid", "representative_plan_natural"):
            record_predicate_operands(
                requirement_id="C5-P0-14",
                predicate_id=predicate_id,
                subject_kind="postgres_rag_query_plan",
                operands=operands,
            )
    await engine.dispose()
