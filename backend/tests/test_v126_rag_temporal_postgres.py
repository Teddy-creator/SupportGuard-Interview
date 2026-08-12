import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import create_async_engine

from current_predicate_facts import record_predicate_operands

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_postgres_rejects_overlapping_family_scope_interval() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    suffix = uuid4().hex[:10]
    base_key = f"temporal-base-{suffix}"
    insert = text(
        """
        INSERT INTO knowledge_documents(
            id,ingest_run_id,document_key,document_family_key,title,document_type,
            version,status,effective_at,effective_from,effective_until,
            applicability_scope_hash,temporal_manifest_hash,authority_level,
            applicable_plan,applicable_region,content_hash,canonical_blob,source_path,
            index_version,canonicalization_version,created_at,updated_at
        )
        SELECT :id,ingest_run_id,:key,document_family_key,title,document_type,
               :version,status,:starts,:starts,:ends,applicability_scope_hash,
               temporal_manifest_hash,authority_level,applicable_plan,applicable_region,
               content_hash,canonical_blob,source_path,index_version,canonicalization_version,
               clock_timestamp(),clock_timestamp()
          FROM knowledge_documents WHERE document_key=:base_key
        """
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_ingest_runs(id,status,index_version,document_count,"
                        "chunk_count,is_active,created_at,updated_at) VALUES "
                        "(:run,'succeeded',:index,1,0,false,clock_timestamp(),clock_timestamp())"
                    ),
                    {"run": f"ingest_temporal_{suffix}", "index": f"index_temporal_{suffix}"},
                )
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_documents(id,ingest_run_id,document_key,"
                        "document_family_key,title,document_type,version,status,effective_at,"
                        "effective_from,effective_until,applicability_scope_hash,"
                        "temporal_manifest_hash,authority_level,content_hash,canonical_blob,"
                        "source_path,index_version,canonicalization_version,created_at,updated_at) "
                        "VALUES (:id,:run,:key,'temporal-family','Temporal fixture','policy','1.0',"
                        "'deprecated',:starts,:starts,:ends,:scope,:manifest,100,:content_hash,"
                        ":blob,'fixture.md',:index,'utf8-lf-nfc.v1',clock_timestamp(),clock_timestamp())"
                    ),
                    {
                        "id": f"doc_base_{suffix}",
                        "run": f"ingest_temporal_{suffix}",
                        "key": base_key,
                        "starts": datetime(2025, 1, 1, tzinfo=UTC),
                        "ends": datetime(2026, 1, 1, tzinfo=UTC),
                        "scope": "a" * 64,
                        "manifest": "b" * 64,
                        "content_hash": "c" * 64,
                        "blob": b"temporal fixture",
                        "index": f"index_temporal_{suffix}",
                    },
                )
                # [2026, 2027) is adjacent to the frozen legacy [2025, 2026)
                # interval and therefore valid under half-open semantics.
                result = await connection.execute(
                    insert,
                    {
                        "id": f"doc_adjacent_{suffix}",
                        "key": f"legacy-adjacent-{suffix}",
                        "version": f"2-{suffix}",
                        "base_key": base_key,
                        "starts": datetime(2026, 1, 1, tzinfo=UTC),
                        "ends": datetime(2027, 1, 1, tzinfo=UTC),
                    },
                )
                assert result.rowcount == 1

                savepoint = await connection.begin_nested()
                with pytest.raises(
                    exc.IntegrityError, match="knowledge_effective_interval_overlap"
                ):
                    await connection.execute(
                        insert,
                        {
                            "id": f"doc_overlap_{suffix}",
                            "key": f"legacy-overlap-{suffix}",
                            "version": f"1-overlap-{suffix}",
                            "base_key": base_key,
                            "starts": datetime(2025, 12, 1, tzinfo=UTC),
                            "ends": datetime(2026, 2, 1, tzinfo=UTC),
                        },
                    )
                await savepoint.rollback()
                record_predicate_operands(
                    requirement_id="C6-P0-13",
                    predicate_id="document_family_conflict_scope_exact",
                    subject_kind="postgres_temporal_interval",
                    operands={
                        "family_key": "temporal-family",
                        "scope_hash": "a" * 64,
                        "adjacent_row_count": result.rowcount,
                        "overlap_error_code": "knowledge_effective_interval_overlap",
                    },
                )
                for predicate_id in ("effective_until_persisted", "half_open_interval_applied"):
                    record_predicate_operands(
                        requirement_id="C6-P0-13",
                        predicate_id=predicate_id,
                        subject_kind="postgres_temporal_interval",
                        operands={
                            "base_effective_until": "2026-01-01T00:00:00+00:00",
                            "adjacent_effective_from": "2026-01-01T00:00:00+00:00",
                            "adjacent_row_count": result.rowcount,
                            "overlap_persisted_count": 0,
                        },
                    )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
