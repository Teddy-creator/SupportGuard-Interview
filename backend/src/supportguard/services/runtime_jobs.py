from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.errors import RuntimeConflict as RuntimeConflict
from supportguard.contracts.public_failures import (
    classify_public_failure,
    public_failure_reply,
)
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    CheckpointCommitMarker,
    ConversationTurn,
    IdempotencyRequest,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.db.scope import set_local_scope
from supportguard.services.conversation_activity import advance_conversation_activity
from supportguard.services.turn_activation import activate_next_turn


class FinalizerCommitUnknown(RuntimeError):
    """The Finalizer body completed but its outer COMMIT result is unknowable.

    This exception is deliberately narrower than a generic Worker failure.  It
    may only be raised around the database transaction's ``commit()`` call, so
    the queue consumer can reconcile the durable Action effect instead of
    retrying the Segment or terminally failing an active Approval.
    """

    def __init__(self, *, job_id: str, recovery_mode: str) -> None:
        if recovery_mode not in {"action_effect", "finalizer_only"}:
            raise ValueError("finalizer commit recovery mode invalid")
        super().__init__("finalizer_commit_result_unknown")
        self.job_id = job_id
        self.recovery_mode = recovery_mode


class FinalizerRestartRequired(RuntimeError):
    """A pre-effect answer must be recomputed from a newer aggregate head.

    The stale Finalizer has already been durably abandoned before this signal
    escapes.  The queue may therefore advance to a new fence and rerun the
    bounded Agent without replaying a business effect or the stale checkpoint.
    """

    def __init__(self) -> None:
        super().__init__("pre_effect_finalizer_restart_required")


async def converge_dead_aggregates(
    session: AsyncSession, *, job: RuntimeJob, run: AgentRun, reason: str
) -> None:
    """Fail explicitly, publish a safe result, and release the Ticket lane."""
    failure_category = classify_public_failure(reason) or "runtime"
    run.status = "failed"
    run.error_code = reason[:128]
    run.active_job_id = None
    run.active_fencing_token = None
    run.completed_at = datetime.now(UTC)
    if run.turn_id:
        turn = await session.get(ConversationTurn, run.turn_id, with_for_update=True)
        if turn is not None:
            turn.activity_state = "failed"
            turn.result_state = "failed"
            turn.completed_at = run.completed_at
    ticket = await session.get(SupportTicket, run.ticket_id, with_for_update=True)
    if ticket is not None and ticket.automation_mode != "human_queue":
        ticket.status = "failed"
        ticket.version += 1
        publication_key = f"runtime-failure:{job.id}"
        existing_reply = await session.scalar(
            select(TicketMessage.id).where(
                TicketMessage.tenant_id == job.tenant_id,
                TicketMessage.publication_key == publication_key,
            )
        )
        if existing_reply is None:
            ticket.next_message_sequence += 1
            session.add(
                TicketMessage(
                    tenant_id=job.tenant_id,
                    ticket_id=ticket.id,
                    turn_id=run.turn_id,
                    run_id=run.id,
                    approval_id=job.approval_id,
                    conversation_sequence=ticket.next_message_sequence,
                    message_kind="assistant",
                    publication_key=publication_key,
                    role="assistant",
                    content=public_failure_reply(failure_category),
                    source_refs=[
                        {
                            "kind": "runtime_failure",
                            "reason_code": "automatic_processing_failed",
                            "failure_category": failure_category,
                        }
                    ],
                )
            )
            advance_conversation_activity(ticket)
    if job.approval_id and session.get_bind().dialect.name != "postgresql":
        approval = await session.get(ApprovalRequest, job.approval_id, with_for_update=True)
        if approval is not None and approval.status not in {
            "executed",
            "rejected",
            "manual_takeover",
            "failed",
            "stale",
            "withdrawn",
        }:
            from supportguard.services.approval_lifecycle import ActionLifecycleService

            await ActionLifecycleService(session).transition(
                approval,
                to_status="failed",
                expected_status=approval.status,
                expected_version=approval.status_version,
            )
            proposal = await session.get(
                ProposalRecord,
                approval.proposal_id,
                with_for_update=True,
            )
            if proposal is not None and proposal.status in {"draft", "bound"}:
                proposal.status = "stale"
                proposal.status_version += 1
    markers = (
        await session.scalars(
            select(CheckpointCommitMarker)
            .where(
                CheckpointCommitMarker.job_id == job.id,
                CheckpointCommitMarker.status.in_(["prepared", "checkpoint_written"]),
            )
            .with_for_update()
        )
    ).all()
    for marker in markers:
        marker.status = "aborted"
        marker.status_version += 1
    if ticket is not None and ticket.automation_mode == "agent" and ticket.lifecycle == "active":
        await activate_next_turn(
            session,
            ticket=ticket,
            trace_id=f"runtime-failure:{job.id}",
        )
        from supportguard.services.approval_lifecycle import ActionLifecycleService

        await ActionLifecycleService(session).converge_ticket(
            ticket,
            default_status="failed",
        )


