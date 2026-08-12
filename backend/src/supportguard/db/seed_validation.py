from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
    KNOWLEDGE_MANIFEST_SHA256,
    KNOWLEDGE_SOURCE_BUNDLE_SHA256,
    SEED_CONTRACT_SHA256,
    SEED_VERSION,
    SeedContractError,
    SeedReceipt,
)


def _comparable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    return value


async def _scope(session: AsyncSession, tenant_id: str) -> None:
    await set_local_scope(
        session,
        tenant_id=tenant_id,
        principal_id="supportguard-bootstrap",
        principal_role="system_worker",
    )


async def _require_fields(
    session: AsyncSession,
    model: type[Any],
    identity: object,
    label: str,
    expected: dict[str, object],
) -> None:
    row = await session.get(model, identity)
    if row is None:
        raise SeedContractError(f"seed_resource_missing:{label}")
    mismatches = sorted(
        field
        for field, expected_value in expected.items()
        if _comparable(getattr(row, field)) != _comparable(expected_value)
    )
    if mismatches:
        raise SeedContractError(f"seed_resource_drift:{label}:{','.join(mismatches)}")


async def _require_membership(
    session: AsyncSession,
    membership_id: str,
    tenant_id: str,
    user_id: str,
    role: str,
) -> None:
    row = await session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == user_id,
        )
    )
    if row is None:
        raise SeedContractError(f"seed_resource_missing:membership:{tenant_id}:{user_id}")
    if row.id != membership_id or row.role != role or row.status != "active":
        raise SeedContractError(f"seed_resource_drift:membership:{tenant_id}:{user_id}")


async def _require_usage_buckets(
    session: AsyncSession,
    tenant_id: str,
    customer_id: str,
    request_base: int,
    concurrency_limit: int,
    *,
    allow_additional: bool = True,
) -> None:
    seed_prefix = f"usage_bucket_{customer_id}_".replace("_", r"\_")
    rows = (
        await session.scalars(
            select(ApiUsageBucket)
            .where(
                ApiUsageBucket.tenant_id == tenant_id,
                ApiUsageBucket.customer_id == customer_id,
                ApiUsageBucket.id.like(f"{seed_prefix}%", escape="\\"),
            )
            .order_by(ApiUsageBucket.id)
        )
    ).all()
    if len(rows) != 1440:
        raise SeedContractError(f"seed_resource_drift:usage_buckets:{tenant_id}:count")
    window_end: datetime | None = None
    for minute, row in enumerate(rows):
        expected_id = f"usage_bucket_{customer_id}_{minute:04d}"
        requests = request_base + minute % 4
        implied_window_end = row.bucket_end + timedelta(minutes=minute)
        if window_end is None:
            window_end = implied_window_end
        valid = (
            row.id == expected_id
            and row.bucket_end - row.bucket_start == timedelta(minutes=1)
            and implied_window_end == window_end
            and row.request_count == requests
            and row.input_token_count == requests * 120
            and row.output_token_count == requests * 40
            and row.concurrency_peak == min(concurrency_limit, request_base + minute % 6)
            and row.concurrency_end == min(concurrency_limit, request_base + minute % 3)
            and row.source_version == minute + 1
        )
        if not valid:
            raise SeedContractError(f"seed_resource_drift:usage_buckets:{tenant_id}:{expected_id}")
    if not allow_additional:
        total = len(
            (
                await session.scalars(
                    select(ApiUsageBucket.id).where(
                        ApiUsageBucket.tenant_id == tenant_id,
                        ApiUsageBucket.customer_id == customer_id,
                    )
                )
            ).all()
        )
        if total != len(rows):
            raise SeedContractError(f"clean_demo_state_drift:usage_buckets:{tenant_id}:extra")


_GLOBAL_RESOURCES: tuple[tuple[type[Any], object, str, dict[str, object]], ...] = (
    (Tenant, "tenant_demo", "tenant_demo", {"name": "Aster Labs", "status": "active"}),
    (Tenant, "tenant_other", "tenant_other", {"name": "Northwind AI", "status": "active"}),
    (
        User,
        "user_customer_demo",
        "user_customer_demo",
        {"external_subject": "oidc-customer-demo", "display_name": "Aster Customer"},
    ),
    (
        User,
        "user_approver_demo",
        "user_approver_demo",
        {"external_subject": "oidc-approver-demo", "display_name": "Support Approver"},
    ),
    (
        User,
        "user_customer_other_demo",
        "user_customer_other_demo",
        {"external_subject": "oidc-customer-other-demo", "display_name": "Northwind Customer"},
    ),
    (
        PlanCatalog,
        "catalog_pro_eu_v1",
        "catalog_pro_eu_v1",
        {
            "plan": "pro",
            "region": "eu-west",
            "min_rpm": 10,
            "max_rpm": 120,
            "min_concurrency": 2,
            "max_concurrency": 80,
            "version": 1,
        },
    ),
    (
        ServiceIncident,
        "incident_atlas_eu_resolved",
        "incident_atlas_eu_resolved",
        {
            "model": "atlas-chat",
            "region": "eu-west",
            "status": "resolved",
            "summary": "A short latency incident was resolved and is no longer active.",
            "started_at": datetime(2026, 7, 10, 2, 0, tzinfo=UTC),
            "resolved_at": datetime(2026, 7, 10, 2, 18, tzinfo=UTC),
        },
    ),
)


