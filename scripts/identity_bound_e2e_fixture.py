from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApprovalRequest,
    ApproverTenantScope,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    CitationBinding,
    ContextMembership,
    Customer,
    HumanDecision,
    InboxDelivery,
    Membership,
    MutationKillSwitch,
    OutboxEvent,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    ProposalRecord,
    RetrievalTrace,
    RuntimeJob,
    ServiceInstanceHeartbeat,
    Subscription,
    SupportTicket,
    Tenant,
    ToolInvocation,
    ToolObservation,
    User,
)

FAILURE_SNAPSHOT_SCHEMA = "supportguard-failure-snapshot.v1"
FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {
        "action_payload",
        "approver_note",
        "content",
        "decision_reason",
        "final_response",
        "observation",
        "observation_binding",
        "payload",
        "policy_binding",
        "private_namespace",
        "prompt",
        "reason",
        "result",
        "review_context",
        "state_delta",
    }
)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _validate_failure_snapshot(value: Any, path: str = "snapshot") -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN_SNAPSHOT_KEYS.intersection(value)
        if forbidden:
            names = ",".join(sorted(forbidden))
            raise RuntimeError(f"failure_snapshot_forbidden_fields:{path}:{names}")
        for key, item in value.items():
            _validate_failure_snapshot(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_failure_snapshot(item, f"{path}[{index}]")


def _seal_failure_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    _validate_failure_snapshot(snapshot)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return {**snapshot, "snapshot_sha256": hashlib.sha256(encoded).hexdigest()}


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL is required")
    return value


def boundary_safe_usage_buckets(
    *,
    suffix: str,
    tenant_id: str,
    customer_id: str,
    window_end: datetime,
) -> list[ApiUsageBucket]:
    """Cover the two one-minute windows an in-flight test may straddle.

    The runtime intentionally evaluates usage against the immutable logical
    time recorded when its ToolInvocation is opened.  A fixture created just
    before a wall-clock minute boundary can therefore be queried just after
    that boundary.  Keep both adjacent immutable buckets in the fixture so
    either legitimate logical window is complete; the production query still
    filters out the later bucket until its window has actually closed.
    """

    return [
        ApiUsageBucket(
            id=f"usage_bucket_previous_{suffix}",
            tenant_id=tenant_id,
            customer_id=customer_id,
            bucket_start=window_end - timedelta(minutes=1),
            bucket_end=window_end,
            request_count=12,
            input_token_count=1440,
            output_token_count=480,
            concurrency_peak=20,
            concurrency_end=20,
            source_version=1,
        ),
        ApiUsageBucket(
            id=f"usage_bucket_rollover_{suffix}",
            tenant_id=tenant_id,
            customer_id=customer_id,
            bucket_start=window_end,
            bucket_end=window_end + timedelta(minutes=1),
            request_count=12,
            input_token_count=1440,
            output_token_count=480,
            concurrency_peak=20,
            concurrency_end=20,
            source_version=1,
        ),
    ]


async def seed(action_type: str) -> dict[str, Any]:
    suffix = uuid4().hex[:12]
    tenant_id = f"tenant_e2e_{suffix}"
    customer_id = f"cust_e2e_{suffix}"
    customer_user_id = f"user_customer_{suffix}"
    approver_user_id = f"user_approver_{suffix}"
    customer_subject = f"e2e-customer-{suffix}"
    approver_subject = f"e2e-approver-{suffix}"
    subscription_id = f"sub_e2e_{suffix}"
    api_key_id = f"key_e2e_{suffix}"
    original_billing_id = f"bill_original_{suffix}"
    duplicate_billing_id = f"bill_duplicate_{suffix}"
    now = datetime.now(UTC)
    window_end = now.replace(second=0, microsecond=0)

    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add_all(
                [
                    Tenant(id=tenant_id, name=f"E2E Tenant {suffix}", status="active"),
                    User(
                        id=customer_user_id,
                        external_subject=customer_subject,
                        display_name="E2E Customer",
                    ),
                    User(
                        id=approver_user_id,
                        external_subject=approver_subject,
                        display_name="E2E Approver",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Membership(
                        tenant_id=tenant_id,
                        user_id=customer_user_id,
                        role="customer_admin",
                        status="active",
                    ),
                    Membership(
                        tenant_id=tenant_id,
                        user_id=approver_user_id,
                        role="support_approver",
                        status="active",
                    ),
                    ApproverTenantScope(
                        user_id=approver_user_id,
                        tenant_id=tenant_id,
                    ),
                    Customer(
                        id=customer_id,
                        tenant_id=tenant_id,
                        display_name=f"E2E Customer {suffix}",
                        email=f"{suffix}@e2e.example.test",
                        status="active",
                        security_status="normal",
                        region="eu-west",
                        version=1,
                    ),
                    *[
                        MutationKillSwitch(
                            tenant_id=tenant_id,
                            action_type=item,
                            enabled=True,
                            changed_by="identity-bound-e2e-fixture",
                        )
                        for item in (
                            "refund",
                            "api_key_revocation",
                            "entitlement_change",
                        )
                    ],
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Subscription(
                        id=subscription_id,
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        plan="pro",
                        status="active",
                        balance=Decimal("120.00"),
                        currency="USD",
                        rpm_limit=60,
                        concurrency_limit=40,
                        version=3,
                    ),
                    ApiUsageSnapshot(
                        id=f"usage_snapshot_{suffix}",
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        observed_at=now,
                        requests_last_minute=12,
                        concurrency_current=20,
                        remaining_balance=Decimal("120.00"),
                    ),
                    *boundary_safe_usage_buckets(
                        suffix=suffix,
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        window_end=window_end,
                    ),
                    ApiKeyMetadata(
                        id=f"keymeta_{suffix}",
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        key_id=api_key_id,
                        fingerprint=f"fp_{suffix}",
                        status="active",
                        version=2,
                        last_used_summary={"region": "eu-west", "request_count": 3},
                    ),
                    BillingRecord(
                        id=original_billing_id,
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        amount=Decimal("49.00"),
                        currency="USD",
                        status="charged",
                        duplicate_of=None,
                        version=1,
                    ),
                ]
            )
            await session.flush()
            session.add(
                BillingRecord(
                    id=duplicate_billing_id,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    amount=Decimal("49.00"),
                    currency="USD",
                    status="charged",
                    duplicate_of=original_billing_id,
                    version=2,
                )
            )

        resource = {
            "refund": {"id": duplicate_billing_id, "version": 2, "expected": "refunded"},
            "api_key_revocation": {"id": api_key_id, "version": 2, "expected": "revoked"},
            "entitlement_change": {"id": subscription_id, "version": 3, "expected": 60},
        }[action_type]
        return {
            "action_type": action_type,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_subject": customer_subject,
            "approver_subject": approver_subject,
            "resource_id": resource["id"],
            "resource_version": resource["version"],
            "expected_effect": resource["expected"],
        }
    finally:
        await engine.dispose()


async def inspect_effect(tenant_id: str, approval_id: str) -> dict[str, Any]:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.tenant_id != tenant_id:
                raise RuntimeError("approval_identity_mismatch")
            actions = list(
                (
                    await session.scalars(
                        select(BusinessAction).where(
                            BusinessAction.tenant_id == tenant_id,
                            BusinessAction.approval_id == approval_id,
                        )
                    )
                ).all()
            )
            decisions = int(
                await session.scalar(
                    select(func.count(HumanDecision.id)).where(
                        HumanDecision.tenant_id == tenant_id,
                        HumanDecision.approval_id == approval_id,
                    )
                )
                or 0
            )
            resource_id = str(
                approval.action_payload.get("billing_record_id")
                or approval.action_payload.get("api_key_id")
                or approval.action_payload.get("subscription_id")
            )
            effect: str | int | None
            resource_version: int | None
            if approval.action_type == "refund":
                billing = await session.get(BillingRecord, resource_id)
                effect = None if billing is None else billing.status
                resource_version = None if billing is None else billing.version
            elif approval.action_type == "api_key_revocation":
                api_key = await session.scalar(
                    select(ApiKeyMetadata).where(
                        ApiKeyMetadata.tenant_id == tenant_id,
                        ApiKeyMetadata.key_id == resource_id,
                    )
                )
                effect = None if api_key is None else api_key.status
                resource_version = None if api_key is None else api_key.version
            else:
                subscription = await session.get(Subscription, resource_id)
                effect = None if subscription is None else subscription.concurrency_limit
                resource_version = None if subscription is None else subscription.version
            return {
                "tenant_id": tenant_id,
                "ticket_id": approval.ticket_id,
                "run_id": approval.run_id,
                "approval_id": approval.id,
                "approval_status": approval.status,
                "action_type": approval.action_type,
                "resource_id": resource_id,
                "resource_version": resource_version,
                "human_decision_count": decisions,
                "business_action_count": len(actions),
                "business_action_id": actions[0].id if len(actions) == 1 else None,
                "effect_identity": actions[0].effect_identity if len(actions) == 1 else None,
                "resource_effect": effect,
            }
    finally:
        await engine.dispose()


async def inspect_run(tenant_id: str, run_id: str) -> dict[str, Any]:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RuntimeError("run_identity_mismatch")
            invocations = list(
                (
                    await session.scalars(
                        select(ToolInvocation).where(
                            ToolInvocation.tenant_id == tenant_id,
                            ToolInvocation.run_id == run_id,
                        )
                    )
                ).all()
            )
            observations = list(
                (
                    await session.scalars(
                        select(ToolObservation).where(
                            ToolObservation.tenant_id == tenant_id,
                            ToolObservation.run_id == run_id,
                        )
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(AgentEvent)
                        .where(
                            AgentEvent.tenant_id == tenant_id,
                            AgentEvent.run_id == run_id,
                        )
                        .order_by(AgentEvent.run_sequence)
                    )
                ).all()
            )
            event_types = [item.event_type for item in events]
            decisions = [item for item in events if item.event_type == "agent_decision"]
            latest_observation_tokens = 0
            if len(decisions) >= 2:
                manifest = decisions[-1].payload.get("context_manifest") or {}
                sections = manifest.get("sections") or []
                latest_observation_tokens = sum(
                    int(item.get("token_count") or 0)
                    for item in sections
                    if item.get("name") == "latest_observations"
                )
            retrieval_count = int(
                await session.scalar(
                    select(func.count(RetrievalTrace.id)).where(
                        RetrievalTrace.tenant_id == tenant_id,
                        RetrievalTrace.run_id == run_id,
                        RetrievalTrace.trace_status == "terminal_ok",
                    )
                )
                or 0
            )
            membership_count = int(
                await session.scalar(
                    select(func.count(ContextMembership.id)).where(
                        ContextMembership.tenant_id == tenant_id,
                        ContextMembership.run_id == run_id,
                    )
                )
                or 0
            )
            citation_count = int(
                await session.scalar(
                    select(func.count(CitationBinding.id)).where(
                        CitationBinding.tenant_id == tenant_id,
                        CitationBinding.run_id == run_id,
                    )
                )
                or 0
            )
            finalized_markers = int(
                await session.scalar(
                    select(func.count(CheckpointCommitMarker.id)).where(
                        CheckpointCommitMarker.tenant_id == tenant_id,
                        CheckpointCommitMarker.run_id == run_id,
                        CheckpointCommitMarker.status == "finalized",
                    )
                )
                or 0
            )
            return {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "run_status": run.status,
                "decision_count": len(decisions),
                "tool_invocation_count": len(invocations),
                "terminal_invocation_count": sum(
                    item.lifecycle == "terminal" for item in invocations
                ),
                "observation_count": len(observations),
                "ok_observation_count": sum(item.status == "ok" for item in observations),
                "retrieval_trace_count": retrieval_count,
                "context_membership_count": membership_count,
                "citation_binding_count": citation_count,
                "observation_feedback_tokens": latest_observation_tokens,
                "policy_count": event_types.count("policy_decision"),
                "final_outcome_count": event_types.count("final_outcome"),
                "final_event_type": event_types[-1] if event_types else None,
                "finalized_marker_count": finalized_markers,
            }
    finally:
        await engine.dispose()


async def snapshot_run(tenant_id: str, run_id: str) -> dict[str, Any]:
    """Capture bounded, payload-free durable state for one accepted Agent run."""
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RuntimeError("run_identity_mismatch")
            ticket = await session.get(SupportTicket, run.ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                raise RuntimeError("ticket_identity_mismatch")

            jobs = list(
                (
                    await session.scalars(
                        select(RuntimeJob)
                        .where(
                            RuntimeJob.tenant_id == tenant_id,
                            RuntimeJob.run_id == run_id,
                        )
                        .order_by(RuntimeJob.created_at, RuntimeJob.id)
                    )
                ).all()
            )
            job_ids = [item.id for item in jobs]
            outbox = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.tenant_id == tenant_id,
                            OutboxEvent.run_id == run_id,
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    )
                ).all()
            )
            inbox = (
                list(
                    (
                        await session.scalars(
                            select(InboxDelivery)
                            .where(
                                InboxDelivery.tenant_id == tenant_id,
                                InboxDelivery.job_id.in_(job_ids),
                            )
                            .order_by(InboxDelivery.created_at, InboxDelivery.id)
                        )
                    ).all()
                )
                if job_ids
                else []
            )
            events = list(
                (
                    await session.scalars(
                        select(AgentEvent)
                        .where(
                            AgentEvent.tenant_id == tenant_id,
                            AgentEvent.run_id == run_id,
                        )
                        .order_by(AgentEvent.run_sequence)
                    )
                ).all()
            )
            proposals = list(
                (
                    await session.scalars(
                        select(ProposalRecord)
                        .where(
                            ProposalRecord.tenant_id == tenant_id,
                            ProposalRecord.run_id == run_id,
                        )
                        .order_by(ProposalRecord.created_at, ProposalRecord.id)
                    )
                ).all()
            )
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequest)
                        .where(
                            ApprovalRequest.tenant_id == tenant_id,
                            ApprovalRequest.run_id == run_id,
                        )
                        .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
                    )
                ).all()
            )
            approval_ids = [item.id for item in approvals]
            decisions = (
                list(
                    (
                        await session.scalars(
                            select(HumanDecision)
                            .where(
                                HumanDecision.tenant_id == tenant_id,
                                HumanDecision.approval_id.in_(approval_ids),
                            )
                            .order_by(HumanDecision.created_at, HumanDecision.id)
                        )
                    ).all()
                )
                if approval_ids
                else []
            )
            actions = list(
                (
                    await session.scalars(
                        select(BusinessAction)
                        .where(
                            BusinessAction.tenant_id == tenant_id,
                            BusinessAction.ticket_id == run.ticket_id,
                        )
                        .order_by(BusinessAction.created_at, BusinessAction.id)
                    )
                ).all()
            )
            markers = list(
                (
                    await session.scalars(
                        select(CheckpointCommitMarker)
                        .where(
                            CheckpointCommitMarker.tenant_id == tenant_id,
                            CheckpointCommitMarker.run_id == run_id,
                        )
                        .order_by(CheckpointCommitMarker.created_at, CheckpointCommitMarker.id)
                    )
                ).all()
            )
            capabilities = list(
                (
                    await session.scalars(
                        select(PolicyCapabilityInvocation)
                        .where(
                            PolicyCapabilityInvocation.tenant_id == tenant_id,
                            PolicyCapabilityInvocation.run_id == run_id,
                        )
                        .order_by(
                            PolicyCapabilityInvocation.sequence,
                            PolicyCapabilityInvocation.id,
                        )
                    )
                ).all()
            )
            capability_results = list(
                (
                    await session.scalars(
                        select(PolicyCapabilityResult)
                        .where(
                            PolicyCapabilityResult.tenant_id == tenant_id,
                            PolicyCapabilityResult.run_id == run_id,
                        )
                        .order_by(PolicyCapabilityResult.created_at, PolicyCapabilityResult.id)
                    )
                ).all()
            )
            heartbeats = list(
                (
                    await session.scalars(
                        select(ServiceInstanceHeartbeat)
                        .where(
                            ServiceInstanceHeartbeat.service.in_(
                                ("worker", "dispatcher", "reconciler")
                            )
                        )
                        .order_by(
                            ServiceInstanceHeartbeat.service,
                            ServiceInstanceHeartbeat.id,
                        )
                    )
                ).all()
            )

            snapshot = {
                "schema_version": FAILURE_SNAPSHOT_SCHEMA,
                "captured_at": datetime.now(UTC).isoformat(),
                "source_identity": {
                    "commit": os.getenv("TESTED_CODE_COMMIT"),
                    "tree": os.getenv("TESTED_TREE_HASH"),
                },
                "embedding": {
                    "mode": os.getenv("EMBEDDING_MODE", "unknown"),
                    "model": os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
                    "revision": os.getenv(
                        "EMBEDDING_REVISION",
                        "614241f622f53c4eeff9890bdc4f31cfecc418b3",
                    ),
                    "dimensions": 384,
                    "normalized_cosine": True,
                },
                "identity": {
                    "tenant_id": tenant_id,
                    "ticket_id": run.ticket_id,
                    "run_id": run_id,
                },
                "ticket": {
                    "id": ticket.id,
                    "status": ticket.status,
                    "issue_type": ticket.issue_type,
                    "risk": ticket.risk,
                    "version": ticket.version,
                    "next_event_sequence": ticket.next_event_sequence,
                },
                "run": {
                    "id": run.id,
                    "status": run.status,
                    "status_version": run.status_version,
                    "active_job_id": run.active_job_id,
                    "active_fencing_token": run.active_fencing_token,
                    "canonical_checkpoint_id": run.canonical_checkpoint_id,
                    "canonical_checkpoint_hash": run.canonical_checkpoint_hash,
                    "canonical_checkpoint_version": run.canonical_checkpoint_version,
                    "agent_finish_reason": run.agent_finish_reason,
                    "checkpoint_stage": run.checkpoint_stage,
                    "step_index": run.step_index,
                    "tool_rounds": run.tool_rounds,
                    "tool_attempts": run.tool_attempts,
                    "llm_calls": run.llm_calls,
                    "provider_mode": run.provider_mode,
                    "tool_call_mode": run.tool_call_mode,
                    "error_code": run.error_code,
                    "completed_at": _timestamp(run.completed_at),
                    "next_run_sequence": run.next_run_sequence,
                },
                "runtime_jobs": [
                    {
                        "id": item.id,
                        "approval_id": item.approval_id,
                        "kind": item.kind,
                        "status": item.status,
                        "status_version": item.status_version,
                        "attempt": item.attempt,
                        "available_at": _timestamp(item.available_at),
                        "lease_owner": item.lease_owner,
                        "lease_expires_at": _timestamp(item.lease_expires_at),
                        "heartbeat_at": _timestamp(item.heartbeat_at),
                        "fencing_token": item.fencing_token,
                        "outcome": item.outcome,
                        "terminal_error_code": item.last_error,
                        "terminal_at": _timestamp(item.terminal_at),
                        "delivery_hold_reason_code": item.delivery_hold_reason,
                    }
                    for item in jobs
                ],
                "outbox": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "delivery_id": item.delivery_id,
                        "delivery_generation": item.delivery_generation,
                        "event_type": item.event_type,
                        "available_at": _timestamp(item.available_at),
                        "published_at": _timestamp(item.published_at),
                        "last_delivery_at": _timestamp(item.last_delivery_at),
                        "publish_attempts": item.publish_attempts,
                        "superseded_at": _timestamp(item.superseded_at),
                        "delivery_state_version": item.delivery_state_version,
                    }
                    for item in outbox
                ],
                "inbox": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "delivery_id": item.delivery_id,
                        "consumer_group": item.consumer_group,
                        "status": item.status,
                        "outcome": item.outcome,
                        "terminal_at": _timestamp(item.terminal_at),
                    }
                    for item in inbox
                ],
                "events": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "ticket_sequence": item.ticket_sequence,
                        "run_sequence": item.run_sequence,
                        "step_index": item.step_index,
                        "tool_round": item.tool_round,
                        "event_type": item.event_type,
                        "status": item.status,
                        "visibility": item.visibility,
                        "event_hash": item.event_hash,
                        "delivery_generation": item.delivery_generation,
                        "fencing_token": item.fencing_token,
                        "created_at": _timestamp(item.created_at),
                    }
                    for item in events
                ],
                "proposals": [
                    {
                        "id": item.id,
                        "proposal_identity": item.proposal_identity,
                        "action_type": item.action_type,
                        "resource_id": item.resource_id,
                        "resource_version": item.resource_version,
                        "action_hash": item.action_hash,
                        "status": item.status,
                        "status_version": item.status_version,
                        "created_at": _timestamp(item.created_at),
                    }
                    for item in proposals
                ],
                "approvals": [
                    {
                        "id": item.id,
                        "proposal_id": item.proposal_id,
                        "checkpoint_id": item.checkpoint_id,
                        "checkpoint_version": item.checkpoint_version,
                        "marker_id": item.marker_id,
                        "action_type": item.action_type,
                        "action_hash": item.action_hash,
                        "business_version": item.business_version,
                        "status": item.status,
                        "status_version": item.status_version,
                        "selected_revision_id": item.selected_revision_id,
                        "selected_revision_number": item.selected_revision_number,
                        "decided_at": _timestamp(item.decided_at),
                        "consumed_at": _timestamp(item.consumed_at),
                    }
                    for item in approvals
                ],
                "policy_capabilities": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "segment_id": item.segment_id,
                        "fencing_token": item.fencing_token,
                        "capability_name": item.capability_name,
                        "sequence": item.sequence,
                        "effect_identity": item.effect_identity,
                        "status": item.status,
                        "error_code": item.error_code,
                        "completed_at": _timestamp(item.completed_at),
                    }
                    for item in capabilities
                ],
                "policy_results": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "invocation_id": item.invocation_id,
                        "effect_identity": item.effect_identity,
                        "status": item.status,
                        "payload_hash": item.payload_hash,
                        "reconciled_at": _timestamp(item.reconciled_at),
                    }
                    for item in capability_results
                ],
                "checkpoint_markers": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "fencing_token": item.fencing_token,
                        "delivery_generation": item.delivery_generation,
                        "segment_kind": item.segment_kind,
                        "status": item.status,
                        "status_version": item.status_version,
                        "final_checkpoint_id": item.final_checkpoint_id,
                        "final_checkpoint_hash": item.final_checkpoint_hash,
                        "final_checkpoint_version": item.final_checkpoint_version,
                        "segment_outcome": item.segment_outcome,
                    }
                    for item in markers
                ],
                "human_decisions": [
                    {
                        "id": item.id,
                        "approval_id": item.approval_id,
                        "decision": item.decision,
                        "action_hash": item.action_hash,
                        "decision_hash": item.decision_hash,
                        "canonical_event_id": item.canonical_event_id,
                    }
                    for item in decisions
                ],
                "business_actions": [
                    {
                        "id": item.id,
                        "approval_id": item.approval_id,
                        "action_type": item.action_type,
                        "resource_id": item.resource_id,
                        "resource_version": item.resource_version,
                        "action_hash": item.action_hash,
                        "effect_identity": item.effect_identity,
                        "status": item.status,
                        "canonical_event_id": item.canonical_event_id,
                    }
                    for item in actions
                ],
                "service_heartbeats": [
                    {
                        "id": item.id,
                        "service": item.service,
                        "capabilities": item.capabilities,
                        "version": item.version,
                        "status": item.status,
                        "last_heartbeat_at": _timestamp(item.last_heartbeat_at),
                        "timing_version": item.timing_version,
                        "runtime_config_hash": item.runtime_config_hash,
                    }
                    for item in heartbeats
                ],
                "durable_event_head": {
                    "ticket_next_event_sequence": ticket.next_event_sequence,
                    "run_next_run_sequence": run.next_run_sequence,
                    "max_ticket_sequence": max(
                        (item.ticket_sequence for item in events), default=0
                    ),
                    "max_run_sequence": max((item.run_sequence for item in events), default=0),
                    "last_event_type": events[-1].event_type if events else None,
                },
            }
            return _seal_failure_snapshot(snapshot)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    seed_parser = subcommands.add_parser("seed")
    seed_parser.add_argument(
        "--action-type",
        required=True,
        choices=("refund", "api_key_revocation", "entitlement_change"),
    )
    inspect_parser = subcommands.add_parser("inspect")
    inspect_parser.add_argument("--tenant-id", required=True)
    inspect_parser.add_argument("--approval-id", required=True)
    inspect_run_parser = subcommands.add_parser("inspect-run")
    inspect_run_parser.add_argument("--tenant-id", required=True)
    inspect_run_parser.add_argument("--run-id", required=True)
    snapshot_run_parser = subcommands.add_parser("snapshot-run")
    snapshot_run_parser.add_argument("--tenant-id", required=True)
    snapshot_run_parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "seed":
        payload = asyncio.run(seed(args.action_type))
    elif args.command == "inspect":
        payload = asyncio.run(inspect_effect(args.tenant_id, args.approval_id))
    elif args.command == "inspect-run":
        payload = asyncio.run(inspect_run(args.tenant_id, args.run_id))
    else:
        payload = asyncio.run(snapshot_run(args.tenant_id, args.run_id))
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
