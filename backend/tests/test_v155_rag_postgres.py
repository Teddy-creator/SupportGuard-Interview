from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.rag.embeddings import DeterministicEmbedding
from supportguard.rag.intent import resolve_retrieval_intent
from supportguard.rag.repository import KnowledgeRepository
from supportguard.rag.service import RetrievalService
from supportguard.rag.types import RetrievalScopeSnapshot


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_natural_language_v_prefixed_version_reaches_postgres_canonically() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL is not configured for PostgreSQL")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    query = "请只按历史 v2.2 文档说明当时的 API 重试规则，并明确你引用的版本。"
    intent = resolve_retrieval_intent(query)
    assert intent.intent == "historical"
    assert intent.historical_version == "2.2"
    try:
        async with factory() as session:
            _, evidence, trace = await RetrievalService(
                KnowledgeRepository(session),
                DeterministicEmbedding(),
            ).retrieve_with_trace(
                query,
                intent=intent.intent,
                historical_version=intent.historical_version,
                scope_snapshot=RetrievalScopeSnapshot(
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    subscription_id="sub_demo",
                    subscription_version=3,
                    plan="pro",
                    region_trace_id="trace_demo_429",
                    region_trace_version=1,
                    region="eu-west",
                ),
            )
        assert evidence.refusal_reason is None
        assert evidence.chunks
        assert {item.chunk.version for item in evidence.chunks} == {"2.2"}
        assert trace.filter_contract.version == "2.2"
        assert trace.filter_contract.temporal_selector.historical_version == "2.2"
        assert all(citation.version == "2.2" for citation in evidence.citations)
    finally:
        await engine.dispose()
