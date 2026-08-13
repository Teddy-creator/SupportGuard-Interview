"""Action domain ORM entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from supportguard.db.base import Base, TimestampMixin
from supportguard.db.entities.foundation import (
    new_id,
    run_ticket_customer_scope_fk,
    runtime_job_scope_fk,
    tenant_resource_fk,
    tenant_run_scope_fk,
    ticket_customer_scope_fk,
)


class EscalationRecord(TimestampMixin, Base):
    __tablename__ = "escalation_records"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_escalation_ticket_customer_scope"),
        UniqueConstraint("idempotency_key", name="uq_escalation_idempotency"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_escalation_records_tenant_support_tickets"
        ),
        tenant_resource_fk(
            "customer_id", "customers", name="fk_escalation_records_tenant_customers"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("esc"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_approval_ticket_customer_scope"),
        run_ticket_customer_scope_fk(name="fk_approval_run_domain_scope"),
        UniqueConstraint("idempotency_key", name="uq_approval_idempotency"),
        UniqueConstraint("tenant_id", "id", name="uq_approval_requests_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "run_id",
            "ticket_id",
            name="uq_approval_requests_resume_scope",
        ),
        CheckConstraint(
            "status IN ("
            "'pending','approved','executed','rejected','stale','withdrawn','failed',"
            "'manual_takeover'"
            ")",
            name="approval_request_status_valid",
        ),
        CheckConstraint(
            "(action_type='refund' AND resource_type='billing_record_id') OR "
            "(action_type='api_key_revocation' AND resource_type='api_key_id') OR "
            "(action_type='entitlement_change' AND resource_type='subscription_id')",
            name="approval_resource_type_matches_action",
        ),
        Index("ix_approval_status_created", "status", "created_at"),
        Index(
            "ix_v1512_approval_identity_lookup",
            "tenant_id",
            "customer_id",
            "action_type",
            "resource_id",
            "id",
        ),
        Index(
            "uq_approval_active_resource",
            "tenant_id",
            "customer_id",
            "action_type",
            "resource_id",
            unique=True,
            postgresql_where=text("status IN ('pending','approved')"),
            sqlite_where=text("status IN ('pending','approved')"),
        ),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_approval_requests_tenant_support_tickets"
        ),
        tenant_resource_fk(
            "customer_id", "customers", name="fk_approval_requests_tenant_customers"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_turn_id", "ticket_id", "run_id"],
            [
                "conversation_turns.tenant_id",
                "conversation_turns.id",
                "conversation_turns.ticket_id",
                "conversation_turns.run_id",
            ],
            name="fk_approval_origin_turn_scope",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("approval")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("proposal_records.id"), unique=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.id"))
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    canonical_checkpoint_ns: Mapped[str | None] = mapped_column(String(300))
    canonical_checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marker_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("checkpoint_commit_markers.id")
    )
    expected_ticket_head_event_id: Mapped[str | None] = mapped_column(String(64))
    expected_ticket_sequence: Mapped[int | None] = mapped_column(BigInteger)
    expected_ticket_event_hash: Mapped[str | None] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    origin_turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    review_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    business_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    approver_id: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    approver_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_revision_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "approval_action_revisions.id",
            use_alter=True,
            name="fk_approval_requests_selected_revision",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    selected_revision_number: Mapped[int | None] = mapped_column(Integer)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Approval transitions use the frozen column-level Worker CAS grant. Avoid
    # TimestampMixin silently expanding an ORM status transition to updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __mapper_args__ = {"version_id_col": status_version}


class ProposalWithdrawal(TimestampMixin, Base):
    __tablename__ = "proposal_withdrawals"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_proposal_withdrawal_approval"),
        UniqueConstraint("tenant_id", "id", name="uq_proposal_withdrawals_tenant_id_id"),
        ticket_customer_scope_fk(name="fk_proposal_withdrawal_ticket_customer_scope"),
        tenant_resource_fk(
            "ticket_id",
            "support_tickets",
            name="fk_proposal_withdrawal_tenant_ticket",
        ),
        tenant_resource_fk(
            "customer_id",
            "customers",
            name="fk_proposal_withdrawal_tenant_customer",
        ),
        tenant_resource_fk(
            "approval_id",
            "approval_requests",
            name="fk_proposal_withdrawal_approval_scope",
        ),
        tenant_resource_fk(
            "proposal_id",
            "proposal_records",
            name="fk_proposal_withdrawal_proposal_scope",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("withdrawal")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class BusinessAction(TimestampMixin, Base):
    __tablename__ = "business_actions"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_business_action_ticket_customer_scope"),
        UniqueConstraint("action_type", "idempotency_key", name="uq_action_type_idempotency"),
        UniqueConstraint(
            "tenant_id",
            "action_type",
            "resource_id",
            "resource_version",
            name="uq_business_action_resource_effect",
        ),
        UniqueConstraint("tenant_id", "effect_identity", name="uq_business_action_effect_identity"),
        Index(
            "ix_business_action_approval_created",
            "tenant_id",
            "approval_id",
            "created_at",
            "id",
        ),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_business_actions_tenant_support_tickets"
        ),
        tenant_resource_fk("customer_id", "customers", name="fk_business_actions_tenant_customers"),
        tenant_resource_fk(
            "approval_id", "approval_requests", name="fk_business_actions_tenant_approvals"
        ),
        tenant_resource_fk(
            "human_decision_id",
            "human_decisions",
            name="fk_business_actions_tenant_human_decisions",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("action"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    resource_version: Mapped[int | None] = mapped_column(Integer)
    action_hash: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("approval_requests.id"))
    human_decision_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("human_decisions.id")
    )
    action_revision_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("approval_action_revisions.id", name="fk_business_action_revision")
    )
    decision_hash: Mapped[str | None] = mapped_column(String(64))
    effect_identity: Mapped[str | None] = mapped_column(String(64))
    canonical_event_id: Mapped[str | None] = mapped_column(String(64))
    canonical_event_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class FinalizerPayload(TimestampMixin, Base):
    __tablename__ = "finalizer_payloads"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_finalizer_payload_runtime_job_scope"),
        UniqueConstraint("marker_id", name="uq_finalizer_payload_marker"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("final"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    marker_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("checkpoint_commit_markers.id"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    full_payload: Mapped[dict[str, Any]] = mapped_column("payload", JSON, nullable=False)
    state_delta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    domain_delta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expected_heads: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ProposalRecord(TimestampMixin, Base):
    __tablename__ = "proposal_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "proposal_identity", name="uq_proposal_identity"),
        CheckConstraint("status IN ('draft','bound','stale')", name="proposal_status_valid"),
        CheckConstraint(
            "(action_type='refund' AND ((refund_original_resource_id IS NULL AND "
            "refund_original_version IS NULL AND refund_pair_hash IS NULL) OR "
            "(refund_original_resource_id IS NOT NULL AND refund_original_version IS NOT NULL "
            "AND refund_pair_hash IS NOT NULL))) OR (action_type<>'refund' AND "
            "refund_original_resource_id IS NULL AND refund_original_version IS NULL AND "
            "refund_pair_hash IS NULL)",
            name="refund_pair_binding_complete",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_proposal_records_tenant_agent_runs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("proposal")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    proposal_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observation_binding: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refund_original_resource_id: Mapped[str | None] = mapped_column(String(128))
    refund_original_version: Mapped[int | None] = mapped_column(Integer)
    refund_pair_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Proposal transitions are a frozen two-column CAS surface. The generic
    # TimestampMixin on-update clause would silently add ``updated_at`` to the
    # UPDATE statement and exceed the Worker column grant.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __mapper_args__ = {"version_id_col": status_version}


class HumanDecision(TimestampMixin, Base):
    __tablename__ = "human_decisions"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_human_decision_approval"),
        UniqueConstraint("tenant_id", "id", name="uq_human_decisions_tenant_id_id"),
        tenant_resource_fk(
            "approval_id",
            "approval_requests",
            name="fk_human_decisions_tenant_approval_requests",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("decision")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    approval_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("approval_requests.id"), nullable=False
    )
    action_revision_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("approval_action_revisions.id", name="fk_human_decision_revision")
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    action_hash: Mapped[str | None] = mapped_column(String(64))
    decision_hash: Mapped[str | None] = mapped_column(String(64))
    canonical_event_id: Mapped[str | None] = mapped_column(String(64))
    canonical_event_hash: Mapped[str | None] = mapped_column(String(64))
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ApprovalSnapshot(TimestampMixin, Base):
    __tablename__ = "approval_snapshots"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_approval_snapshot_run_scope"),
        ticket_customer_scope_fk(name="fk_approval_snapshot_ticket_customer_scope"),
        run_ticket_customer_scope_fk(name="fk_approval_snapshot_run_domain_scope"),
        UniqueConstraint("approval_id", name="uq_approval_snapshot_approval"),
        UniqueConstraint("tenant_id", "id", name="uq_approval_snapshots_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["support_tickets.tenant_id", "support_tickets.id"],
            name="fk_approval_snapshots_ticket",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_approval_snapshots_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "origin_job_id", "origin_marker_id", "origin_fencing_token"],
            [
                "checkpoint_commit_markers.tenant_id",
                "checkpoint_commit_markers.run_id",
                "checkpoint_commit_markers.job_id",
                "checkpoint_commit_markers.id",
                "checkpoint_commit_markers.fencing_token",
            ],
            name="fk_approval_snapshots_origin_marker_fence",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("snapshot")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", name="fk_snap_tenant"), nullable=False
    )
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id", name="fk_snap_ticket_id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", name="fk_snap_customer_id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_runs.id", name="fk_snap_run_id"), nullable=False
    )
    approval_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("approval_requests.id", name="fk_snap_approval_id"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("proposal_records.id", name="fk_snap_proposal_id"), nullable=False
    )
    origin_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_marker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_segment_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_binding: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    citation_binding_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class ApprovalActionRevision(TimestampMixin, Base):
    __tablename__ = "approval_action_revisions"
    __table_args__ = (
        UniqueConstraint(
            "approval_id", "revision_number", name="uq_approval_action_revision_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_approval_action_revisions_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_requests.tenant_id", "approval_requests.id"],
            name="fk_approval_action_revisions_approval",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["approval_snapshots.tenant_id", "approval_snapshots.id"],
            name="fk_approval_action_revisions_snapshot",
        ),
        CheckConstraint("revision_number >= 0", name="approval_revision_number_nonnegative"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("revision")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", name="fk_revision_tenant"), nullable=False
    )
    approval_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("approval_requests.id", name="fk_revision_approval_id"),
        nullable=False,
    )
    proposal_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("proposal_records.id", name="fk_revision_proposal_id"),
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("approval_snapshots.id", name="fk_revision_snapshot_id"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_reason: Mapped[str] = mapped_column(String(64), nullable=False)
