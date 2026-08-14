from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.evidence import assess_terminal_evidence
from supportguard.agent.schemas import CandidateResponse
from supportguard.config import Settings
from supportguard.db.base import Base
from supportguard.db.models import (
    ApiUsageBucket,
    ApiUsageSnapshot,
    BillingRecord,
    SupportTicket,
)
from supportguard.db.seed import seed_demo_data
from supportguard.services.demo_temporal import (
    demo_coverage_window_end,
    demo_resource_preflight,
    demo_temporal_preflight,
    refresh_demo_temporal_fixtures,
)

ROOT = Path(__file__).resolve().parents[2]


def _candidate(*, source_id: str = "usage:1") -> CandidateResponse:
    return CandidateResponse.model_validate(
        {
            "answer": "余额和并发是不同机制；当前并发状态需要实时用量支持。",
            "action": "answer",
            "knowledge_chunk_ids": [],
            "business_source_ids": [source_id],
            "material_claims": [
                {
                    "text": "当前并发状态需要实时用量支持。",
                    "observation_source_ids": [source_id],
                }
            ],
        }
    )


def test_stale_usage_cannot_support_a_current_429_claim_and_requests_replan() -> None:
    assessment = assess_terminal_evidence(
        issue_type="api_diagnostics",
        candidate=_candidate(),
        observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "freshness_status": "fresh",
                "data": {"evidence": [{"chunk_id": "knowledge:1"}]},
            },
            {
                "tool_name": "query_api_usage",
                "status": "ok",
                "freshness_status": "stale",
                "source_refs": [{"source_id": "usage:1"}],
                "data": {"concurrency_current": 40},
            },
        ],
        evidence_conflict=False,
        specified_request=False,
        can_replan=True,
    )
    assert assessment.result == "replan"
    assert assessment.stale_groups == ["query_api_usage"]
    assert assessment.error_code == "evidence_freshness_insufficient"


def test_specified_request_requires_trace_but_does_not_prescribe_tool_order() -> None:
    assessment = assess_terminal_evidence(
        issue_type="api_diagnostics",
        candidate=CandidateResponse(
            answer="请结合请求轨迹定位。",
            action="answer",
            knowledge_chunk_ids=[],
            business_source_ids=[],
            material_claims=[{"text": "请结合请求轨迹定位。"}],
        ),
        observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "freshness_status": "fresh",
                "data": {"evidence": [{"chunk_id": "knowledge:1"}]},
            }
        ],
        evidence_conflict=False,
        specified_request=True,
        can_replan=False,
    )
    assert assessment.result == "terminal"
    assert assessment.missing_groups == ["request_trace"]


def test_refund_policy_followup_needs_knowledge_not_a_second_billing_read() -> None:
    assessment = assess_terminal_evidence(
        issue_type="billing_refund",
        candidate=CandidateResponse(
            answer="退款通常按当前政策所列周期原路退回。",
            action="answer",
            knowledge_chunk_ids=["refund-policy"],
            business_source_ids=[],
            material_claims=[
                {
                    "text": "退款通常按当前政策所列周期原路退回。",
                    "citation_binding_ids": ["citation-refund-policy"],
                    "knowledge_locator_hashes": ["a" * 64],
                }
            ],
        ),
        observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "freshness_status": "fresh",
                "fresh_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "data": {"evidence": [{"chunk_id": "refund-policy"}]},
            }
        ],
        evidence_conflict=False,
        specified_request=False,
        can_replan=True,
    )
    assert assessment.sufficient is True
    assert assessment.required_groups == ["knowledge"]


def test_complete_published_version_comparison_satisfies_knowledge_group() -> None:
    comparison_evidence = [
        {
            "chunk_id": f"product:{group}",
            "evidence_group": group,
            "supporting_span_eligible": True,
            "source_locator": {"locator_hash": marker * 64},
        }
        for group, marker in (("current", "a"), ("historical", "b"))
    ]
    assessment = assess_terminal_evidence(
        issue_type="product_knowledge",
        candidate=CandidateResponse(
            answer="当前与历史版本存在已发布差异。",
            action="answer",
            knowledge_chunk_ids=["product:current", "product:historical"],
            business_source_ids=[],
        ),
        observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "data": {
                    "conflict": False,
                    "refusal_reason": None,
                    "evidence": comparison_evidence,
                },
            }
        ],
        evidence_conflict=False,
        specified_request=False,
        can_replan=True,
        explainable_comparison=True,
    )

    assert assessment.sufficient is True
    assert assessment.satisfied_groups == ["knowledge"]
    assert assessment.result == "accept"


