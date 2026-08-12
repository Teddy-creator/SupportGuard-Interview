from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select, text

from supportguard.api.auth import (
    CustomerContext,
    Principal,
    PrincipalResolution,
    SubscriptionContext,
    TenantContext,
    app_settings,
    issue_token,
    new_csrf_token,
    resolve_principal_capability,
)
from supportguard.api.contracts import (
    SessionContextResponse,
    SessionRequest,
    SessionResponse,
)
from supportguard.api.dependencies import AnyIdentity, request_session
from supportguard.config import Settings
from supportguard.db.models import (
    ApproverTenantScope,
    Customer,
    Membership,
    Subscription,
    Tenant,
    User,
)
from supportguard.db.session import ScopedAsyncSession

router = APIRouter()


@router.post("/demo-sessions", response_model=SessionResponse)
async def create_demo_session(
    body: SessionRequest, request: Request, response: Response
) -> SessionResponse:
    if app_settings(request).auth_mode != "development":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    csrf_token = new_csrf_token()
    custom_identity = body.tenant_id is not None or body.external_subject is not None
    if custom_identity:
        if not body.tenant_id or not body.external_subject:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "tenant_id and external_subject must be provided together",
            )
        candidate = Principal(
            role=body.role,
            subject_id=body.external_subject,
            tenant_id=body.tenant_id,
            customer_id=body.customer_id if body.role == "customer" else None,
            csrf_token=csrf_token,
        )
        async with request_session(request, candidate) as session:
            resolved = await resolve_principal_capability(
                session,
                subject=body.external_subject,
                tenant_id=body.tenant_id,
            )
        if resolved is None or resolved.role != body.role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Active demo membership required")
        if body.customer_id is not None and resolved.customer_id != body.customer_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Demo customer scope mismatch")
        principal = Principal(
            role=resolved.role,
            subject_id=resolved.subject_id,
            tenant_id=resolved.tenant_id,
            customer_id=resolved.customer_id,
            membership_role=resolved.membership_role,
            csrf_token=csrf_token,
        )
    elif body.role == "approver":
        principal = Principal(
            role="approver",
            subject_id="user_approver_demo",
            tenant_id="tenant_demo",
            membership_role="support_approver",
            csrf_token=csrf_token,
        )
    else:
        if body.customer_id not in {"cust_demo", "cust_other"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown seed customer")
        tenant_id = "tenant_demo" if body.customer_id == "cust_demo" else "tenant_other"
        seed_identity = Principal(
            role="customer",
            subject_id=str(body.customer_id),
            tenant_id=tenant_id,
            customer_id=str(body.customer_id),
            membership_role="customer_member",
        )
        async with request_session(request, seed_identity) as session:
            if session.get_bind().dialect.name == "postgresql":
                customer_exists = await session.scalar(
                    text("SELECT supportguard_api_customer_exists(:customer_id)"),
                    {"customer_id": body.customer_id},
                )
            else:
                customer_exists = await session.get(Customer, body.customer_id) is not None
            if not customer_exists:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Seed database is not ready")
        principal = Principal(
            role="customer",
            subject_id=(
                "user_customer_demo"
                if body.customer_id == "cust_demo"
                else "user_customer_other_demo"
            ),
            tenant_id=tenant_id,
            customer_id=body.customer_id,
            membership_role="customer_admin",
            csrf_token=csrf_token,
        )
    token = issue_token(principal, request.app.state.session_serializer)
    response.set_cookie(
        "supportguard_session",
        token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=8 * 60 * 60,
        path="/",
    )
    return SessionResponse(
        principal=principal,
        csrf_token=csrf_token,
    )


async def _sqlite_principal_resolution(
    session: ScopedAsyncSession,
    identity: Principal,
) -> PrincipalResolution | None:
    user = await session.get(User, identity.subject_id)
    tenant = await session.get(Tenant, identity.tenant_id)
    if user is None or tenant is None or tenant.status != "active":
        return None
    membership = await session.scalar(
        select(Membership).where(
            Membership.tenant_id == identity.tenant_id,
            Membership.user_id == user.id,
            Membership.status == "active",
        )
    )
    if membership is None:
        return None
    customer: Customer | None = None
    subscription: Subscription | None = None
    if identity.role == "approver":
        authorized = await session.get(
            ApproverTenantScope,
            {"user_id": user.id, "tenant_id": identity.tenant_id},
        )
        if authorized is None or membership.role != "support_approver":
            return None
        scope_rows = (
            (
                await session.execute(
                    select(Tenant)
                    .join(ApproverTenantScope, ApproverTenantScope.tenant_id == Tenant.id)
                    .where(
                        ApproverTenantScope.user_id == user.id,
                        Tenant.status == "active",
                    )
                    .order_by(Tenant.name, Tenant.id)
                )
            )
            .scalars()
            .all()
        )
    else:
        customer = await session.scalar(
            select(Customer).where(
                Customer.tenant_id == identity.tenant_id,
                Customer.id == identity.customer_id,
                Customer.status == "active",
            )
        )
        if customer is None or membership.role not in {"customer_member", "customer_admin"}:
            return None
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.tenant_id == identity.tenant_id,
                Subscription.customer_id == customer.id,
            )
        )
        scope_rows = [tenant]
    return PrincipalResolution(
        schema_version="principal-resolution.v2",
        role=identity.role,
        subject_id=user.id,
        display_name=user.display_name,
        tenant_id=tenant.id,
        tenant=TenantContext(id=tenant.id, name=tenant.name, status=tenant.status),
        customer_id=customer.id if customer is not None else None,
        customer=(
            CustomerContext(
                id=customer.id,
                display_name=customer.display_name,
                status=customer.status,
                security_status=customer.security_status,
                region=customer.region,
                version=customer.version,
            )
            if customer is not None
            else None
        ),
        subscription=(
            SubscriptionContext(
                id=subscription.id,
                plan=subscription.plan,
                status=subscription.status,
                balance=subscription.balance,
                currency=subscription.currency,
                rpm_limit=subscription.rpm_limit,
                concurrency_limit=subscription.concurrency_limit,
                version=subscription.version,
            )
            if subscription is not None
            else None
        ),
        accessible_tenants=[
            TenantContext(id=item.id, name=item.name, status=item.status) for item in scope_rows
        ],
        membership_role=membership.role,
    )


@router.get("/session", response_model=SessionContextResponse)
async def session_context(
    request: Request,
    identity: AnyIdentity,
) -> SessionContextResponse:
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            resolved = await resolve_principal_capability(
                session,
                subject=identity.subject_id,
                tenant_id=identity.tenant_id,
            )
        else:
            resolved = await _sqlite_principal_resolution(session, identity)
    if resolved is None or resolved.tenant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Active membership required")
    settings: Settings = request.app.state.settings
    actual_mode = (
        "fake"
        if bool(request.app.state.testing)
        or settings.app_env == "test"
        or settings.demo_fake_provider
        else "worker-owned"
    )
    return SessionContextResponse(
        auth_mode=settings.auth_mode,
        csrf_token=identity.csrf_token if settings.auth_mode == "development" else None,
        principal={
            "id": resolved.subject_id,
            "display_name": resolved.display_name or resolved.subject_id,
            "role": resolved.role,
            "membership_role": resolved.membership_role,
        },
        active_tenant=resolved.tenant,
        customer=resolved.customer,
        subscription=resolved.subscription,
        accessible_tenants=resolved.accessible_tenants,
        configured_runtime={
            "mode": actual_mode,
            "model": ("deterministic-fake" if actual_mode == "fake" else settings.llm_model),
            "actual_run_source": "ticket.latest_run",
        },
    )
