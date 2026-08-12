from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from supportguard.actions.service import ACTION_SPECS, get_action_spec_or_none
from supportguard.contracts.errors import RuntimeConflict
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    ConversationTurn,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
)
from supportguard.services.turn_activation import activate_next_turn

ACTIVE_APPROVAL_STATUSES: Final[frozenset[str]] = frozenset({"pending", "approved"})
TERMINAL_APPROVAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"executed", "rejected", "stale", "withdrawn", "failed", "manual_takeover"}
)
NEW_APPROVAL_STATUSES: Final[frozenset[str]] = frozenset(
    ACTIVE_APPROVAL_STATUSES | (TERMINAL_APPROVAL_STATUSES - {"manual_takeover"})
)
APPROVAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "pending": frozenset({"approved", "rejected", "withdrawn", "stale", "failed"}),
    "approved": frozenset({"executed", "stale", "failed"}),
}
ACTION_RESOURCE_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {action_type: action_spec.resource_field for action_type, action_spec in ACTION_SPECS.items()}
)


async def activate_next_turn_and_converge_ticket(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    trace_id: str,
    default_status: str,
) -> str:
    """Release one application-fallback lane, then derive the Ticket projection.

    Restricted PostgreSQL workers perform both steps inside the owner-controlled
    finish capability.  SQLite and deterministic application fallbacks need the
    same ordering explicitly: first promote the oldest accepted Turn, then
    derive the aggregate status from every remaining Turn, Job, and Approval.
    """

    if session.get_bind().dialect.name == "postgresql":
        raise RuntimeConflict("ticket_convergence_capability_required")
    await activate_next_turn(session, ticket=ticket, trace_id=trace_id)
    return await ActionLifecycleService(session).converge_ticket(
        ticket,
        default_status=default_status,
    )


@dataclass(frozen=True)
class CanonicalApprovalIdentity:
    tenant_id: str
    customer_id: str
    action_type: str
    resource_type: str
    resource_id: str
    resource_version: int
    origin_turn_id: str


@dataclass(frozen=True)
class ApprovalTransition:
    previous_status: str
    status: str
    changed: bool


def canonical_approval_identity(
    *,
    proposal: ProposalRecord,
    run: AgentRun,
    customer_id: str,
) -> CanonicalApprovalIdentity:
    """Build trusted identity from persisted proposal/run bindings, never action JSON."""

    if proposal.tenant_id != run.tenant_id or proposal.run_id != run.id:
        raise RuntimeConflict("approval_canonical_identity_invalid")
    return canonical_approval_identity_values(
        tenant_id=proposal.tenant_id,
        customer_id=customer_id,
        action_type=proposal.action_type,
        resource_id=proposal.resource_id,
        resource_version=proposal.resource_version,
        run=run,
    )


def canonical_approval_identity_values(
    *,
    tenant_id: str,
    customer_id: str,
    action_type: str,
    resource_id: str,
    resource_version: int,
    run: AgentRun,
) -> CanonicalApprovalIdentity:
    action_spec = get_action_spec_or_none(action_type)
    if (
        action_spec is None
        or tenant_id != run.tenant_id
        or not resource_id
        or resource_version < 1
        or not run.turn_id
    ):
        raise RuntimeConflict("approval_canonical_identity_invalid")
    return CanonicalApprovalIdentity(
        tenant_id=tenant_id,
        customer_id=customer_id,
        action_type=action_type,
        resource_type=action_spec.resource_field,
        resource_id=resource_id,
        resource_version=resource_version,
        origin_turn_id=run.turn_id,
    )