_TENANT_RESOURCES: dict[str, tuple[tuple[type[Any], object, str, dict[str, object]], ...]] = {
    "tenant_demo": (
        (
            Customer,
            "cust_demo",
            "cust_demo",
            {
                "tenant_id": "tenant_demo",
                "display_name": "Aster Labs",
                "email": "owner@aster-labs.example",
                "status": "active",
                "security_status": "normal",
                "region": "eu-west",
                "version": 1,
            },
        ),
        (
            Subscription,
            "sub_demo",
            "sub_demo",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "currency": "USD",
            },
        ),
        (
            ApiUsageSnapshot,
            "usage_demo_current",
            "usage_demo_current",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "requests_last_minute": 32,
                "concurrency_current": 40,
                "remaining_balance": Decimal("120.00"),
            },
        ),
        (
            ApiRequestTrace,
            "trace_demo_429",
            "trace_demo_429",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "request_id": "req_demo_429",
                "model": "atlas-chat",
                "region": "eu-west",
                "status_code": 429,
                "error_class": "concurrency_limit_exceeded",
                "stage_latency_ms": {"connect": 20, "queue": 900, "total": 1100},
                "version": 1,
            },
        ),
        (
            ApiKeyMetadata,
            "keymeta_demo_leaked",
            "keymeta_demo_leaked",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "key_id": "key_demo_leaked",
                "fingerprint": "fp_demo_leaked",
                "last_used_summary": {"region": "eu-west", "request_count": 8},
            },
        ),
        (
            BillingRecord,
            "bill_demo_original",
            "bill_demo_original",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "amount": Decimal("49.00"),
                "currency": "USD",
                "duplicate_of": None,
            },
        ),
        (
            BillingRecord,
            "bill_demo_duplicate",
            "bill_demo_duplicate",
            {
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "amount": Decimal("49.00"),
                "currency": "USD",
                "duplicate_of": "bill_demo_original",
            },
        ),
        (
            IncidentImpact,
            "impact_demo_429",
            "impact_demo_429",
            {
                "tenant_id": "tenant_demo",
                "request_trace_id": "trace_demo_429",
                "incident_id": "incident_atlas_eu_resolved",
                "impacted": True,
                "public_incident_ref": "status:atlas-eu-2026-07-10",
            },
        ),
    ),
    "tenant_other": (
        (
            Customer,
            "cust_other",
            "cust_other",
            {
                "tenant_id": "tenant_other",
                "display_name": "Northwind AI",
                "email": "admin@northwind-ai.example",
                "status": "active",
                "security_status": "normal",
                "region": "us-east",
                "version": 1,
            },
        ),
        (
            Subscription,
            "sub_other",
            "sub_other",
            {
                "tenant_id": "tenant_other",
                "customer_id": "cust_other",
                "currency": "USD",
            },
        ),
        (
            BillingRecord,
            "bill_other_001",
            "bill_other_001",
            {
                "tenant_id": "tenant_other",
                "customer_id": "cust_other",
                "amount": Decimal("19.00"),
                "currency": "USD",
                "duplicate_of": None,
            },
        ),
    ),
}


_MEMBERSHIPS = {
    "tenant_demo": (
        ("mem_demo_customer", "user_customer_demo", "customer_admin"),
        ("mem_demo_approver", "user_approver_demo", "support_approver"),
    ),
    "tenant_other": (
        ("mem_other_customer", "user_customer_other_demo", "customer_admin"),
        ("mem_other_approver", "user_approver_demo", "support_approver"),
    ),
}


# These are initial demo values, not immutable seed identity.  The three
# approved action pipelines legitimately change them and increment versions.
# Ordinary idempotent bootstrap must preserve that authoritative business
# state.  Call ``validate_clean_demo_contract`` explicitly when an operator
# needs to prove that a fresh demo is still at its initial state.
_CLEAN_DEMO_MUTABLE_STATE: dict[
    str, tuple[tuple[type[Any], object, str, dict[str, object]], ...]
] = {
    "tenant_demo": (
        (
            Subscription,
            "sub_demo",
            "sub_demo",
            {
                "plan": "pro",
                "status": "active",
                "balance": Decimal("120.00"),
                "rpm_limit": 60,
                "concurrency_limit": 40,
                "version": 3,
            },
        ),
        (
            ApiKeyMetadata,
            "keymeta_demo_leaked",
            "keymeta_demo_leaked",
            {"status": "active", "version": 2},
        ),
        (
            BillingRecord,
            "bill_demo_original",
            "bill_demo_original",
            {"status": "charged", "version": 1},
        ),
        (
            BillingRecord,
            "bill_demo_duplicate",
            "bill_demo_duplicate",
            {"status": "charged", "version": 2},
        ),
    ),
    "tenant_other": (
        (
            Subscription,
            "sub_other",
            "sub_other",
            {
                "plan": "starter",
                "status": "active",
                "balance": Decimal("20.00"),
                "rpm_limit": 20,
                "concurrency_limit": 5,
                "version": 1,
            },
        ),
        (
            BillingRecord,
            "bill_other_001",
            "bill_other_001",
            {"status": "charged", "version": 1},
        ),
    ),
}


