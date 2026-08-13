from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import (
    ApiKeyMetadata,
    ApiRequestTrace,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApproverTenantScope,
    BillingRecord,
    Customer,
    IncidentImpact,
    Membership,
    MutationKillSwitch,
    PlanCatalog,
    ServiceIncident,
    Subscription,
    Tenant,
    User,
)
from supportguard.db.scope import set_local_scope
from supportguard.db.seed_contract import (
    DEMO_BILLING_SERVICE_PERIOD_END,
    DEMO_BILLING_SERVICE_PERIOD_START,
    SeedContractError,
    SeedReceipt,
)
from supportguard.db.seed_validation import validate_seed_contract


def _captured_seed_clock(clock: Callable[[], datetime] | None) -> datetime:
    captured = (clock or (lambda: datetime.now(UTC)))()
    if captured.tzinfo is None:
        raise SeedContractError("seed_clock_must_be_timezone_aware")
    return captured.astimezone(UTC).replace(second=0, microsecond=0)


async def _scope(session: AsyncSession, tenant_id: str) -> None:
    await set_local_scope(
        session,
        tenant_id=tenant_id,
        principal_id="supportguard-bootstrap",
        principal_role="system_worker",
    )


async def seed_demo_data(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] | None = None,
) -> SeedReceipt:
    """Seed two tenants and verify their frozen Interview data contract.

    The wall clock is captured once.  It keeps the live usage fixtures fresh but
    is deliberately excluded from the semantic seed hash.  Re-running the seed
    validates existing resources instead of silently rewriting demo state.
    """

    captured_at = _captured_seed_clock(clock)
    billing_period_start = DEMO_BILLING_SERVICE_PERIOD_START
    billing_period_end = DEMO_BILLING_SERVICE_PERIOD_END
    billing_charged_at = captured_at - timedelta(days=1)
    for tenant in (
        Tenant(id="tenant_demo", name="Aster Labs", status="active"),
        Tenant(id="tenant_other", name="Northwind AI", status="active"),
    ):
        if await session.get(Tenant, tenant.id) is None:
            session.add(tenant)
    for user in (
        User(
            id="user_customer_demo",
            external_subject="oidc-customer-demo",
            display_name="Aster Customer",
        ),
        User(
            id="user_approver_demo",
            external_subject="oidc-approver-demo",
            display_name="Support Approver",
        ),
        User(
            id="user_customer_other_demo",
            external_subject="oidc-customer-other-demo",
            display_name="Northwind Customer",
        ),
    ):
        if await session.get(User, user.id) is None:
            session.add(user)
    if await session.get(PlanCatalog, "catalog_pro_eu_v1") is None:
        session.add(
            PlanCatalog(
                id="catalog_pro_eu_v1",
                plan="pro",
                region="eu-west",
                min_rpm=10,
                max_rpm=120,
                min_concurrency=2,
                max_concurrency=80,
                version=1,
            )
        )
    if await session.get(ServiceIncident, "incident_atlas_eu_resolved") is None:
        session.add(
            ServiceIncident(
                id="incident_atlas_eu_resolved",
                model="atlas-chat",
                region="eu-west",
                status="resolved",
                summary="A short latency incident was resolved and is no longer active.",
                started_at=datetime(2026, 7, 10, 2, 0, tzinfo=UTC),
                resolved_at=datetime(2026, 7, 10, 2, 18, tzinfo=UTC),
            )
        )
    await session.flush()

    await _scope(session, "tenant_demo")
    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        if (
            await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_demo", "action_type": action_type},
            )
            is None
        ):
            session.add(
                MutationKillSwitch(
                    tenant_id="tenant_demo",
                    action_type=action_type,
                    enabled=True,
                    changed_by="supportguard-bootstrap-demo",
                )
            )
    for membership_id, user_id, role in (
        ("mem_demo_customer", "user_customer_demo", "customer_admin"),
        ("mem_demo_approver", "user_approver_demo", "support_approver"),
    ):
        membership = await session.scalar(
            select(Membership).where(
                Membership.tenant_id == "tenant_demo",
                Membership.user_id == user_id,
            )
        )
        if membership is None:
            session.add(
                Membership(
                    id=membership_id,
                    tenant_id="tenant_demo",
                    user_id=user_id,
                    role=role,
                )
            )
    if (
        await session.get(
            ApproverTenantScope,
            {"user_id": "user_approver_demo", "tenant_id": "tenant_demo"},
        )
        is None
    ):
        session.add(ApproverTenantScope(user_id="user_approver_demo", tenant_id="tenant_demo"))
    if await session.get(Customer, "cust_demo") is None:
        session.add(
            Customer(
                id="cust_demo",
                tenant_id="tenant_demo",
                display_name="Aster Labs",
                email="owner@aster-labs.example",
                status="active",
                security_status="normal",
                region="eu-west",
                version=1,
            )
        )
        await session.flush()
        session.add_all(
            [
                Subscription(
                    id="sub_demo",
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    plan="pro",
                    status="active",
                    balance=Decimal("120.00"),
                    currency="USD",
                    rpm_limit=60,
                    concurrency_limit=40,
                    version=3,
                ),
                ApiUsageSnapshot(
                    id="usage_demo_current",
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    observed_at=captured_at,
                    requests_last_minute=32,
                    concurrency_current=40,
                    remaining_balance=Decimal("120.00"),
                ),
                ApiRequestTrace(
                    id="trace_demo_429",
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    request_id="req_demo_429",
                    model="atlas-chat",
                    region="eu-west",
                    status_code=429,
                    error_class="concurrency_limit_exceeded",
                    stage_latency_ms={"connect": 20, "queue": 900, "total": 1100},
                    observed_at=captured_at,
                    version=1,
                ),
                ApiKeyMetadata(
                    id="keymeta_demo_leaked",
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    key_id="key_demo_leaked",
                    fingerprint="fp_demo_leaked",
                    status="active",
                    version=2,
                    last_used_summary={"region": "eu-west", "request_count": 8},
                ),
                BillingRecord(
                    id="bill_demo_original",
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    charged_at=billing_charged_at,
                    service_period_start=billing_period_start,
                    service_period_end=billing_period_end,
                    duplicate_of=None,
                    version=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            BillingRecord(
                id="bill_demo_duplicate",
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                amount=Decimal("49.00"),
                currency="USD",
                status="charged",
                charged_at=billing_charged_at,
                service_period_start=billing_period_start,
                service_period_end=billing_period_end,
                duplicate_of="bill_demo_original",
                version=2,
            )
        )
        await session.flush()

    if await session.get(IncidentImpact, "impact_demo_429") is None:
        session.add(
            IncidentImpact(
                id="impact_demo_429",
                tenant_id="tenant_demo",
                request_trace_id="trace_demo_429",
                incident_id="incident_atlas_eu_resolved",
                impacted=True,
                public_incident_ref="status:atlas-eu-2026-07-10",
            )
        )
    await session.flush()

    await _scope(session, "tenant_other")
    for membership_id, user_id, role in (
        ("mem_other_customer", "user_customer_other_demo", "customer_admin"),
        ("mem_other_approver", "user_approver_demo", "support_approver"),
    ):
        membership = await session.scalar(
            select(Membership).where(
                Membership.tenant_id == "tenant_other",
                Membership.user_id == user_id,
            )
        )
        if membership is None:
            session.add(
                Membership(
                    id=membership_id,
                    tenant_id="tenant_other",
                    user_id=user_id,
                    role=role,
                )
            )
    if (
        await session.get(
            ApproverTenantScope,
            {"user_id": "user_approver_demo", "tenant_id": "tenant_other"},
        )
        is None
    ):
        session.add(ApproverTenantScope(user_id="user_approver_demo", tenant_id="tenant_other"))
    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        if (
            await session.get(
                MutationKillSwitch,
                {"tenant_id": "tenant_other", "action_type": action_type},
            )
            is None
        ):
            session.add(
                MutationKillSwitch(
                    tenant_id="tenant_other",
                    action_type=action_type,
                    enabled=True,
                    changed_by="supportguard-bootstrap-demo",
                )
            )
    if await session.get(Customer, "cust_other") is None:
        session.add(
            Customer(
                id="cust_other",
                tenant_id="tenant_other",
                display_name="Northwind AI",
                email="admin@northwind-ai.example",
                status="active",
                security_status="normal",
                region="us-east",
                version=1,
            )
        )
        await session.flush()
        session.add_all(
            [
                Subscription(
                    id="sub_other",
                    tenant_id="tenant_other",
                    customer_id="cust_other",
                    plan="starter",
                    status="active",
                    balance=Decimal("20.00"),
                    currency="USD",
                    rpm_limit=20,
                    concurrency_limit=5,
                    version=1,
                ),
                BillingRecord(
                    id="bill_other_001",
                    tenant_id="tenant_other",
                    customer_id="cust_other",
                    amount=Decimal("19.00"),
                    currency="USD",
                    status="charged",
                    charged_at=billing_charged_at,
                    service_period_start=billing_period_start,
                    service_period_end=billing_period_end,
                    duplicate_of=None,
                    version=1,
                ),
            ]
        )
    await session.flush()

    window_end = captured_at
    for tenant_id, customer_id, request_base, concurrency_limit in (
        ("tenant_demo", "cust_demo", 3, 40),
        ("tenant_other", "cust_other", 1, 5),
    ):
        await _scope(session, tenant_id)
        existing_bucket = await session.scalar(
            select(ApiUsageBucket.id)
            .where(
                ApiUsageBucket.tenant_id == tenant_id,
                ApiUsageBucket.customer_id == customer_id,
            )
            .limit(1)
        )
        if existing_bucket is None:
            session.add_all(
                [
                    ApiUsageBucket(
                        id=f"usage_bucket_{customer_id}_{minute:04d}",
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        bucket_start=window_end - timedelta(minutes=minute + 1),
                        bucket_end=window_end - timedelta(minutes=minute),
                        request_count=request_base + minute % 4,
                        input_token_count=(request_base + minute % 4) * 120,
                        output_token_count=(request_base + minute % 4) * 40,
                        concurrency_peak=min(concurrency_limit, request_base + minute % 6),
                        concurrency_end=min(concurrency_limit, request_base + minute % 3),
                        source_version=minute + 1,
                    )
                    for minute in range(1440)
                ]
            )
            await session.flush()

    await session.flush()
    return await validate_seed_contract(session, captured_at=captured_at)
