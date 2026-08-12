from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import uuid4

from fastapi import Depends, Request

from supportguard.api.auth import (
    Principal,
    approver_principal,
    customer_principal,
    principal,
)
from supportguard.contracts.context import RequestContext
from supportguard.db.session import ScopedAsyncSession, ScopedSessionFactory
from supportguard.observability.context import current_request_context
from supportguard.runtime import AppRuntime

CustomerIdentity = Annotated[Principal, Depends(customer_principal)]
ApproverIdentity = Annotated[Principal, Depends(approver_principal)]
AnyIdentity = Annotated[Principal, Depends(principal)]


def runtime(request: Request) -> AppRuntime:
    return request.app.state.runtime  # type: ignore[no-any-return]


def scoped_session_factory(request: Request) -> ScopedSessionFactory:
    if hasattr(request.app.state, "scoped_factory"):
        return cast(ScopedSessionFactory, request.app.state.scoped_factory)
    return runtime(request).scoped_factory


def request_ids() -> tuple[str, str]:
    context = current_request_context.get()
    if context is None:
        return f"request_{uuid4().hex}", f"trace_{uuid4().hex}"
    return context.request_id, context.traceparent or context.trace_id


def _request_scope(request: Request, identity: Principal) -> RequestContext:
    request_id, trace_id = request_ids()
    return RequestContext(
        tenant_id=identity.tenant_id,
        authenticated_actor_id=identity.subject_id,
        authenticated_actor_role=identity.membership_role or identity.role,
        request_id=request_id,
        trace_id=trace_id,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        subject_customer_id=identity.customer_id,
    )


@asynccontextmanager
async def request_session(
    request: Request, identity: Principal
) -> AsyncIterator[ScopedAsyncSession]:
    async with scoped_session_factory(request).request(
        _request_scope(request, identity)
    ) as session:
        yield session
