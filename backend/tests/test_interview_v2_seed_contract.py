from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.db.base import Base
from supportguard.db.models import (
    ApiKeyMetadata,
    ApiRequestTrace,
    ApiUsageBucket,
    ApiUsageSnapshot,
    BillingRecord,
    MutationKillSwitch,
    PlanCatalog,
    Subscription,
)
from supportguard.db.seed import seed_demo_data
from supportguard.db.seed_contract import (
    KNOWLEDGE_MANIFEST_SHA256,
    KNOWLEDGE_SOURCE_BUNDLE_SHA256,
    SEED_CONTRACT_SHA256,
    SEED_MANIFEST,
    SEED_VERSION,
    SeedContractError,
    canonical_seed_manifest,
)
from supportguard.db.seed_validation import validate_clean_demo_contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_seed_contract_identity_and_knowledge_binding_are_frozen() -> None:
    assert SEED_VERSION == "interview-seed.v1"
    assert hashlib.sha256(canonical_seed_manifest()).hexdigest() == SEED_CONTRACT_SHA256
    assert SEED_CONTRACT_SHA256 == (
        "c346ab6d909506d24c3300fb7730239290ddd71cabde5f1e853c0d8bba260dc9"
    )
    assert SEED_MANIFEST["clock_contract"] == {
        "capture": "once_per_seed_invocation",
        "precision": "minute",
        "dynamic_fields": [
            "api_usage_snapshots.observed_at",
            "api_request_traces.observed_at",
            "api_usage_buckets.bucket_start",
            "api_usage_buckets.bucket_end",
            "billing_records.charged_at",
        ],
        "semantic_hash_excludes_wall_clock": True,
        "ordinary_seed_allows_explicit_temporal_refresh_rows": True,
    }

    manifest = REPOSITORY_ROOT / "knowledge/manifests/documents.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == KNOWLEDGE_MANIFEST_SHA256
    bundle_lines: list[str] = []
    for path in (manifest, *sorted((REPOSITORY_ROOT / "knowledge/source_docs").glob("*.md"))):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        bundle_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    assert (
        hashlib.sha256("".join(bundle_lines).encode()).hexdigest() == KNOWLEDGE_SOURCE_BUNDLE_SHA256
    )


@pytest.mark.asyncio
async def test_seed_is_repeatable_preserves_first_clock_and_rejects_identity_drift() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_clock = datetime(2026, 8, 12, 3, 4, 59, tzinfo=UTC)
    second_clock = datetime(2026, 8, 13, 8, 9, 10, tzinfo=UTC)
    try:
        async with factory() as session:
            first = await seed_demo_data(session, clock=lambda: first_clock)
            await session.commit()
            snapshot = await session.get(ApiUsageSnapshot, "usage_demo_current")
            trace = await session.get(ApiRequestTrace, "trace_demo_429")
            assert snapshot is not None and trace is not None
            first_observed_at = snapshot.observed_at
            assert trace.observed_at == first_observed_at

        async with factory() as session:
            second = await seed_demo_data(session, clock=lambda: second_clock)
            await session.commit()
            snapshot = await session.get(ApiUsageSnapshot, "usage_demo_current")
            assert snapshot is not None
            assert snapshot.observed_at == first_observed_at

        assert first.version == second.version == SEED_VERSION
        assert first.contract_sha256 == second.contract_sha256 == SEED_CONTRACT_SHA256
        assert first.row_counts == second.row_counts
        assert first.row_counts["api_usage_buckets"] == 2880
        assert first.captured_at == first_clock.replace(second=0, microsecond=0)
        assert second.captured_at == second_clock.replace(second=0, microsecond=0)

        async with factory() as session:
            billing = await session.get(BillingRecord, "bill_demo_duplicate")
            assert billing is not None
            billing.duplicate_of = None
            await session.commit()
        async with factory() as session:
            with pytest.raises(
                SeedContractError, match="seed_resource_drift:bill_demo_duplicate:duplicate_of"
            ):
                await seed_demo_data(session, clock=lambda: second_clock)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_seed_preserves_action_state_and_uses_explicit_clean_preflight() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 8, 12, 3, 4, tzinfo=UTC)
    try:
        async with factory() as session:
            await seed_demo_data(session, clock=lambda: captured_at)
            await session.commit()

        async with factory() as session:
            refresh_start = captured_at + timedelta(minutes=1)
            session.add_all(
                [
                    ApiUsageBucket(
                        id="usage_refresh_test_0000",
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        bucket_start=refresh_start,
                        bucket_end=refresh_start + timedelta(minutes=1),
                        request_count=4,
                        input_token_count=480,
                        output_token_count=160,
                        concurrency_peak=4,
                        concurrency_end=4,
                        source_version=1441,
                    ),
                    ApiUsageSnapshot(
                        id="usage_refresh_test",
                        tenant_id="tenant_demo",
                        customer_id="cust_demo",
                        observed_at=refresh_start,
                        requests_last_minute=4,
                        concurrency_current=4,
                        remaining_balance=Decimal("120.00"),
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            await seed_demo_data(session, clock=lambda: captured_at)
            await session.commit()
            with pytest.raises(
                SeedContractError,
                match="clean_demo_state_drift:usage_buckets:tenant_demo:extra",
            ):
                await validate_clean_demo_contract(session, captured_at=captured_at)

        async with factory() as session:
            billing = await session.get(BillingRecord, "bill_demo_duplicate")
            key = await session.get(ApiKeyMetadata, "keymeta_demo_leaked")
            subscription = await session.get(Subscription, "sub_demo")
            switch = await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_demo", "action_type": "refund"},
            )
            assert billing is not None and key is not None
            assert subscription is not None and switch is not None
            session.add(
                PlanCatalog(
                    id="catalog_enterprise_eu_v1",
                    plan="enterprise",
                    region="eu-west",
                    min_rpm=10,
                    max_rpm=240,
                    min_concurrency=2,
                    max_concurrency=120,
                    version=1,
                )
            )
            billing.status = "refunded"
            billing.version += 1
            key.status = "revoked"
            key.version += 1
            subscription.plan = "enterprise"
            subscription.rpm_limit = 80
            subscription.concurrency_limit = 60
            subscription.version += 1
            switch.enabled = False
            switch.version += 1
            switch.changed_by = "support-operator"
            await session.commit()

        async with factory() as session:
            await seed_demo_data(session, clock=lambda: captured_at)
            await session.commit()
            billing = await session.get(BillingRecord, "bill_demo_duplicate")
            key = await session.get(ApiKeyMetadata, "keymeta_demo_leaked")
            subscription = await session.get(Subscription, "sub_demo")
            switch = await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_demo", "action_type": "refund"},
            )
            assert billing is not None and (billing.status, billing.version) == ("refunded", 3)
            assert key is not None and (key.status, key.version) == ("revoked", 3)
            assert subscription is not None
            assert (
                subscription.plan,
                subscription.rpm_limit,
                subscription.concurrency_limit,
                subscription.version,
            ) == ("enterprise", 80, 60, 4)
            assert switch is not None
            assert (switch.enabled, switch.version, switch.changed_by) == (
                False,
                2,
                "support-operator",
            )
            # The earlier explicit clean-state check was observational: an
            # ordinary seed still preserves authoritative action results.
            billing = await session.get(BillingRecord, "bill_demo_duplicate")
            assert billing is not None and billing.status == "refunded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_rejects_naive_clock() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            with pytest.raises(SeedContractError, match="seed_clock_must_be_timezone_aware"):
                await seed_demo_data(session, clock=lambda: datetime(2026, 8, 12, 3, 4))
    finally:
        await engine.dispose()
