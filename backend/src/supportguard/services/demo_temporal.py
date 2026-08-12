from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.config import Settings
from supportguard.db.models import (
    ApiKeyMetadata,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    Customer,
    Subscription,
    SupportTicket,
)
from supportguard.db.scope import set_local_scope


@dataclass(frozen=True)
class DemoTemporalReport:
    schema: str
    tenant_id: str
    mode: str
    refreshed_at: str
    usage_snapshot_count: int
    usage_bucket_count: int
    latest_snapshot_age_seconds: int | None

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DemoResourceReport:
    schema: str
    tenant_id: str
    ready: bool
    customer_status: str | None
    duplicate_billing_status: str | None
    api_key_status: str | None
    subscription_status: str | None
    concurrency_limit: int | None
    conversation_count: int
    pending_approval_count: int
    business_action_count: int
    violations: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _authorize(settings: Settings, tenant_id: str) -> None:
    if settings.app_env == "production" or settings.auth_mode == "production":
        raise RuntimeError("demo_temporal_refresh_forbidden_in_production")
    if tenant_id != "tenant_demo":
        raise RuntimeError("demo_temporal_refresh_requires_demo_tenant")


def demo_coverage_window_end(now: datetime) -> datetime:
    """Choose a Demo-only bucket horizon that survives an imminent minute rollover."""

    boundary = now.replace(second=0, microsecond=0)
    if now.second >= 55:
        return boundary + timedelta(minutes=1)
    return boundary


async def demo_temporal_preflight(
    session: AsyncSession, *, settings: Settings, tenant_id: str
) -> DemoTemporalReport:
    _authorize(settings, tenant_id)
    await set_local_scope(
        session,
        tenant_id=tenant_id,
        principal_id="demo_temporal_cli",
        principal_role="system_worker",
    )
    now = datetime.now(UTC)
    customer = await session.scalar(select(Customer).where(Customer.tenant_id == tenant_id))
    if customer is None:
        raise RuntimeError("demo_temporal_customer_missing")
    snapshots = list(
        (
            await session.scalars(
                select(ApiUsageSnapshot).where(
                    ApiUsageSnapshot.tenant_id == tenant_id,
                    ApiUsageSnapshot.customer_id == customer.id,
                )
            )
        ).all()
    )
    buckets = list(
        (
            await session.scalars(
                select(ApiUsageBucket).where(
                    ApiUsageBucket.tenant_id == tenant_id,
                    ApiUsageBucket.customer_id == customer.id,
                )
            )
        ).all()
    )
    observed_times = [
        item.observed_at
        if item.observed_at.tzinfo is not None
        else item.observed_at.replace(tzinfo=UTC)
        for item in snapshots
    ]
    latest = max(observed_times, default=None)
    return DemoTemporalReport(
        schema="demo-temporal-report.v1",
        tenant_id=tenant_id,
        mode="preflight",
        refreshed_at=now.isoformat(),
        usage_snapshot_count=len(snapshots),
        usage_bucket_count=len(buckets),
        latest_snapshot_age_seconds=(
            None if latest is None else max(0, int((now - latest).total_seconds()))
        ),
    )


async def demo_resource_preflight(
    session: AsyncSession, *, settings: Settings, tenant_id: str
) -> DemoResourceReport:
    _authorize(settings, tenant_id)
    await set_local_scope(
        session,
        tenant_id=tenant_id,
        principal_id="demo_resource_cli",
        principal_role="system_worker",
    )
    customer = await session.get(Customer, "cust_demo")
    billing = await session.get(BillingRecord, "bill_demo_duplicate")
    key = await session.scalar(
        select(ApiKeyMetadata).where(
            ApiKeyMetadata.tenant_id == tenant_id,
            ApiKeyMetadata.key_id == "key_demo_leaked",
        )
    )
    subscription = await session.get(Subscription, "sub_demo")
    conversation_count = int(
        await session.scalar(
            select(func.count()).select_from(SupportTicket).where(
                SupportTicket.tenant_id == tenant_id
            )
        )
        or 0
    )
    pending_approval_count = int(
        await session.scalar(
            select(func.count()).select_from(ApprovalRequest).where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.status.in_(("pending", "approved", "executing")),
            )
        )
        or 0
    )
    business_action_count = int(
        await session.scalar(
            select(func.count()).select_from(BusinessAction).where(
                BusinessAction.tenant_id == tenant_id
            )
        )
        or 0
    )
    checks = {
        "customer_not_active": customer is None or customer.status != "active",
        "duplicate_billing_not_charged": billing is None or billing.status != "charged",
        "api_key_not_active": key is None or key.status != "active",
        "subscription_not_active": subscription is None or subscription.status != "active",
        "concurrency_limit_not_40": (
            subscription is None or subscription.concurrency_limit != 40
        ),
        "demo_has_conversations": conversation_count != 0,
        "demo_has_pending_approvals": pending_approval_count != 0,
        "demo_has_business_actions": business_action_count != 0,
    }
    violations = tuple(name for name, failed in checks.items() if failed)
    return DemoResourceReport(
        schema="demo-resource-report.v1",
        tenant_id=tenant_id,
        ready=not violations,
        customer_status=None if customer is None else customer.status,
        duplicate_billing_status=None if billing is None else billing.status,
        api_key_status=None if key is None else key.status,
        subscription_status=None if subscription is None else subscription.status,
        concurrency_limit=(
            None if subscription is None else subscription.concurrency_limit
        ),
        conversation_count=conversation_count,
        pending_approval_count=pending_approval_count,
        business_action_count=business_action_count,
        violations=violations,
    )


