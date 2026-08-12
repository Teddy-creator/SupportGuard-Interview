from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jwt import PyJWKSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from supportguard.config import Settings
from supportguard.contracts.context import RequestContext
from supportguard.db.session import ScopedAsyncSession, ScopedSessionFactory


class Principal(BaseModel):
    role: Literal["customer", "approver"]
    subject_id: str
    tenant_id: str
    customer_id: str | None = None
    membership_role: str | None = None
    csrf_token: str = ""


class TenantContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: str | None = None


class CustomerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    status: str
    security_status: str
    region: str
    version: int


class SubscriptionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    plan: str
    status: str
    balance: Decimal
    currency: str
    rpm_limit: int
    concurrency_limit: int
    version: int


class PrincipalResolution(BaseModel):
    """Minimal, fail-closed result returned by the production auth capability."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["principal-resolution.v1", "principal-resolution.v2"]
    role: Literal["customer", "approver"]
    subject_id: str
    display_name: str | None = None
    tenant_id: str
    tenant: TenantContext | None = None
    customer_id: str | None
    customer: CustomerContext | None = None
    subscription: SubscriptionContext | None = None
    accessible_tenants: list[TenantContext] = Field(default_factory=list)
    membership_role: Literal["customer_member", "customer_admin", "support_approver"]


class OIDCAuthenticator:
    def __init__(self, *, issuer: str, audience: str, jwks_json: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks = PyJWKSet.from_json(jwks_json)

    def decode(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = [key for key in self.jwks.keys if key.key_id == kid]
        if len(keys) != 1:
            raise jwt.InvalidTokenError("JWT kid is not present in configured JWKS")
        return jwt.decode(
            token,
            key=keys[0].key,
            algorithms=["RS256", "ES256"],
            issuer=self.issuer,
            audience=self.audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )


def build_oidc_authenticator(settings: Settings) -> OIDCAuthenticator | None:
    if settings.auth_mode != "production":
        return None
    if not settings.oidc_issuer or not settings.oidc_audience or settings.oidc_jwks_json is None:
        raise RuntimeError("production auth requires OIDC issuer, audience, and JWKS")
    raw = settings.oidc_jwks_json.get_secret_value()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):
        raise RuntimeError("OIDC_JWKS_JSON is not a JWKS document")
    return OIDCAuthenticator(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_json=raw,
    )


def serializer(request: Request) -> URLSafeTimedSerializer:
    return cast(URLSafeTimedSerializer, request.app.state.session_serializer)


def app_settings(request: Request) -> Settings:
    """Return the immutable Settings instance owned by this app lifecycle."""

    return cast(Settings, request.app.state.settings)


def issue_token(principal: Principal, signer: URLSafeTimedSerializer) -> str:
    return signer.dumps(principal.model_dump())


async def resolve_principal_capability(
    session: ScopedAsyncSession, *, subject: str, tenant_id: str
) -> PrincipalResolution | None:
    payload = await session.scalar(
        text("SELECT supportguard_api_resolve_principal(:subject, :tenant_id)"),
        {"subject": subject, "tenant_id": tenant_id},
    )
    if payload is None:
        return None
    if isinstance(payload, str):
        payload = json.loads(payload)
    return PrincipalResolution.model_validate(payload)


async def _production_principal(request: Request, token: str) -> Principal:
    authenticator = cast(OIDCAuthenticator | None, request.app.state.oidc_authenticator)
    if authenticator is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OIDC is unavailable")
    try:
        claims = authenticator.decode(token)
        subject = str(claims["sub"])
        tenant_id = str(claims.get("tenant_id", ""))
        if not tenant_id:
            raise jwt.InvalidTokenError("Active tenant claim is required")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token") from exc
    factory = cast(ScopedSessionFactory, request.app.state.scoped_factory)
    context = RequestContext(
        tenant_id=tenant_id,
        authenticated_actor_id=subject,
        authenticated_actor_role="oidc_candidate",
        request_id=str(request.headers.get("x-request-id", "oidc-auth")),
        trace_id=str(request.headers.get("traceparent", "oidc-auth")),
        deadline=datetime.now(UTC) + timedelta(seconds=15),
    )
    async with factory.request(context) as session:
        try:
            resolved = await resolve_principal_capability(
                session, subject=subject, tenant_id=tenant_id
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Active membership required"
            ) from exc
        if resolved is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Active membership required")
        return Principal(
            role=resolved.role,
            subject_id=resolved.subject_id,
            tenant_id=resolved.tenant_id,
            customer_id=resolved.customer_id,
            membership_role=resolved.membership_role,
        )


async def principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> Principal:
    settings = app_settings(request)
    if settings.auth_mode == "production":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
        return await _production_principal(request, authorization[7:])
    signed = request.cookies.get("supportguard_session")
    if signed is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing development session")
    try:
        payload = serializer(request).loads(signed, max_age=8 * 60 * 60)
        value = Principal.model_validate(payload)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(
            x_csrf_token or "", value.csrf_token
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid CSRF token")
        return value
    except (BadSignature, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid development session") from exc


async def customer_principal(
    value: Annotated[Principal, Depends(principal)],
) -> Principal:
    if value.role != "customer" or value.customer_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Customer session required")
    return value


async def approver_principal(
    value: Annotated[Principal, Depends(principal)],
) -> Principal:
    if value.role != "approver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Approver session required")
    return value


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