def test_published_version_difference_remains_unsafe_without_comparison_contract() -> None:
    assessment = assess_terminal_evidence(
        issue_type="product_knowledge",
        candidate=CandidateResponse(
            answer="当前与历史版本存在差异。",
            action="answer",
            knowledge_chunk_ids=["product:current"],
            business_source_ids=[],
        ),
        observations=[
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "data": {
                    "conflict": True,
                    "refusal_reason": "unresolved_published_version_conflict",
                    "evidence": [{"chunk_id": "product:current"}],
                },
            }
        ],
        evidence_conflict=True,
        specified_request=False,
        can_replan=True,
    )

    assert assessment.sufficient is False
    assert assessment.result == "replan"
    assert assessment.missing_groups == ["knowledge", "non_conflicting_knowledge"]


@pytest.mark.asyncio
async def test_demo_resource_preflight_requires_a_clean_unconsumed_demo(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}",
    )
    async with factory() as session:
        await seed_demo_data(session)
        await session.commit()
        ready = await demo_resource_preflight(session, settings=settings, tenant_id="tenant_demo")
        assert ready.ready is True
        assert ready.violations == ()

        billing = await session.get(BillingRecord, "bill_demo_duplicate")
        assert billing is not None
        billing.status = "refunded"
        await session.commit()
        consumed = await demo_resource_preflight(
            session, settings=settings, tenant_id="tenant_demo"
        )
        assert consumed.ready is False
        assert "duplicate_billing_not_charged" in consumed.violations
    await engine.dispose()


def test_demo_preflight_uses_the_role_that_owns_each_resource() -> None:
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("demo-preflight:", 1)[1].split("\n\n", 1)[0]
    assert "run --rm --no-deps bootstrap-demo supportguard demo temporal-refresh" in target
    assert "run --rm --no-deps bootstrap-demo supportguard demo temporal-preflight" in target
    assert target.index("demo temporal-refresh") < target.index("demo temporal-preflight")
    assert "run --rm --no-deps worker supportguard demo resource-preflight" in target
    assert "bootstrap-demo supportguard demo resource-preflight" not in target


def test_compose_bootstrap_refreshes_demo_time_after_seed_and_knowledge() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    target = compose.split("  bootstrap-demo:", 1)[1].split("\n  api:", 1)[0]

    assert target.index("supportguard db seed") < target.index("supportguard knowledge ingest")
    assert target.index("supportguard knowledge ingest") < target.index(
        "supportguard demo temporal-refresh --tenant tenant_demo"
    )


def test_compose_demo_maintainer_keeps_local_usage_fresh() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    target = compose.split("  demo-temporal:", 1)[1].split("\n  api:", 1)[0]

    assert '"temporal-maintain", "--tenant", "tenant_demo"' in target
    assert "supportguard_bootstrap" in target
    assert "bootstrap-demo:" in target
    assert "service_completed_successfully" in target
    assert "restart: unless-stopped" in target