async def _require_kill_switch_identity(session: AsyncSession, tenant_id: str) -> None:
    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        await _require_fields(
            session,
            MutationKillSwitch,
            {"tenant_id": tenant_id, "action_type": action_type},
            f"kill_switch:{tenant_id}:{action_type}",
            {},
        )


async def _require_clean_demo_state(session: AsyncSession, tenant_id: str) -> None:
    for model, identity, label, expected in _CLEAN_DEMO_MUTABLE_STATE[tenant_id]:
        await _require_fields(session, model, identity, label, expected)
    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        await _require_fields(
            session,
            MutationKillSwitch,
            {"tenant_id": tenant_id, "action_type": action_type},
            f"kill_switch:{tenant_id}:{action_type}",
            {"enabled": True, "version": 1, "changed_by": "supportguard-bootstrap-demo"},
        )
    customer_id, request_base, concurrency_limit = (
        ("cust_demo", 3, 40) if tenant_id == "tenant_demo" else ("cust_other", 1, 5)
    )
    await _require_usage_buckets(
        session,
        tenant_id,
        customer_id,
        request_base,
        concurrency_limit,
        allow_additional=False,
    )
    snapshots = len(
        (
            await session.scalars(
                select(ApiUsageSnapshot.id).where(
                    ApiUsageSnapshot.tenant_id == tenant_id,
                    ApiUsageSnapshot.customer_id == customer_id,
                )
            )
        ).all()
    )
    expected_snapshots = 1 if tenant_id == "tenant_demo" else 0
    if snapshots != expected_snapshots:
        raise SeedContractError(f"clean_demo_state_drift:usage_snapshots:{tenant_id}:count")


async def validate_seed_contract(
    session: AsyncSession,
    *,
    captured_at: datetime,
) -> SeedReceipt:
    for model, identity, label, expected in _GLOBAL_RESOURCES:
        await _require_fields(session, model, identity, label, expected)
    for tenant_id in ("tenant_demo", "tenant_other"):
        await _scope(session, tenant_id)
        for membership_id, user_id, role in _MEMBERSHIPS[tenant_id]:
            await _require_membership(session, membership_id, tenant_id, user_id, role)
        await _require_fields(
            session,
            ApproverTenantScope,
            {"user_id": "user_approver_demo", "tenant_id": tenant_id},
            f"approver_scope:{tenant_id}",
            {},
        )
        await _require_kill_switch_identity(session, tenant_id)
        for model, identity, label, expected in _TENANT_RESOURCES[tenant_id]:
            await _require_fields(session, model, identity, label, expected)
        customer_id, request_base, concurrency_limit = (
            ("cust_demo", 3, 40) if tenant_id == "tenant_demo" else ("cust_other", 1, 5)
        )
        await _require_usage_buckets(
            session, tenant_id, customer_id, request_base, concurrency_limit
        )

    return SeedReceipt(
        version=SEED_VERSION,
        contract_sha256=SEED_CONTRACT_SHA256,
        captured_at=captured_at,
        row_counts={
            "tenants": 2,
            "users": 3,
            "memberships": 4,
            "approver_tenant_scopes": 2,
            "mutation_kill_switches": 6,
            "customers": 2,
            "subscriptions": 2,
            "billing_records": 3,
            "api_key_metadata": 1,
            "api_request_traces": 1,
            "api_usage_snapshots": 1,
            "api_usage_buckets": 2880,
            "service_incidents": 1,
            "incident_impacts": 1,
            "plan_catalog": 1,
        },
        knowledge_manifest_sha256=KNOWLEDGE_MANIFEST_SHA256,
        knowledge_source_bundle_sha256=KNOWLEDGE_SOURCE_BUNDLE_SHA256,
    )


async def validate_clean_demo_contract(
    session: AsyncSession,
    *,
    captured_at: datetime,
) -> SeedReceipt:
    """Explicitly prove clean initial mutable state without resetting it.

    This is intentionally separate from ``validate_seed_contract`` so an
    ordinary process restart cannot silently undo an approved business action.
    A failed preflight is observational only; clean reset remains an explicit
    environment operation.
    """

    receipt = await validate_seed_contract(session, captured_at=captured_at)
    for tenant_id in ("tenant_demo", "tenant_other"):
        await _scope(session, tenant_id)
        await _require_clean_demo_state(session, tenant_id)
    return receipt
