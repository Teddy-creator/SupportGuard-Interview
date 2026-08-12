from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.context import RequestContext
from supportguard.db.models import (
    AgentRun,
    ApiKeyMetadata,
    ApprovalRequest,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    HumanDecision,
    ProposalRecord,
    ReconcileIntent,
    RuntimeJob,
    Subscription,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.action_effect_reconciliation import (
    ActionEffectReconciliationRunner,
)
from supportguard.services.approval_commands import ApprovalCommandCoordinator
from supportguard.services.runtime_jobs import RuntimeJobRepository
from supportguard.services.segments import SegmentRepository
from test_postgres_finalizer_faults import _seed_pending_approval

pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class _UnknownEffectFixture:
    action_type: str
    approval_id: str
    proposal_id: str
    ticket_id: str
    run_id: str
    job_id: str
    marker_id: str
    resource_id: str
    resource_version: int
    job_status_version: int
    action_count: int


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _role_url(database_url: str, *, username: str, password: str) -> str:
    return (
        make_url(database_url)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


def _approver_scope(prefix: str) -> RequestContext:
    return RequestContext(
        tenant_id="tenant_demo",
        authenticated_actor_id="user_approver_demo",
        authenticated_actor_role="support_approver",
        subject_customer_id=None,
        request_id=f"request-{prefix}",
        trace_id=f"trace-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


async def _prepare_unknown_action_effect(
    database_url: str,
    *,
    prefix: str,
    action_type: str,
    evidence: str,
) -> _UnknownEffectFixture:
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_api",
            password="supportguard_api",  # noqa: S106
        )
    )
    api_factory = create_scoped_session_factory(api)
    worker = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_worker",
            password="supportguard_worker",  # noqa: S106
        )
    )
    worker_factory = async_sessionmaker(worker, expire_on_commit=False)
    try:
        approval_id, ticket_id = await _seed_pending_approval(
            admin_factory,
            prefix,
            action_type=action_type,
        )
        async with api_factory.request(_approver_scope(prefix)) as session:
            accepted = await ApprovalCommandCoordinator(session).decide(
                tenant_id="tenant_demo",
                approval_id=approval_id,
                decision="approve",
                actor_id="user_approver_demo",
                idempotency_key=f"decision-{prefix}",
                reason="The evidence and policy binding were reviewed.",
                approver_note="Unknown-effect reconciliation PostgreSQL fixture.",
                trace_id=f"trace-{prefix}",
            )
            await session.commit()

        async with admin_factory() as session, session.begin():
            approval = await session.get(ApprovalRequest, approval_id)
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            assert approval is not None
            assert decision is not None
            assert approval.status == "approved"
            assert approval.run_id is not None
            lease = await RuntimeJobRepository(session).claim(
                job_id=accepted.job_id,
                owner=f"worker-{prefix}",
            )
            marker = await SegmentRepository(session).prepare(
                lease,
                delivery_generation=1,
                segment_kind="approval_resume",
                segment_input={"approval_id": approval_id},
            )
            await SegmentRepository(session).checkpoint_written(
                lease,
                marker_id=marker.id,
                checkpoint_id=f"checkpoint-{prefix}",
                checkpoint_hash="e" * 64,
                outcome="completed",
                state={
                    "ticket_id": approval.ticket_id,
                    "customer_id": approval.customer_id,
                    "run_id": approval.run_id,
                    "trace_id": f"trace-{prefix}",
                    "classification": {
                        "issue_type": action_type,
                        "risk": "high",
                    },
                    "agent_finish_reason": "proposed",
                    "human_decision": {
                        "approval_id": approval_id,
                        "action": "approve",
                    },
                    "execution_result": {
                        "status": "approved",
                        "execution_state": "verification_pending",
                        "effect_status": "unknown",
                    },
                    "tool_observations": [],
                    "evidence": [],
                    "segment_events": [],
                    # This sentinel models the unsafe pre-v1.5.12 Graph output.
                    # The Segment Finalizer must suppress it because the effect
                    # is not authoritative yet.
                    "final": {
                        "answer": "FALSE_SUCCESS_SENTINEL_ACTION_EXECUTED",
                        "terminal_state": "resolved",
                        "knowledge_chunk_ids": [],
                        "business_source_ids": [],
                        "material_claims": [],
                        "policy_route": "await_approval",
                    },
                },
                approval_id=approval_id,
            )
        async with worker_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id','tenant_demo',true)")
            )
            finalized = await SegmentRepository(session).finalize(
                lease,
                marker_id=marker.id,
            )
            assert finalized.status == "finalized"

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            job = await session.get(RuntimeJob, accepted.job_id)
            run = await session.get(AgentRun, lease.run_id)
            ticket = await session.get(SupportTicket, ticket_id)
            assert approval is not None
            active_identity_count = int(
                await session.scalar(
                    select(func.count(ApprovalRequest.id)).where(
                        ApprovalRequest.tenant_id == "tenant_demo",
                        ApprovalRequest.customer_id == approval.customer_id,
                        ApprovalRequest.action_type == approval.action_type,
                        ApprovalRequest.resource_id == approval.resource_id,
                        ApprovalRequest.status.in_({"pending", "approved"}),
                    )
                )
                or 0
            )
            false_success_messages = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.ticket_id == ticket_id,
                        TicketMessage.content.contains(
                            "FALSE_SUCCESS_SENTINEL_ACTION_EXECUTED"
                        ),
                    )
                )
                or 0
            )
            assert approval.status == "approved"
            assert job is not None
            assert job.status == "succeeded"
            assert job.outcome == "verification_pending"
            assert run is not None
            assert run.status == "interrupted"
            assert run.agent_finish_reason == "verification_pending"
            assert run.active_job_id is None
            assert run.active_fencing_token is None
            assert ticket is not None
            assert ticket.status == "verification_pending"
            assert ticket.final_response != "FALSE_SUCCESS_SENTINEL_ACTION_EXECUTED"
            assert active_identity_count == 1
            assert false_success_messages == 0

        async with worker.begin() as connection:
            handoff = await connection.scalar(
                text(
                    "SELECT supportguard_worker_finish_job("
                    ":job_id,:owner,:fencing_token,'completed')"
                ),
                {
                    "job_id": accepted.job_id,
                    "owner": lease.owner,
                    "fencing_token": lease.fencing_token,
                },
            )
        assert isinstance(handoff, dict)
        assert handoff["outcome"] == "verification_pending"

        async with admin_factory() as session, session.begin():
            # The authoritative binding guard is SECURITY DEFINER and the
            # approval/event ledgers FORCE RLS.  Exercise it under the same
            # tenant context as the production worker instead of relying on
            # the test administrator's visibility.
            await session.execute(
                text("SELECT set_config('app.tenant_id','tenant_demo',true)")
            )
            approval = await session.get(ApprovalRequest, approval_id)
            decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            run = await session.get(
                AgentRun,
                lease.run_id,
                with_for_update=True,
            )
            assert approval is not None
            assert decision is not None
            assert run is not None
            if evidence in {"executed", "business_action_only"}:
                canonical_effect_event = await AgentRunStore(session).append_event(
                    run,
                    event_type="action_effect_authority_observed",
                    payload={
                        "approval_id": approval.id,
                        "action_type": approval.action_type,
                        "resource_id": approval.resource_id,
                        "effect_status": "succeeded",
                    },
                    visibility="internal",
                    idempotency_id=f"unknown-effect:{prefix}",
                )
                session.add(
                    BusinessAction(
                        id=f"action_{prefix}",
                        tenant_id=approval.tenant_id,
                        ticket_id=approval.ticket_id,
                        customer_id=approval.customer_id,
                        action_type=approval.action_type,
                        resource_id=approval.resource_id,
                        resource_version=approval.business_version,
                        action_hash=approval.action_hash,
                        approval_id=approval.id,
                        human_decision_id=decision.id,
                        action_revision_id=approval.selected_revision_id,
                        decision_hash=decision.decision_hash,
                        effect_identity=hashlib.sha256(
                            f"unknown-effect:{prefix}".encode()
                        ).hexdigest(),
                        canonical_event_id=canonical_effect_event.id,
                        canonical_event_hash=canonical_effect_event.event_hash,
                        status="succeeded",
                        idempotency_key=f"unknown-effect:{prefix}",
                        result={
                            "approval_id": approval.id,
                            "action_type": approval.action_type,
                            "status": "succeeded",
                        },
                    )
                )
                await session.flush()
                authority_binding_count = int(
                    await session.scalar(
                        text(
                            """
                            SELECT count(*)
                            FROM business_actions action
                            JOIN human_decisions decision
                              ON decision.tenant_id=action.tenant_id
                             AND decision.id=action.human_decision_id
                            JOIN approval_requests approval
                              ON approval.tenant_id=decision.tenant_id
                             AND approval.id=decision.approval_id
                            JOIN approval_action_revisions revision
                              ON revision.tenant_id=approval.tenant_id
                             AND revision.id=decision.action_revision_id
                             AND revision.approval_id=approval.id
                            JOIN agent_events event
                              ON event.tenant_id=approval.tenant_id
                             AND event.id=action.canonical_event_id
                             AND event.run_id=approval.run_id
                            WHERE action.id=:action_id
                              AND approval.id=action.approval_id
                              AND decision.id=action.human_decision_id
                              AND revision.id=action.action_revision_id
                              AND decision.decision_hash=action.decision_hash
                              AND decision.action_hash=action.action_hash
                              AND revision.action_hash=action.action_hash
                              AND event.event_hash=action.canonical_event_hash
                            """
                        ),
                        {"action_id": f"action_{prefix}"},
                    )
                    or 0
                )
                assert authority_binding_count == 1
            if evidence == "executed":
                if action_type == "refund":
                    billing = await session.get(
                        BillingRecord,
                        approval.resource_id,
                        with_for_update=True,
                    )
                    assert billing is not None
                    billing.status = "refunded"
                    billing.version += 1
                elif action_type == "api_key_revocation":
                    api_key = await session.scalar(
                        select(ApiKeyMetadata)
                        .where(
                            ApiKeyMetadata.tenant_id == approval.tenant_id,
                            ApiKeyMetadata.customer_id == approval.customer_id,
                            ApiKeyMetadata.key_id == approval.resource_id,
                        )
                        .with_for_update()
                    )
                    assert api_key is not None
                    api_key.status = "revoked"
                    api_key.version += 1
                else:
                    subscription = await session.get(
                        Subscription,
                        approval.resource_id,
                        with_for_update=True,
                    )
                    assert subscription is not None
                    target = approval.action_payload.get("target", {})
                    assert isinstance(target, dict)
                    assert target.get("concurrency_limit") == 60
                    subscription.concurrency_limit = 60
                    subscription.version += 1

        async with admin_factory() as session:
            job = await session.get(RuntimeJob, accepted.job_id)
            approval = await session.get(ApprovalRequest, approval_id)
            assert job is not None
            assert approval is not None
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            return _UnknownEffectFixture(
                action_type=action_type,
                approval_id=approval.id,
                proposal_id=str(approval.proposal_id),
                ticket_id=ticket_id,
                run_id=approval.run_id,
                job_id=job.id,
                marker_id=marker.id,
                resource_id=approval.resource_id,
                resource_version=approval.business_version,
                job_status_version=job.status_version,
                action_count=action_count,
            )
    finally:
        await worker.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.parametrize(
    "action_type",
    ["refund", "api_key_revocation", "entitlement_change"],
)
@pytest.mark.parametrize("evidence", ["executed", "zero_effect", "business_action_only"])
@pytest.mark.asyncio
async def test_real_postgres_unknown_effect_converges_only_from_joint_authority(
    action_type: str,
    evidence: str,
) -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    expected = {
        "executed": {
            "resolution": "executed",
            "approval": "executed",
            "job_outcome": "verification_executed",
            "run": "completed",
            "ticket": "resolved",
        },
        "zero_effect": {
            "resolution": "confirmed_zero_effect",
            "approval": "failed",
            "job_outcome": "verified_zero_effect",
            "run": "completed",
            "ticket": "failed",
        },
        "business_action_only": {
            "resolution": "pending",
            "approval": "approved",
            "job_outcome": "verification_pending",
            "run": "interrupted",
            "ticket": "verification_pending",
        },
    }[evidence]
    expected_resolution = expected["resolution"]
    expected_approval = expected["approval"]
    expected_job_outcome = expected["job_outcome"]
    expected_run = expected["run"]
    expected_ticket = expected["ticket"]
    expected_resource_value = {
        "refund": "refunded" if evidence == "executed" else "charged",
        "api_key_revocation": "revoked" if evidence == "executed" else "active",
        "entitlement_change": 60 if evidence == "executed" else 40,
    }[action_type]
    expected_resource_version_delta = 1 if evidence == "executed" else 0
    action_code = {
        "refund": "refund",
        "api_key_revocation": "key",
        "entitlement_change": "ent",
    }[action_type]
    evidence_code = {
        "executed": "exec",
        "zero_effect": "zero",
        "business_action_only": "ba_only",
    }[evidence]
    # The shared PostgreSQL fixture prefixes several varchar(64) identifiers.
    # Keep the case identity descriptive while leaving room for those prefixes.
    prefix = f"v1512_fx_{action_code}_{evidence_code}_{uuid4().hex[:8]}"
    fixture = await _prepare_unknown_action_effect(
        database_url,
        prefix=prefix,
        action_type=action_type,
        evidence=evidence,
    )
    reconciler = create_async_engine(
        _role_url(
            database_url,
            username="supportguard_reconciler",
            password="supportguard_reconciler",  # noqa: S106
        )
    )
    reconciler_factory = async_sessionmaker(reconciler, expire_on_commit=False)
    admin = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin, expire_on_commit=False)
    candidate = {
        "job_id": fixture.job_id,
        "job_status": "succeeded",
        "status_version": fixture.job_status_version,
    }
    try:
        runner = ActionEffectReconciliationRunner(reconciler_factory)
        report = await runner.reconcile_candidates([candidate])
        assert report.handled_job_ids == (fixture.job_id,)
        if expected_resolution == "executed":
            assert report.resolved_executed == 1
        elif expected_resolution == "confirmed_zero_effect":
            assert report.resolved_zero_effect == 1
        else:
            assert report.pending == 1

        async with admin_factory() as session:
            approval = await session.get(ApprovalRequest, fixture.approval_id)
            proposal = await session.get(ProposalRecord, fixture.proposal_id)
            job = await session.get(RuntimeJob, fixture.job_id)
            run = await session.get(AgentRun, fixture.run_id)
            ticket = await session.get(SupportTicket, fixture.ticket_id)
            marker = await session.get(CheckpointCommitMarker, fixture.marker_id)
            if action_type == "refund":
                resource = await session.get(BillingRecord, fixture.resource_id)
                resource_value = resource.status if resource is not None else None
            elif action_type == "api_key_revocation":
                resource = await session.scalar(
                    select(ApiKeyMetadata).where(
                        ApiKeyMetadata.tenant_id == approval.tenant_id,
                        ApiKeyMetadata.customer_id == approval.customer_id,
                        ApiKeyMetadata.key_id == fixture.resource_id,
                    )
                )
                resource_value = resource.status if resource is not None else None
            else:
                resource = await session.get(Subscription, fixture.resource_id)
                resource_value = (
                    resource.concurrency_limit if resource is not None else None
                )
            action_count = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == fixture.approval_id
                    )
                )
                or 0
            )
            redis_reconcile_intents = int(
                await session.scalar(
                    select(func.count(ReconcileIntent.id)).where(
                        ReconcileIntent.job_id == fixture.job_id
                    )
                )
                or 0
            )
            assert approval is not None
            active_identity_count = int(
                await session.scalar(
                    select(func.count(ApprovalRequest.id)).where(
                        ApprovalRequest.tenant_id == approval.tenant_id,
                        ApprovalRequest.customer_id == approval.customer_id,
                        ApprovalRequest.action_type == approval.action_type,
                        ApprovalRequest.resource_id == approval.resource_id,
                        ApprovalRequest.status.in_({"pending", "approved"}),
                    )
                )
                or 0
            )
            false_success_messages = int(
                await session.scalar(
                    select(func.count(TicketMessage.id)).where(
                        TicketMessage.ticket_id == fixture.ticket_id,
                        TicketMessage.content.contains(
                            "FALSE_SUCCESS_SENTINEL_ACTION_EXECUTED"
                        ),
                    )
                )
                or 0
            )
            assert approval.status == expected_approval
            assert job is not None and job.outcome == expected_job_outcome
            assert run is not None and run.status == expected_run
            assert ticket is not None and ticket.status == expected_ticket
            assert marker is not None and marker.status == "finalized"
            assert resource is not None
            assert resource_value == expected_resource_value
            assert resource.version == (
                fixture.resource_version + expected_resource_version_delta
            )
            assert action_count == fixture.action_count
            assert redis_reconcile_intents == 0
            assert false_success_messages == 0
            if expected_resolution == "pending":
                assert active_identity_count == 1
                assert proposal is not None and proposal.status == "bound"
            else:
                assert active_identity_count == 0
                assert proposal is not None and proposal.status == "stale"

        replay = await runner.reconcile_candidates([candidate])
        if expected_resolution == "pending":
            # The first pending result durably reschedules verification and
            # advances status_version. Replaying that old candidate snapshot is
            # therefore a stale CAS, not a second pending observation.
            assert replay.stale == 1
            async with admin_factory() as session, session.begin():
                job = await session.get(
                    RuntimeJob,
                    fixture.job_id,
                    with_for_update=True,
                )
                database_now = await session.scalar(select(func.clock_timestamp()))
                assert job is not None
                assert database_now is not None
                assert job.status_version == fixture.job_status_version + 1
                assert job.available_at > database_now
                first_retry_version = job.status_version
                first_retry_due_at = job.available_at
                forced_due_at = await session.scalar(
                    text(
                        """
                        UPDATE runtime_jobs
                        SET available_at=clock_timestamp()-interval '1 second'
                        WHERE id=:job_id AND status_version=:status_version
                        RETURNING available_at
                        """
                    ),
                    {
                        "job_id": fixture.job_id,
                        "status_version": first_retry_version,
                    },
                )
                assert forced_due_at is not None

            due_replay = await runner.reconcile_candidates(
                [
                    {
                        "job_id": fixture.job_id,
                        "job_status": "succeeded",
                        "status_version": first_retry_version,
                    }
                ]
            )
            assert due_replay.pending == 1
            async with admin_factory() as session:
                job = await session.get(RuntimeJob, fixture.job_id)
                database_now = await session.scalar(select(func.clock_timestamp()))
                assert job is not None
                assert database_now is not None
                assert job.status_version == first_retry_version + 1
                assert job.available_at > database_now
                assert job.available_at > first_retry_due_at
        else:
            assert replay.stale == 1
        async with admin_factory() as session:
            assert (
                int(
                    await session.scalar(
                        select(func.count(BusinessAction.id)).where(
                            BusinessAction.approval_id == fixture.approval_id
                        )
                    )
                    or 0
                )
                == fixture.action_count
            )
            assert (
                int(
                    await session.scalar(
                        select(func.count(ReconcileIntent.id)).where(
                            ReconcileIntent.job_id == fixture.job_id
                        )
                    )
                    or 0
                )
                == 0
            )
    finally:
        await admin.dispose()
        await reconciler.dispose()