def canonical_request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class AcceptedCommand:
    record: IdempotencyRequest
    reused: bool


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def accept(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        route: str,
        key: str,
        payload: dict[str, Any],
        resource_ids: dict[str, str],
        response_snapshot: dict[str, Any],
        expires_at: datetime | None,
    ) -> AcceptedCommand:
        request_hash = canonical_request_hash(payload)
        low_risk_route = route == "POST /api/tickets" or bool(
            re.fullmatch(r"POST /api/tickets/[^/]+/messages", route)
        )
        retention_class = "low_risk_non_action" if low_risk_route else "protected_action"
        completed_at = datetime.now(UTC) if response_snapshot else None
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            statement = (
                postgresql_insert(IdempotencyRequest)
                .values(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    route=route,
                    idempotency_key=key,
                    request_hash=request_hash,
                    resource_ids=resource_ids,
                    response_snapshot=response_snapshot,
                    expires_at=expires_at,
                    completed_at=completed_at,
                    retention_class=retention_class,
                )
                .on_conflict_do_nothing(
                    constraint="uq_idempotency_request_scope",
                )
                .returning(IdempotencyRequest.id)
            )
            inserted_id = await self.session.scalar(statement)
            existing = await self.session.scalar(
                select(IdempotencyRequest)
                .where(
                    IdempotencyRequest.tenant_id == tenant_id,
                    IdempotencyRequest.principal_id == principal_id,
                    IdempotencyRequest.route == route,
                    IdempotencyRequest.idempotency_key == key,
                )
                .with_for_update()
            )
            if existing is None:
                raise RuntimeError("idempotency_claim_disappeared")
            if existing.request_hash != request_hash:
                raise RuntimeConflict("idempotency_conflict")
            return AcceptedCommand(existing, reused=inserted_id is None)

        existing = await self.session.scalar(
            select(IdempotencyRequest)
            .where(
                IdempotencyRequest.tenant_id == tenant_id,
                IdempotencyRequest.principal_id == principal_id,
                IdempotencyRequest.route == route,
                IdempotencyRequest.idempotency_key == key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise RuntimeConflict("idempotency_conflict")
            return AcceptedCommand(existing, reused=True)
        record = IdempotencyRequest(
            tenant_id=tenant_id,
            principal_id=principal_id,
            route=route,
            idempotency_key=key,
            request_hash=request_hash,
            resource_ids=resource_ids,
            response_snapshot=response_snapshot,
            expires_at=expires_at,
            completed_at=completed_at,
            retention_class=retention_class,
        )
        self.session.add(record)
        await self.session.flush()
        return AcceptedCommand(record, reused=False)


@dataclass(frozen=True)
class JobLease:
    job_id: str
    run_id: str
    tenant_id: str
    owner: str
    fencing_token: int
    expires_at: datetime
    kind: str = "agent_start"
    approval_id: str | None = None
    attempt: int = 1
    ticket_id: str | None = None
    dispatch_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class RetryDecision:
    terminal: bool
    next_status: str
    available_at: datetime
    reason: str
    attempt: int


def transition_runtime_job_status(job: RuntimeJob, status: str) -> None:
    """Advance the durable CAS version exactly once for a real Job transition."""

    if job.status == status:
        return
    job.status = status
    job.status_version += 1


class RuntimeJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        tenant_id: str,
        run_id: str,
        kind: str,
        approval_id: str | None = None,
        job_id: str | None = None,
        ticket_id: str | None = None,
    ) -> RuntimeJob:
        if (
            (kind == "agent_start" and approval_id is not None)
            or (kind == "approval_resume" and not approval_id)
            or kind not in {"agent_start", "approval_resume"}
        ):
            raise RuntimeConflict("runtime_job_kind_approval_shape_invalid")
        run_ticket_id = await self.session.scalar(
            select(AgentRun.ticket_id).where(
                AgentRun.id == run_id,
                AgentRun.tenant_id == tenant_id,
            )
        )
        if run_ticket_id is None or (ticket_id is not None and ticket_id != run_ticket_id):
            raise RuntimeConflict("job_ticket_mismatch")
        if kind == "approval_resume":
            matching_approval_id = await self.session.scalar(
                select(ApprovalRequest.id).where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.run_id == run_id,
                    ApprovalRequest.ticket_id == run_ticket_id,
                )
            )
            if matching_approval_id is None:
                raise RuntimeConflict("runtime_job_approval_mismatch")
        if self.session.get_bind().dialect.name == "postgresql":
            # PostgreSQL owns Ticket-lane identity in the BEFORE INSERT trigger.
            # Supplying or pre-allocating it here would advance the Ticket counter
            # twice and create an artificial gap for every RuntimeJob.
            job = RuntimeJob(
                id=job_id or new_id("job"),
                tenant_id=tenant_id,
                run_id=run_id,
                kind=kind,
                approval_id=approval_id,
            )
            self.session.add(job)
            await self.session.flush()
            return job
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == run_ticket_id,
                SupportTicket.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if ticket is None:
            raise RuntimeConflict("ticket_not_found")
        ticket.next_dispatch_sequence += 1
        dispatch_sequence = ticket.next_dispatch_sequence
        job = RuntimeJob(
            id=job_id or new_id("job"),
            tenant_id=tenant_id,
            ticket_id=run_ticket_id,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            kind=kind,
            approval_id=approval_id,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def claim(
        self, *, job_id: str, owner: str, now: datetime | None = None, lease_seconds: int = 30
    ) -> JobLease:
        del now  # PostgreSQL/SQLite database time is the lease authority.
        database_now = await self.session.scalar(select(func.now()))
        if database_now is None:
            raise RuntimeConflict("database_time_unavailable")
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        identity = (
            await self.session.execute(
                select(
                    RuntimeJob.tenant_id,
                    RuntimeJob.ticket_id,
                    RuntimeJob.run_id,
                ).where(RuntimeJob.id == job_id)
            )
        ).one_or_none()
        if identity is None:
            raise RuntimeConflict("job_not_claimable")
        tenant_id, ticket_id, _ = identity
        await set_local_scope(
            self.session,
            tenant_id=tenant_id,
            principal_id=owner,
            principal_role="system_worker",
        )
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == ticket_id,
                SupportTicket.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if (
            ticket is None
            or ticket.lifecycle != "active"
            or ticket.automation_mode == "human_queue"
        ):
            raise RuntimeConflict("ticket_not_claimable")
        job = await self.session.scalar(
            select(RuntimeJob)
            .where(
                RuntimeJob.id == job_id,
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.ticket_id == ticket_id,
            )
            .with_for_update()
        )
        if job is None or job.status not in {"queued", "retry_wait"}:
            raise RuntimeConflict("job_not_claimable")
        head_sequence = await self.session.scalar(
            select(func.min(RuntimeJob.dispatch_sequence)).where(
                RuntimeJob.tenant_id == job.tenant_id,
                RuntimeJob.ticket_id == job.ticket_id,
                RuntimeJob.status.in_(("queued", "retry_wait", "leased")),
            )
        )
        if head_sequence is None or int(head_sequence) != job.dispatch_sequence:
            raise RuntimeConflict("ticket_fifo_blocked")
        leased_job_id = await self.session.scalar(
            select(RuntimeJob.id)
            .where(
                RuntimeJob.tenant_id == job.tenant_id,
                RuntimeJob.ticket_id == job.ticket_id,
                RuntimeJob.status == "leased",
            )
            .limit(1)
        )
        if leased_job_id is not None:
            raise RuntimeConflict("ticket_lane_leased")
        available_at = job.available_at
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=UTC)
        if available_at > database_now:
            raise RuntimeConflict("job_not_due")
        run = await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == job.run_id,
                AgentRun.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        if run is None or run.status not in {"queued", "interrupted", "running"}:
            raise RuntimeConflict("run_not_claimable")
        if job.kind == "approval_resume":
            # The canonical lane lock order is Ticket -> RuntimeJob -> AgentRun.
            # Approval validation is intentionally a non-locking read after the
            # Run lock. Every supported Approval writer first locks this same
            # Ticket, so its status cannot change while this transaction owns the
            # Ticket; taking Approval FOR UPDATE here would add an unnecessary
            # Run -> Approval edge outside the frozen Worker Claim order.
            approval = await self.session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == job.approval_id,
                    ApprovalRequest.tenant_id == job.tenant_id,
                    ApprovalRequest.run_id == job.run_id,
                    ApprovalRequest.ticket_id == job.ticket_id,
                )
            )
            if approval is None or approval.status != "approved":
                raise RuntimeConflict("approval_resume_not_claimable")
        transition_runtime_job_status(job, "leased")
        job.attempt += 1
        job.lease_owner = owner
        job.heartbeat_at = database_now
        job.lease_expires_at = database_now + timedelta(seconds=lease_seconds)
        job.fencing_token += 1
        run.active_job_id = job.id
        run.active_fencing_token = job.fencing_token
        run.status = "running"
        run.status_version += 1
        if run.turn_id:
            turn = await self.session.get(ConversationTurn, run.turn_id, with_for_update=True)
            if turn is not None and job.kind == "agent_start":
                turn.activity_state = "running"
        if run.ticket_id != job.ticket_id:
            raise RuntimeConflict("job_ticket_mismatch")
        if ticket.status == "queued":
            ticket.status = "running"
            ticket.version += 1
        await self.session.flush()
        return JobLease(
            job.id,
            job.run_id,
            job.tenant_id,
            owner,
            job.fencing_token,
            job.lease_expires_at,
            job.kind,
            job.approval_id,
            job.attempt,
            job.ticket_id,
            job.dispatch_sequence,
        )

    async def heartbeat(self, lease: JobLease, *, lease_seconds: int = 30) -> None:
        job = await self.assert_fence(lease)
        database_now = await self.session.scalar(select(func.now()))
        if database_now is None:
            raise RuntimeConflict("database_time_unavailable")
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        job.heartbeat_at = database_now
        job.lease_expires_at = database_now + timedelta(seconds=lease_seconds)
        await self.session.flush()

    async def fail(
        self,
        lease: JobLease,
        *,
        error_code: str,
        max_attempts: int = 5,
        max_age_seconds: int = 3600,
    ) -> str:
        job = await self.assert_fence(lease)
        database_now = await self.session.scalar(select(func.now()))
        if database_now is None:
            raise RuntimeConflict("database_time_unavailable")
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is None:
            raise RuntimeConflict("run_not_found")
        created_at = job.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        decision = self.retry_decision(
            job_id=job.id,
            attempt=job.attempt,
            created_at=created_at,
            now=database_now,
            max_attempts=max_attempts,
            max_age_seconds=max_age_seconds,
        )
        transition_runtime_job_status(job, decision.next_status)
        job.last_error = error_code[:128]
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.available_at = decision.available_at
        run.active_job_id = None
        run.active_fencing_token = None
        run.status = "failed" if decision.terminal else "queued"
        run.error_code = error_code[:128] if decision.terminal else None
        run.status_version += 1
        if decision.terminal:
            await converge_dead_aggregates(self.session, job=job, run=run, reason=error_code)
        await self.session.flush()
        return job.status

    async def terminal_fail(self, lease: JobLease, *, error_code: str) -> str:
        """Persist a typed non-retryable domain/policy failure atomically."""

        if self.session.get_bind().dialect.name == "postgresql":
            session_user = await self.session.scalar(text("SELECT session_user"))
            if session_user == "supportguard_worker":
                # The restricted Worker cannot and must not widen itself into
                # direct aggregate writes. Validate the lease, then hand the
                # typed, explicitly terminal outcome to
                # supportguard_worker_finish_job in the outer RuntimeWorker
                # transaction. Approval-resume failures require their own
                # effect-aware protocol and may never use this zero-action
                # terminal handoff.
                await self.assert_fence(lease)
                if lease.kind != "agent_start":
                    raise RuntimeConflict("terminal_fail_action_lane_forbidden")
                return f"terminal_failed:{error_code[:110]}"
        job = await self.assert_fence(lease)
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is None:
            raise RuntimeConflict("run_not_found")
        transition_runtime_job_status(job, "dead")
        job.last_error = error_code[:128]
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        await converge_dead_aggregates(self.session, job=job, run=run, reason=error_code)
        run.status_version += 1
        if self.session.get_bind().dialect.name == "postgresql":
            await self.session.flush()
            await self.finalize_control(lease, status="dead", outcome=error_code)
        await self.session.flush()
        return job.status

    @staticmethod
    def retry_decision(
        *,
        job_id: str,
        attempt: int,
        created_at: datetime,
        now: datetime,
        max_attempts: int = 5,
        max_age_seconds: int = 3600,
    ) -> RetryDecision:
        terminal = attempt >= max_attempts or now - created_at >= timedelta(seconds=max_age_seconds)
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode()).digest()
        jitter_ms = int.from_bytes(digest[:2], "big") % 1001
        backoff_seconds = min(30.0, float(2**attempt)) + jitter_ms / 1000
        return RetryDecision(
            terminal=terminal,
            next_status="dead" if terminal else "retry_wait",
            available_at=now + timedelta(seconds=backoff_seconds),
            reason="max_attempts_or_age" if terminal else "bounded_backoff",
            attempt=attempt,
        )

    async def assert_fence(self, lease: JobLease) -> RuntimeJob:
        await set_local_scope(
            self.session,
            tenant_id=lease.tenant_id,
            principal_id=lease.owner,
            principal_role="system_worker",
        )
        if self.session.get_bind().dialect.name == "postgresql":
            session_user = await self.session.scalar(text("SELECT session_user"))
            if session_user == "supportguard_worker":
                snapshot = await self.session.scalar(
                    text("SELECT supportguard_worker_claim_job(:job_id,:owner)"),
                    {"job_id": lease.job_id, "owner": lease.owner},
                )
                if not isinstance(snapshot, dict):
                    raise RuntimeConflict("stale_fencing_token")
                if (
                    str(snapshot.get("job_id", "")) != lease.job_id
                    or str(snapshot.get("run_id", "")) != lease.run_id
                    or str(snapshot.get("tenant_id", "")) != lease.tenant_id
                    or int(snapshot.get("fencing_token", -1)) != lease.fencing_token
                    or (
                        lease.ticket_id is not None
                        and str(snapshot.get("ticket_id", "")) != lease.ticket_id
                    )
                    or (
                        lease.dispatch_sequence is not None
                        and int(snapshot.get("dispatch_sequence", -1)) != lease.dispatch_sequence
                    )
                ):
                    raise RuntimeConflict("stale_fencing_token")
                current_expiry = datetime.fromisoformat(str(snapshot["expires_at"]))
                if current_expiry.tzinfo is None:
                    current_expiry = current_expiry.replace(tzinfo=UTC)
                return RuntimeJob(
                    id=lease.job_id,
                    tenant_id=lease.tenant_id,
                    ticket_id=str(snapshot["ticket_id"]),
                    run_id=lease.run_id,
                    dispatch_sequence=int(snapshot["dispatch_sequence"]),
                    kind=str(snapshot["kind"]),
                    approval_id=snapshot.get("approval_id"),
                    attempt=int(snapshot["attempt"]),
                    status="leased",
                    status_version=int(snapshot["status_version"]),
                    lease_owner=lease.owner,
                    fencing_token=lease.fencing_token,
                    lease_expires_at=current_expiry,
                    last_error=(
                        str(snapshot["last_error"])
                        if snapshot.get("last_error") is not None
                        else None
                    ),
                )
        database_now = await self.session.scalar(select(func.now()))
        if database_now is None:
            raise RuntimeConflict("database_time_unavailable")
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        ticket_id = lease.ticket_id
        if ticket_id is None:
            ticket_id = await self.session.scalar(
                select(AgentRun.ticket_id).where(
                    AgentRun.id == lease.run_id,
                    AgentRun.tenant_id == lease.tenant_id,
                )
            )
        if ticket_id is None:
            raise RuntimeConflict("stale_fencing_token")
        ticket = await self.session.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.id == ticket_id,
                SupportTicket.tenant_id == lease.tenant_id,
            )
            .with_for_update()
        )
        if ticket is None:
            raise RuntimeConflict("stale_fencing_token")
        job = await self.session.scalar(
            select(RuntimeJob)
            .where(
                RuntimeJob.id == lease.job_id,
                RuntimeJob.tenant_id == lease.tenant_id,
                RuntimeJob.run_id == lease.run_id,
                RuntimeJob.ticket_id == ticket_id,
            )
            .with_for_update()
        )
        if job is None:
            raise RuntimeConflict("stale_fencing_token")
        run = await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == lease.run_id,
                AgentRun.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        if (
            run is None
            or job.status != "leased"
            or job.lease_owner != lease.owner
            or job.fencing_token != lease.fencing_token
            or job.lease_expires_at is None
            or (
                job.lease_expires_at.replace(tzinfo=UTC)
                if job.lease_expires_at.tzinfo is None
                else job.lease_expires_at
            )
            <= database_now
            or run.active_job_id != lease.job_id
            or run.active_fencing_token != lease.fencing_token
            or (
                lease.dispatch_sequence is not None
                and job.dispatch_sequence != lease.dispatch_sequence
            )
        ):
            raise RuntimeConflict("stale_fencing_token")
        return job

    async def refresh_lease(self, lease: JobLease) -> JobLease:
        """Return the authoritative lease snapshot after any heartbeat renewal."""

        job = await self.assert_fence(lease)
        current_expiry = job.lease_expires_at
        if current_expiry is None:
            raise RuntimeConflict("stale_fencing_token")
        if current_expiry.tzinfo is None:
            current_expiry = current_expiry.replace(tzinfo=UTC)
        return JobLease(
            job.id,
            job.run_id,
            job.tenant_id,
            lease.owner,
            job.fencing_token,
            current_expiry,
            job.kind,
            job.approval_id,
            job.attempt,
            job.ticket_id,
            job.dispatch_sequence,
        )

    async def finalize_control(
        self,
        lease: JobLease,
        *,
        status: str,
        outcome: str,
    ) -> None:
        """Mutate RuntimeJob only through the frozen Worker capability on PostgreSQL."""

        is_worker_postgres = False
        if self.session.get_bind().dialect.name == "postgresql":
            is_worker_postgres = (
                await self.session.scalar(text("SELECT session_user")) == "supportguard_worker"
            )
        if not is_worker_postgres:
            job = await self.session.get(RuntimeJob, lease.job_id, with_for_update=True)
            if job is None:
                raise RuntimeConflict("stale_fencing_token")
            transition_runtime_job_status(job, status)
            job.outcome = outcome
            job.last_error = outcome[:128] if status == "dead" else None
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            return
        result = await self.session.scalar(
            text("SELECT supportguard_worker_finalize(CAST(:payload AS jsonb))"),
            {
                "payload": json.dumps(
                    {
                        "schema_version": "worker-finalize.v1",
                        "job_id": lease.job_id,
                        "run_id": lease.run_id,
                        "tenant_id": lease.tenant_id,
                        "owner": lease.owner,
                        "fencing_token": lease.fencing_token,
                        "status": status,
                        "outcome": outcome,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        if not isinstance(result, dict) or result.get("status") != status:
            raise RuntimeConflict("worker_finalize_failed")

    async def complete(self, lease: JobLease, *, outcome: str) -> None:
        job = await self.assert_fence(lease)
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is None:
            raise RuntimeConflict("run_not_found")
        transition_runtime_job_status(job, "succeeded")
        job.outcome = outcome
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        run.active_job_id = None
        run.active_fencing_token = None
        if outcome == "interrupted":
            run.status = "interrupted"
        elif outcome in {"completed", "rejected", "manual_takeover"}:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
        run.status_version += 1
        await self.session.flush()