class ActionLifecycleService:
    """Application orchestration for Approval state and identity.

    PostgreSQL transitions remain owned by the restricted SECURITY DEFINER
    capabilities and the database transition guard. This service provides the
    equivalent deterministic CAS semantics for SQLite/unit-test execution and a
    single read path for canonical active-Approval reuse.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def converge_ticket(
        self,
        ticket: SupportTicket,
        *,
        default_status: str,
    ) -> str:
        """Derive the SQLite/test Ticket projection from all active child work.

        PostgreSQL callers must use the owner-only database kernel so this read
        and the aggregate transition commit atomically under the Ticket lock.
        """

        if self.session.get_bind().dialect.name == "postgresql":
            raise RuntimeConflict("ticket_convergence_capability_required")
        if ticket.lifecycle != "active" or ticket.automation_mode != "agent":
            return ticket.status

        active_turn = await self.session.scalar(
            select(ConversationTurn.id)
            .where(
                ConversationTurn.tenant_id == ticket.tenant_id,
                ConversationTurn.ticket_id == ticket.id,
                ConversationTurn.activity_state.in_(("queued", "running")),
            )
            .limit(1)
        )
        active_job = await self.session.scalar(
            select(RuntimeJob.id)
            .where(
                RuntimeJob.tenant_id == ticket.tenant_id,
                RuntimeJob.ticket_id == ticket.id,
                RuntimeJob.status.in_(("queued", "retry_wait", "leased")),
            )
            .limit(1)
        )
        verification_pending = await self.session.scalar(
            select(ApprovalRequest.id)
            .join(
                RuntimeJob,
                (
                    (RuntimeJob.tenant_id == ApprovalRequest.tenant_id)
                    & (RuntimeJob.ticket_id == ApprovalRequest.ticket_id)
                    & (RuntimeJob.approval_id == ApprovalRequest.id)
                ),
            )
            .where(
                ApprovalRequest.tenant_id == ticket.tenant_id,
                ApprovalRequest.ticket_id == ticket.id,
                ApprovalRequest.status == "approved",
                RuntimeJob.status == "succeeded",
                RuntimeJob.outcome == "verification_pending",
            )
            .limit(1)
        )
        active_approval = await self.session.scalar(
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.tenant_id == ticket.tenant_id,
                ApprovalRequest.ticket_id == ticket.id,
                ApprovalRequest.status.in_(ACTIVE_APPROVAL_STATUSES),
            )
            .limit(1)
        )
        if active_turn is not None or active_job is not None:
            status = "queued"
        elif verification_pending is not None:
            status = "verification_pending"
        elif active_approval is not None:
            status = "awaiting_approval"
        else:
            status = default_status
        clear_response = status in {
            "running",
            "queued",
            "awaiting_approval",
            "verification_pending",
        }
        if ticket.status != status or (clear_response and ticket.final_response is not None):
            ticket.status = status
            if clear_response:
                ticket.final_response = None
            ticket.version += 1
        await self.session.flush()
        return status

    async def find_active(
        self,
        identity: CanonicalApprovalIdentity,
        *,
        lock: bool = False,
    ) -> ApprovalRequest | None:
        statement = select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == identity.tenant_id,
            ApprovalRequest.customer_id == identity.customer_id,
            ApprovalRequest.action_type == identity.action_type,
            ApprovalRequest.resource_type == identity.resource_type,
            ApprovalRequest.resource_id == identity.resource_id,
            ApprovalRequest.status.in_(ACTIVE_APPROVAL_STATUSES),
        )
        if lock:
            statement = statement.with_for_update()
        return cast(ApprovalRequest | None, await self.session.scalar(statement))

    async def transition(
        self,
        approval: ApprovalRequest,
        *,
        to_status: str,
        expected_status: str,
        expected_version: int | None = None,
        decided_at: datetime | None = None,
    ) -> ApprovalTransition:
        if self.session.get_bind().dialect.name == "postgresql":
            await self.session.refresh(approval)
            if approval.status == to_status:
                return ApprovalTransition(to_status, to_status, changed=False)
            raise RuntimeConflict("approval_transition_capability_required")

        previous = approval.status
        if previous == to_status:
            return ApprovalTransition(previous, previous, changed=False)
        if to_status == "manual_takeover" or to_status not in NEW_APPROVAL_STATUSES:
            raise RuntimeConflict("approval_transition_invalid")
        if previous != expected_status or (
            expected_version is not None and approval.status_version != expected_version
        ):
            raise RuntimeConflict("approval_state_conflict")
        if previous in TERMINAL_APPROVAL_STATUSES:
            raise RuntimeConflict("approval_terminal_conflict")
        if to_status not in APPROVAL_TRANSITIONS.get(previous, frozenset()):
            raise RuntimeConflict("approval_transition_invalid")
        approval.status = to_status
        if decided_at is not None:
            approval.decided_at = decided_at.astimezone(UTC)
        try:
            await self.session.flush()
        except StaleDataError as exc:
            raise RuntimeConflict("approval_state_conflict") from exc
        return ApprovalTransition(previous, to_status, changed=True)
