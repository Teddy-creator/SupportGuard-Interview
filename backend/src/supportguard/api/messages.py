from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from supportguard.api.contracts import (
    MUTATION_RESPONSES,
    CommandAcceptedResponse,
    MessageInput,
)
from supportguard.api.dependencies import CustomerIdentity, request_ids, request_session
from supportguard.api.projections import _accepted_provider_identity, _upgrade_unavailable
from supportguard.services.admission import admit_command
from supportguard.services.commands import CommandAccepted, CommandCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict

router = APIRouter()


class AcceptedMessage(BaseModel):
    """Trusted ingress identity plus untrusted customer text.

    This is the first typed Interview Edition stage. It binds the authenticated
    principal and HTTP idempotency identity before the durable command owner is
    called, but it grants no Agent or Action authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["accepted-message.v1"] = "accepted-message.v1"
    tenant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=8000)
    trace_id: str = Field(min_length=1)
    ticket_id: str | None = None
    grants_action_authority: Literal[False] = False


def _accepted_message(
    *,
    ticket_id: str | None,
    body: MessageInput,
    identity: CustomerIdentity,
    idempotency_key: str | None,
) -> AcceptedMessage:
    if not idempotency_key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key required")
    _, trace_id = request_ids()
    return AcceptedMessage(
        tenant_id=identity.tenant_id,
        customer_id=str(identity.customer_id),
        principal_id=identity.subject_id,
        idempotency_key=idempotency_key,
        message=body.message,
        trace_id=trace_id,
        ticket_id=ticket_id,
    )


async def _persist_accepted_message(
    accepted_message: AcceptedMessage,
    request: Request,
    identity: CustomerIdentity,
) -> CommandAccepted:
    async with request_session(request, identity) as session, session.begin():
        coordinator = CommandCoordinator(
            session,
            provider_identity=_accepted_provider_identity(request),
        )
        if accepted_message.ticket_id is None:
            accepted = await coordinator.accept_new_ticket(
                customer_id=accepted_message.customer_id,
                principal_id=accepted_message.principal_id,
                idempotency_key=accepted_message.idempotency_key,
                message=accepted_message.message,
                trace_id=accepted_message.trace_id,
            )
        else:
            accepted = await coordinator.accept_message(
                ticket_id=accepted_message.ticket_id,
                customer_id=accepted_message.customer_id,
                principal_id=accepted_message.principal_id,
                idempotency_key=accepted_message.idempotency_key,
                message=accepted_message.message,
                trace_id=accepted_message.trace_id,
            )
        if not accepted.reused and not bool(request.app.state.testing):
            await admit_command(
                session,
                request.app.state.redis,
                request.app.state.settings,
                tenant_id=accepted_message.tenant_id,
                principal_id=accepted_message.principal_id,
            )
        return accepted


def _message_conflict(exc: RuntimeConflict, *, creates_ticket: bool) -> JSONResponse:
    if exc.code == "upgrade_in_progress":
        return _upgrade_unavailable()
    if exc.code in {"runtime_backpressure", "command_rate_limited"}:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code) from exc
    if creates_ticket:
        code = (
            status.HTTP_409_CONFLICT
            if exc.code == "idempotency_conflict"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    else:
        code = (
            status.HTTP_409_CONFLICT
            if exc.code in {"idempotency_conflict", "ticket_state_conflict"}
            else status.HTTP_404_NOT_FOUND
        )
    raise HTTPException(code, exc.code) from exc


async def _accept_message(
    *,
    ticket_id: str | None,
    body: MessageInput,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: str | None,
) -> CommandAcceptedResponse | JSONResponse:
    message = _accepted_message(
        ticket_id=ticket_id,
        body=body,
        identity=identity,
        idempotency_key=idempotency_key,
    )
    try:
        accepted = await _persist_accepted_message(message, request, identity)
    except RuntimeConflict as exc:
        return _message_conflict(exc, creates_ticket=ticket_id is None)
    return CommandAcceptedResponse.model_validate(accepted.response())


@router.post(
    "/conversations",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
@router.post(
    "/tickets",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def create_ticket(
    body: MessageInput,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CommandAcceptedResponse | JSONResponse:
    return await _accept_message(
        ticket_id=None,
        body=body,
        request=request,
        identity=identity,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/conversations/{ticket_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
@router.post(
    "/tickets/{ticket_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def append_message(
    ticket_id: str,
    body: MessageInput,
    request: Request,
    identity: CustomerIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CommandAcceptedResponse | JSONResponse:
    return await _accept_message(
        ticket_id=ticket_id,
        body=body,
        request=request,
        identity=identity,
        idempotency_key=idempotency_key,
    )
