from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from supportguard.api.auth import (
    Principal,
)
from supportguard.api.contracts import (
    MUTATION_RESPONSES,
    ConversationLifecycleResponse,
    WithdrawalAcceptedResponse,
    WithdrawalInput,
)
from supportguard.api.dependencies import (
    CustomerIdentity,
    request_ids,
    request_session,
)
from supportguard.services.conversation_lifecycle import ConversationLifecycleCoordinator
from supportguard.services.proposal_withdrawals import ProposalWithdrawalCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict

router = APIRouter()


@router.post(
    "/conversations/{conversation_id}/actions/{approval_id}/withdraw",
    response_model=WithdrawalAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def withdraw_proposal(
    conversation_id: str,
    approval_id: str,
    body: WithdrawalInput,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WithdrawalAcceptedResponse | JSONResponse:
    if not idempotency_key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key required")
    _, trace_id = request_ids()
    try:
        async with request_session(request, identity) as session, session.begin():
            accepted = await ProposalWithdrawalCoordinator(session).withdraw(
                tenant_id=identity.tenant_id,
                customer_id=str(identity.customer_id),
                principal_id=identity.subject_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
                reason=body.reason,
                trace_id=trace_id,
            )
            if accepted.ticket_id != conversation_id:
                raise RuntimeConflict("approval_not_found")
    except RuntimeConflict as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "approval_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, exc.code) from exc
    return WithdrawalAcceptedResponse.model_validate(accepted.response())


async def _transition_conversation(
    *,
    conversation_id: str,
    lifecycle: Literal["active", "archived"],
    request: Request,
    identity: Principal,
    idempotency_key: str | None,
) -> ConversationLifecycleResponse:
    if not idempotency_key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key required")
    _, trace_id = request_ids()
    try:
        async with request_session(request, identity) as session, session.begin():
            accepted = await ConversationLifecycleCoordinator(session).transition(
                tenant_id=identity.tenant_id,
                customer_id=str(identity.customer_id),
                principal_id=identity.subject_id,
                conversation_id=conversation_id,
                lifecycle=lifecycle,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
    except RuntimeConflict as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "conversation_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, exc.code) from exc
    return ConversationLifecycleResponse.model_validate(accepted.response())


@router.post(
    "/conversations/{conversation_id}/archive",
    response_model=ConversationLifecycleResponse,
)
async def archive_conversation(
    conversation_id: str,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ConversationLifecycleResponse:
    return await _transition_conversation(
        conversation_id=conversation_id,
        lifecycle="archived",
        request=request,
        identity=identity,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/conversations/{conversation_id}/restore",
    response_model=ConversationLifecycleResponse,
)
async def restore_conversation(
    conversation_id: str,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ConversationLifecycleResponse:
    return await _transition_conversation(
        conversation_id=conversation_id,
        lifecycle="active",
        request=request,
        identity=identity,
        idempotency_key=idempotency_key,
    )