async def refresh_demo_temporal_fixtures(
    session: AsyncSession, *, settings: Settings, tenant_id: str
) -> DemoTemporalReport:
    _authorize(settings, tenant_id)
    before = await demo_temporal_preflight(session, settings=settings, tenant_id=tenant_id)
    if before.usage_snapshot_count == 0 or before.usage_bucket_count == 0:
        raise RuntimeError("demo_temporal_fixture_incomplete")
    customer = await session.scalar(select(Customer).where(Customer.tenant_id == tenant_id))
    if customer is None:
        raise RuntimeError("demo_temporal_customer_missing")
    snapshots = list(
        (
            await session.scalars(
                select(ApiUsageSnapshot).where(
                    ApiUsageSnapshot.tenant_id == tenant_id,
                    ApiUsageSnapshot.customer_id == customer.id,
                )
            )
        ).all()
    )
    buckets = list(
        (
            await session.scalars(
                select(ApiUsageBucket)
                .where(
                    ApiUsageBucket.tenant_id == tenant_id,
                    ApiUsageBucket.customer_id == customer.id,
                )
                .order_by(ApiUsageBucket.bucket_start, ApiUsageBucket.id)
            )
        ).all()
    )
    now = datetime.now(UTC)
    window_end = demo_coverage_window_end(now)
    original_end = max(item.bucket_end for item in buckets)
    if original_end.tzinfo is None:
        original_end = original_end.replace(tzinfo=UTC)
    shift = window_end - original_end
    missing_minutes = int(shift.total_seconds() // 60)
    if missing_minutes < 0:
        raise RuntimeError("demo_temporal_fixture_in_future")
    append_count = min(missing_minutes, 1440)
    append_start = window_end - timedelta(minutes=append_count)
    epoch = int(window_end.timestamp())
    if append_count:
        max_source_version = max(item.source_version for item in buckets)
        for index in range(append_count):
            template = buckets[index % len(buckets)]
            start = append_start + timedelta(minutes=index)
            session.add(
                ApiUsageBucket(
                    id=f"usage_refresh_{epoch}_{index:04d}",
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    bucket_start=start,
                    bucket_end=start + timedelta(minutes=1),
                    request_count=template.request_count,
                    input_token_count=template.input_token_count,
                    output_token_count=template.output_token_count,
                    concurrency_peak=template.concurrency_peak,
                    concurrency_end=template.concurrency_end,
                    source_version=max_source_version + index + 1,
                )
            )
    snapshot_id = f"usage_refresh_{epoch}"
    snapshot_exists = await session.get(ApiUsageSnapshot, snapshot_id) is not None
    if not snapshot_exists:
        latest = max(snapshots, key=lambda item: item.observed_at)
        session.add(
            ApiUsageSnapshot(
                id=snapshot_id,
                tenant_id=tenant_id,
                customer_id=customer.id,
                observed_at=now,
                requests_last_minute=latest.requests_last_minute,
                concurrency_current=latest.concurrency_current,
                remaining_balance=latest.remaining_balance,
            )
        )
    await session.flush()
    return DemoTemporalReport(
        schema="demo-temporal-report.v1",
        tenant_id=tenant_id,
        mode="refresh",
        refreshed_at=now.isoformat(),
        usage_snapshot_count=len(snapshots) + int(not snapshot_exists),
        usage_bucket_count=len(buckets) + append_count,
        latest_snapshot_age_seconds=0,
    )
