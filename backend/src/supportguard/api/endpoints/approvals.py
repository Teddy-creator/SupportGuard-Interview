from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, select, text

from supportguard.api.approval_projection import (
    ApprovalDetailResponse,
    ApprovalProjectionError,
    project_approval_detail,
)
from supportguard.api.auth import (
    Principal,
)
from supportguard.api.contracts import (
    MUTATION_RESPONSES,
    ApprovalInput,
    ApprovalListItemResponse,
    ApprovalSourceResponse,
    DecisionAcceptedResponse,
    EditApprovalInput,
    RejectionInput,
)
from supportguard.api.dependencies import (
    ApproverIdentity,
    request_ids,
    request_session,
)
from supportguard.api.projections import (
    _apply_approval_action_projection,
    _approval_list_item,
    _project_action_source,
    _sqlite_approval_projection,
    _sqlite_approval_source,
    _upgrade_unavailable,
)
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    CheckpointCommitMarker,
    ProposalRecord,
    SupportTicket,
)
from supportguard.services.admission import admit_command
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.conversation_action_state import (
    ConversationActionStateProjectionError,
    ConversationActionStateProjector,
)
from supportguard.services.runtime_jobs import RuntimeConflict

router = APIRouter()


@router.get("/approvals", response_model=list[ApprovalListItemResponse])
async def list_approvals(request: Request, identity: ApproverIdentity) -> list[dict[str, Any]]:
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text("SELECT supportguard_api_list_approvals(:limit)"), {"limit": 200}
            )
            if not isinstance(payload, list):
                raise RuntimeError("api_list_approvals_capability_invalid")
            result: list[dict[str, Any]] = []
            try:
                for item in payload:
                    if not isinstance(item, dict):
                        raise ConversationActionStateProjectionError(
                            "approval list item is invalid"
                        )
                    source_payload = item.pop("conversation_action_sources", None)
                    action_state = _project_action_source(
                        source_payload,
                        tenant_id=identity.tenant_id,
                    )
                    result.append(
                        _approval_list_item(_apply_approval_action_projection(item, action_state))
                    )
            except ConversationActionStateProjectionError as exc:
                raise RuntimeError("api_list_approvals_projection_invalid") from exc
            return result
        approvals = (
            await session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.tenant_id == identity.tenant_id)
                .order_by(
                    case((ApprovalRequest.status == "pending", 0), else_=1),
                    ApprovalRequest.created_at.desc(),
                    ApprovalRequest.id.desc(),
                )
                .limit(200)
            )
        ).all()
        result = []
        ticket_labels = {
            str(ticket_id): str(title or "未命名对话")
            for ticket_id, title in (
                await session.execute(
                    select(SupportTicket.id, SupportTicket.title).where(
                        SupportTicket.id.in_([item.ticket_id for item in approvals])
                    )
                )
            ).all()
        }
        projector = ConversationActionStateProjector(session)
        for item in approvals:
            run = await session.get(AgentRun, item.run_id) if item.run_id else None
            proposal = (
                await session.get(ProposalRecord, item.proposal_id) if item.proposal_id else None
            )
            marker = (
                await session.get(CheckpointCommitMarker, item.marker_id)
                if item.marker_id
                else None
            )
            legacy_actionable = bool(
                item.status == "pending"
                and run is not None
                and run.status == "interrupted"
                and run.checkpoint_stage == "awaiting_approval"
                and run.checkpoint_id == item.checkpoint_id
            )
            v12_actionable = bool(
                legacy_actionable
                and proposal is not None
                and proposal.status == "bound"
                and marker is not None
                and marker.status == "finalized"
                and run is not None
                and run.canonical_checkpoint_ns == item.canonical_checkpoint_ns
                and run.canonical_checkpoint_hash == item.canonical_checkpoint_hash
            )
            sqlite_action_state = await projector.get_for_approval(
                tenant_id=identity.tenant_id,
                customer_id=item.customer_id,
                approval_id=item.id,
            )
            if sqlite_action_state is None:
                raise RuntimeError("api_list_approvals_projection_invalid")
            list_payload = _apply_approval_action_projection(
                {
                    "id": item.id,
                    "ticket_id": item.ticket_id,
                    "source_label": ticket_labels.get(item.ticket_id, item.ticket_id),
                    "status": item.status,
                    "action_type": item.action_type,
                    "action_payload": item.action_payload,
                    "review_context": item.review_context,
                    "actionable": (
                        legacy_actionable if bool(request.app.state.testing) else v12_actionable
                    ),
                    "created_at": item.created_at,
                },
                sqlite_action_state,
            )
            result.append(_approval_list_item(list_payload))
        return result