def test_demo_temporal_refresh_covers_an_imminent_minute_rollover() -> None:
    base = datetime(2026, 7, 24, 12, 30, tzinfo=UTC)

    assert demo_coverage_window_end(base + timedelta(seconds=54)) == base + timedelta(minutes=5)
    assert demo_coverage_window_end(base + timedelta(seconds=55)) == base + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_demo_temporal_refresh_is_scoped_non_destructive_and_fresh(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'temporal.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'temporal.db'}",
    )
    async with factory() as session:
        await seed_demo_data(session)
        snapshots = list((await session.scalars(select(ApiUsageSnapshot))).all())
        buckets = list((await session.scalars(select(ApiUsageBucket))).all())
        second_refresh = datetime.now(UTC)
        first_refresh = second_refresh.replace(second=59, microsecond=0) - timedelta(minutes=1)
        old_time = first_refresh - timedelta(days=3)
        for snapshot in snapshots:
            snapshot.observed_at = old_time
        for index, bucket in enumerate(buckets):
            bucket.bucket_start = old_time + timedelta(minutes=index)
            bucket.bucket_end = old_time + timedelta(minutes=index + 1)
        await session.commit()
        ticket_count = int(await session.scalar(select(func.count()).select_from(SupportTicket)))
        report = await refresh_demo_temporal_fixtures(
            session,
            settings=settings,
            tenant_id="tenant_demo",
            clock=lambda: first_refresh,
        )
        await session.commit()
        repeated = await refresh_demo_temporal_fixtures(
            session,
            settings=settings,
            tenant_id="tenant_demo",
            clock=lambda: first_refresh,
        )
        await session.commit()
        rolled_over = await refresh_demo_temporal_fixtures(
            session,
            settings=settings,
            tenant_id="tenant_demo",
            clock=lambda: second_refresh,
        )
        await session.commit()
        after = await demo_temporal_preflight(session, settings=settings, tenant_id="tenant_demo")
        assert report.mode == "refresh"
        assert repeated.mode == "refresh"
        assert rolled_over.mode == "refresh"
        assert repeated.usage_bucket_count == report.usage_bucket_count
        assert repeated.usage_snapshot_count == report.usage_snapshot_count
        assert rolled_over.usage_bucket_count == report.usage_bucket_count + 1
        assert rolled_over.usage_snapshot_count == report.usage_snapshot_count + 1
        assert repeated.latest_snapshot_age_seconds is not None
        assert repeated.latest_snapshot_age_seconds <= 5
        assert rolled_over.latest_snapshot_age_seconds is not None
        assert rolled_over.latest_snapshot_age_seconds <= 5
        assert after.latest_snapshot_age_seconds is not None
        assert after.latest_snapshot_age_seconds <= 5
        latest_bucket_end = await session.scalar(
            select(func.max(ApiUsageBucket.bucket_end)).where(
                ApiUsageBucket.tenant_id == "tenant_demo",
                ApiUsageBucket.customer_id == "cust_demo",
            )
        )
        assert latest_bucket_end is not None
        comparable_end = (
            latest_bucket_end
            if latest_bucket_end.tzinfo is not None
            else latest_bucket_end.replace(tzinfo=UTC)
        )
        assert comparable_end >= datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(
            minutes=5
        )
        latest_snapshot = await session.scalar(
            select(ApiUsageSnapshot)
            .where(
                ApiUsageSnapshot.tenant_id == "tenant_demo",
                ApiUsageSnapshot.customer_id == "cust_demo",
            )
            .order_by(ApiUsageSnapshot.observed_at.desc(), ApiUsageSnapshot.id.desc())
            .limit(1)
        )
        latest_completed_bucket = await session.scalar(
            select(ApiUsageBucket)
            .where(
                ApiUsageBucket.tenant_id == "tenant_demo",
                ApiUsageBucket.customer_id == "cust_demo",
                ApiUsageBucket.bucket_end <= datetime.now(UTC).replace(second=0, microsecond=0),
            )
            .order_by(ApiUsageBucket.bucket_end.desc(), ApiUsageBucket.id.desc())
            .limit(1)
        )
        assert latest_snapshot is not None
        assert latest_completed_bucket is not None
        assert latest_snapshot.requests_last_minute == latest_completed_bucket.request_count
        assert latest_snapshot.concurrency_current == latest_completed_bucket.concurrency_end
        remaining_tickets = await session.scalar(select(func.count()).select_from(SupportTicket))
        assert int(remaining_tickets) == ticket_count
    await engine.dispose()


@pytest.mark.asyncio
async def test_demo_temporal_refresh_fails_closed_for_production_or_other_tenant(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine)
    async with factory() as session:
        with pytest.raises(RuntimeError, match="forbidden_in_production"):
            await demo_temporal_preflight(
                session,
                settings=Settings(_env_file=None, app_env="production", auth_mode="production"),
                tenant_id="tenant_demo",
            )
        with pytest.raises(RuntimeError, match="requires_demo_tenant"):
            await demo_temporal_preflight(
                session,
                settings=Settings(_env_file=None, app_env="test", auth_mode="development"),
                tenant_id="tenant_other",
            )
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_demo_temporal_refresh_preserves_conversation_history() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    bootstrap_url = (
        make_url(database_url)
        .set(
            username="supportguard_bootstrap",
            password=os.getenv("BOOTSTRAP_DATABASE_PASSWORD", "supportguard_bootstrap"),
        )
        .render_as_string(hide_password=False)
    )
    owner_engine = create_async_engine(database_url)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    bootstrap_engine = create_async_engine(bootstrap_url)
    bootstrap_factory = async_sessionmaker(bootstrap_engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="development",
        database_url=database_url,
    )
    async with owner_factory() as session:
        before = int(await session.scalar(select(func.count()).select_from(SupportTicket)) or 0)
    async with bootstrap_factory() as session:
        report = await refresh_demo_temporal_fixtures(
            session, settings=settings, tenant_id="tenant_demo"
        )
        await session.commit()
    async with owner_factory() as session:
        after = int(await session.scalar(select(func.count()).select_from(SupportTicket)) or 0)
    assert report.usage_snapshot_count > 0
    assert report.usage_bucket_count > 0
    assert after == before
    await bootstrap_engine.dispose()
    await owner_engine.dispose()