@router.get("/approvals/{approval_id}", response_model=ApprovalDetailResponse)
async def approval_detail(
    approval_id: str,
    request: Request,
    identity: ApproverIdentity,
) -> ApprovalDetailResponse:
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text("SELECT supportguard_api_get_approval(:approval_id)"),
                {"approval_id": approval_id},
            )
            if not isinstance(payload, dict):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
            try:
                source_payload = payload.pop("conversation_action_sources", None)
                action_state = _project_action_source(
                    source_payload,
                    tenant_id=identity.tenant_id,
                )
                return project_approval_detail(
                    _apply_approval_action_projection(payload, action_state)
                )
            except (
                ApprovalProjectionError,
                ConversationActionStateProjectionError,
            ) as exc:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "projection_invalid",
                ) from exc
        approval = await session.get(ApprovalRequest, approval_id)
        if approval is None or approval.tenant_id != identity.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
        try:
            payload = await _sqlite_approval_projection(
                session,
                approval,
                testing=bool(request.app.state.testing),
            )
            return ApprovalDetailResponse.model_validate(payload)
        except ApprovalProjectionError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "projection_invalid",
            ) from exc


@router.get(
    "/approvals/{approval_id}/source",
    response_model=ApprovalSourceResponse,
)
async def approval_source(
    approval_id: str,
    request: Request,
    identity: ApproverIdentity,
    before_sequence: Annotated[int | None, Query(ge=1)] = None,
    before_message_id: Annotated[
        str | None,
        Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    if (before_sequence is None) != (before_message_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "approval_source_cursor_invalid",
        )
    async with request_session(request, identity) as session:
        if session.get_bind().dialect.name == "postgresql":
            payload = await session.scalar(
                text(
                    "SELECT supportguard_api_get_approval_source("
                    ":approval_id,:before_sequence,:before_message_id,:limit)"
                ),
                {
                    "approval_id": approval_id,
                    "before_sequence": before_sequence,
                    "before_message_id": before_message_id,
                    "limit": limit,
                },
            )
        else:
            approval = await session.get(ApprovalRequest, approval_id)
            try:
                payload = (
                    None
                    if approval is None or approval.tenant_id != identity.tenant_id
                    else await _sqlite_approval_source(
                        session,
                        approval,
                        before_sequence=before_sequence,
                        before_message_id=before_message_id,
                        limit=limit,
                    )
                )
            except RuntimeConflict as exc:
                code = (
                    status.HTTP_422_UNPROCESSABLE_ENTITY
                    if exc.code == "approval_source_cursor_invalid"
                    else status.HTTP_409_CONFLICT
                )
                raise HTTPException(code, exc.code) from exc
        if isinstance(payload, dict) and payload.get("error_code"):
            error_code = str(payload["error_code"])
            code = (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if error_code == "approval_source_cursor_invalid"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(code, error_code)
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval source not found")
        return cast(dict[str, Any], payload)


async def _resume_approval(
    approval_id: str,
    action: str,
    request: Request,
    identity: Principal,
    reason: str,
    approver_note: str | None,
    idempotency_key: str | None = None,
    **extra: Any,
) -> DecisionAcceptedResponse | JSONResponse:
    if not idempotency_key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key required")
    _, trace_id = request_ids()
    binding_conflict = False
    try:
        async with request_session(request, identity) as session, session.begin():
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id=identity.tenant_id,
                approval_id=approval_id,
                decision=action,
                actor_id=identity.subject_id,
                idempotency_key=idempotency_key,
                reason=reason,
                approver_note=approver_note,
                trace_id=trace_id,
                edited_payload=(
                    cast(dict[str, object], extra["edited_payload"])
                    if action == "edit_and_approve"
                    else None
                ),
            )
            if action != "reject" and not accepted.reused and not bool(request.app.state.testing):
                await admit_command(
                    session,
                    request.app.state.redis,
                    request.app.state.settings,
                    tenant_id=identity.tenant_id,
                    principal_id=identity.subject_id,
                )
    except RuntimeConflict as exc:
        if exc.code == "upgrade_in_progress":
            return _upgrade_unavailable()
        if exc.code in {"runtime_backpressure", "command_rate_limited"}:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code) from exc
        if exc.code == "approval_edit_not_allowed":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                exc.code,
            ) from exc
        if exc.code in {"checkpoint_binding_conflict", "approval_state_conflict"}:
            try:
                async with (
                    request_session(request, identity) as stale_session,
                    stale_session.begin(),
                ):
                    await ApprovalCommandCoordinator(stale_session).mark_binding_stale(
                        tenant_id=identity.tenant_id,
                        approval_id=approval_id,
                    )
            except RuntimeConflict as stale_exc:
                if exc.code == "checkpoint_binding_conflict":
                    stale_code = (
                        status.HTTP_404_NOT_FOUND
                        if stale_exc.code == "approval_not_found"
                        else status.HTTP_409_CONFLICT
                    )
                    raise HTTPException(stale_code, stale_exc.code) from stale_exc
            else:
                # A repeated decision sees the already-stale Approval first.
                # The convergence capability proves whether that stale state
                # belongs to this exact checkpoint-binding terminalization.
                binding_conflict = True
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "approval_not_found"
            else status.HTTP_409_CONFLICT
        )
        detail = "checkpoint_binding_conflict" if binding_conflict else exc.code
        raise HTTPException(code, detail) from exc
    return DecisionAcceptedResponse.model_validate(accepted.response())


@router.post(
    "/approvals/{approval_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DecisionAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def approve(
    approval_id: str,
    body: ApprovalInput,
    request: Request,
    identity: ApproverIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DecisionAcceptedResponse | JSONResponse:
    return await _resume_approval(
        approval_id,
        "approve",
        request,
        identity,
        body.reason,
        body.approver_note,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/approvals/{approval_id}/edit-and-approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DecisionAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def edit_and_approve(
    approval_id: str,
    body: EditApprovalInput,
    request: Request,
    identity: ApproverIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DecisionAcceptedResponse | JSONResponse:
    return await _resume_approval(
        approval_id,
        "edit_and_approve",
        request,
        identity,
        body.reason or "Approved after review of an allowlisted change.",
        body.approver_note,
        idempotency_key=idempotency_key,
        edited_payload=body.edited_payload(),
    )


@router.post(
    "/approvals/{approval_id}/reject",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DecisionAcceptedResponse,
    responses=MUTATION_RESPONSES,
)
async def reject(
    approval_id: str,
    body: RejectionInput,
    request: Request,
    identity: ApproverIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DecisionAcceptedResponse | JSONResponse:
    return await _resume_approval(
        approval_id,
        "reject",
        request,
        identity,
        body.reason,
        body.approver_note,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/approvals/{approval_id}/manual-takeover",
    status_code=status.HTTP_409_CONFLICT,
    response_model=None,
)
async def manual_takeover(
    approval_id: str,
    body: ApprovalInput,
    request: Request,
    identity: ApproverIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> None:
    # Historical manual-takeover rows and internal fail-closed escalation remain
    # readable and recoverable. The public approver product does not implement a
    # human case ownership workflow, so accepting a new decision here would
    # create a state the product cannot close.
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "manual_takeover_public_unsupported",
    )
