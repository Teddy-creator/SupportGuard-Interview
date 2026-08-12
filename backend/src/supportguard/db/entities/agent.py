"""Agent domain ORM entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
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
    ticket_customer_scope_fk,
)


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_agent_run_ticket_customer_scope"),
        UniqueConstraint("message_id", name="uq_agent_run_message"),
        UniqueConstraint("tenant_id", "id", name="uq_agent_runs_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "ticket_id",
            name="uq_agent_runs_tenant_id_ticket_id",
        ),
        UniqueConstraint("turn_id", name="uq_agent_runs_turn"),
        Index("ix_agent_run_ticket_created", "ticket_id", "created_at"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_agent_runs_tenant_support_tickets"
        ),
        tenant_resource_fk("customer_id", "customers", name="fk_agent_runs_tenant_customers"),
        ForeignKeyConstraint(
            ["tenant_id", "id", "active_job_id"],
            ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
            name="fk_agent_run_active_job_same_run",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "(active_job_id IS NULL AND active_fencing_token IS NULL) OR "
            "(active_job_id IS NOT NULL AND active_fencing_token IS NOT NULL)",
            name="agent_run_active_job_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("run"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ticket_messages.id"), nullable=False
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("conversation_turns.id", use_alter=True, name="fk_agent_run_turn")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_run_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active_job_id: Mapped[str | None] = mapped_column(String(64))
    active_fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    canonical_checkpoint_ns: Mapped[str | None] = mapped_column(String(300))
    canonical_checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    canonical_checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    canonical_checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_finish_reason: Mapped[str | None] = mapped_column(String(64))
    checkpoint_stage: Mapped[str] = mapped_column(
        String(64), nullable=False, default="request_created"
    )
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_call_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_index_version: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_agent_event_ticket_customer_scope"),
        run_ticket_customer_scope_fk(name="fk_agent_event_run_domain_scope"),
        UniqueConstraint("run_id", "run_sequence", name="uq_agent_event_run_sequence_v12"),
        UniqueConstraint("ticket_id", "ticket_sequence", name="uq_agent_event_ticket_sequence_v12"),
        Index("ix_agent_event_ticket_sequence", "ticket_id", "ticket_sequence"),
        tenant_resource_fk("run_id", "agent_runs", name="fk_agent_events_tenant_agent_runs"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_agent_events_tenant_support_tickets"
        ),
        tenant_resource_fk("customer_id", "customers", name="fk_agent_events_tenant_customers"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id"],
            ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
            name="fk_agent_events_job_same_run",
            use_alter=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("event"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    run_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    job_id: Mapped[str | None] = mapped_column(String(64))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    previous_event_id: Mapped[str | None] = mapped_column(String(64))
    parent_event_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="support-ticket-event.v1"
    )
    canonicalization_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="json-sort-keys.v1"
    )
    event_hash_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="event-hash.v1"
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_id: Mapped[str | None] = mapped_column(String(128))
    delivery_generation: Mapped[int | None] = mapped_column(Integer)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuntimeJob(TimestampMixin, Base):
    __tablename__ = "runtime_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('agent_start','approval_resume')", name="runtime_job_kind_valid"),
        CheckConstraint(
            "(kind='agent_start' AND approval_id IS NULL) OR "
            "(kind='approval_resume' AND approval_id IS NOT NULL)",
            name="runtime_job_kind_approval_shape",
        ),
        CheckConstraint(
            "status IN ('queued','leased','succeeded','retry_wait','dead')",
            name="runtime_job_status_valid",
        ),
        CheckConstraint(
            "attempt >= 0 AND fencing_token >= 0", name="runtime_job_counters_nonnegative"
        ),
        Index("ix_runtime_job_due", "status", "available_at"),
        Index(
            "ix_v1512_runtime_job_ticket_dispatch_lookup",
            "tenant_id",
            "ticket_id",
            "dispatch_sequence",
            "id",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runtime_jobs_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_runtime_jobs_tenant_run_id"),
        UniqueConstraint(
            "tenant_id",
            "ticket_id",
            "dispatch_sequence",
            name="uq_runtime_jobs_ticket_dispatch_sequence",
        ),
        CheckConstraint(
            "(delivery_hold_reason IS NULL AND delivery_hold_intent_id IS NULL "
            "AND delivery_hold_since IS NULL) OR "
            "(delivery_hold_reason='state_unknown' AND delivery_hold_intent_id IS NOT NULL "
            "AND delivery_hold_since IS NOT NULL)",
            name="runtime_job_hold_shape",
        ),
        Index(
            "uq_runtime_jobs_single_leased_run",
            "tenant_id",
            "run_id",
            unique=True,
            postgresql_where=text("status='leased'"),
            sqlite_where=text("status='leased'"),
        ),
        Index(
            "uq_runtime_jobs_single_leased_ticket",
            "tenant_id",
            "ticket_id",
            unique=True,
            postgresql_where=text("status='leased'"),
            sqlite_where=text("status='leased'"),
        ),
        tenant_resource_fk(
            "ticket_id",
            "support_tickets",
            name="fk_runtime_jobs_tenant_support_tickets",
        ),
        CheckConstraint(
            "dispatch_sequence > 0",
            name="runtime_job_dispatch_sequence_positive",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_runtime_jobs_tenant_agent_runs"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "ticket_id"],
            ["agent_runs.tenant_id", "agent_runs.id", "agent_runs.ticket_id"],
            name="fk_runtime_jobs_run_ticket_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approval_id", "run_id", "ticket_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.id",
                "approval_requests.run_id",
                "approval_requests.ticket_id",
            ],
            name="fk_runtime_jobs_approval_resume_scope",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["timing_version"],
            ["supportguard_control.runtime_timing_snapshots.timing_version"],
            name="fk_runtime_jobs_timing_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "id", "delivery_hold_intent_id"],
            [
                "reconcile_intents.tenant_id",
                "reconcile_intents.run_id",
                "reconcile_intents.job_id",
                "reconcile_intents.id",
            ],
            name="fk_runtime_job_delivery_hold_intent",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=FetchedValue(),
    )
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    dispatch_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=FetchedValue(),
    )
    approval_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    timing_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(128))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_hold_reason: Mapped[str | None] = mapped_column(Text)
    delivery_hold_intent_id: Mapped[str | None] = mapped_column(Text)
    delivery_hold_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckpointCommitMarker(TimestampMixin, Base):
    __tablename__ = "checkpoint_commit_markers"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_marker_runtime_job_scope"),
        UniqueConstraint("run_id", "job_id", "fencing_token", name="uq_marker_run_job_fence"),
        CheckConstraint(
            "status IN ('prepared','checkpoint_written','finalized','aborted')",
            name="marker_status_valid",
        ),
        tenant_resource_fk(
            "run_id", "agent_runs", name="fk_checkpoint_commit_markers_tenant_agent_runs"
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "id",
            "fencing_token",
            name="uq_marker_tenant_run_job_id_fence",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("marker"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    private_namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    canonical_parent_id: Mapped[str | None] = mapped_column(String(128))
    canonical_parent_hash: Mapped[str | None] = mapped_column(String(64))
    parent_checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_run_status: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_ticket_head_event_id: Mapped[str | None] = mapped_column(String(64))
    expected_ticket_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_ticket_event_hash: Mapped[str | None] = mapped_column(String(64))
    prepared_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    final_checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    final_checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    final_checkpoint_version: Mapped[int | None] = mapped_column(Integer)
    segment_outcome: Mapped[str | None] = mapped_column(String(64))


class AgentCallAttempt(TimestampMixin, Base):
    __tablename__ = "agent_call_attempts"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_agent_attempt_runtime_job_scope"),
        UniqueConstraint("run_id", "call_kind", "ordinal", name="uq_attempt_run_kind_ordinal"),
        CheckConstraint(
            "status IN ('started','succeeded','failed','unknown')", name="attempt_status_valid"
        ),
        CheckConstraint(
            "(call_kind = 'read_mcp' AND logical_invocation_id IS NOT NULL "
            "AND transport_ordinal IS NOT NULL) OR "
            "(call_kind = 'tool_preflight' AND logical_invocation_id IS NOT NULL "
            "AND transport_ordinal IS NULL) OR "
            "(call_kind IN ('llm','structure_repair') AND logical_invocation_id IS NULL "
            "AND transport_ordinal IS NULL)",
            name="attempt_v124_identity_valid",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_agent_call_attempts_tenant_agent_runs"),
        tenant_resource_fk(
            "logical_invocation_id",
            "tool_invocations",
            name="fk_agent_call_attempts_tenant_tool_invocations",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "id",
            name="uq_agent_attempt_tenant_run_job_id_v126",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("attempt"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    call_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_invocation_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("tool_invocations.id")
    )
    transport_ordinal: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    runtime_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ToolTransportAttempt(TimestampMixin, Base):
    __tablename__ = "tool_transport_attempts"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_transport_attempt_runtime_job_scope"),
        UniqueConstraint("agent_call_attempt_id", name="uq_transport_agent_call_attempt"),
        UniqueConstraint(
            "invocation_id", "transport_ordinal", name="uq_transport_invocation_ordinal"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tool_transport_attempts_tenant_id_id"),
        CheckConstraint("transport_ordinal >= 1", name="transport_ordinal_positive"),
        CheckConstraint(
            "status IN ('reserved','executing','succeeded','failed','unknown')",
            name="transport_attempt_status_valid",
        ),
        tenant_resource_fk(
            "invocation_id",
            "tool_invocations",
            name="fk_transport_attempts_tenant_tool_invocations",
        ),
        tenant_resource_fk(
            "agent_call_attempt_id",
            "agent_call_attempts",
            name="fk_transport_attempts_tenant_agent_call_attempts",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_transport_attempts_tenant_runs"),
        tenant_resource_fk("job_id", "runtime_jobs", name="fk_transport_attempts_tenant_jobs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("transport")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tool_invocations.id"), nullable=False
    )
    agent_call_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transport_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawProviderDecisionEnvelope(TimestampMixin, Base):
    __tablename__ = "raw_provider_decision_envelopes"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_raw_decision_runtime_job_scope"),
        UniqueConstraint("provider_attempt_id", name="uq_raw_decision_provider_attempt"),
        CheckConstraint(
            "intake_status IN ('received','parsed','rejected')",
            name="raw_decision_intake_status_valid",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_raw_decisions_tenant_agent_runs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("rawdecision")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    call_count: Mapped[int] = mapped_column(Integer, nullable=False)
    call_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    intake_status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    rejection_code: Mapped[str | None] = mapped_column(String(100))


class PolicyCapabilityInvocation(TimestampMixin, Base):
    __tablename__ = "policy_capability_invocations"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_policy_invocation_runtime_job_scope"),
        UniqueConstraint("run_id", "segment_id", "sequence", name="uq_policy_capability_sequence"),
        UniqueConstraint("tenant_id", "effect_identity", name="uq_policy_capability_effect"),
        UniqueConstraint("tenant_id", "id", name="uq_policy_capability_invocations_tenant_id_id"),
        CheckConstraint(
            "status IN ('reserved','executing','succeeded','failed','denied','unknown')",
            name="policy_capability_status_valid",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_policy_capabilities_tenant_agent_runs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("capability")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capability_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    causal_decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyCapabilityAttempt(TimestampMixin, Base):
    __tablename__ = "policy_capability_attempts"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_policy_attempt_runtime_job_scope"),
        UniqueConstraint("invocation_id", "ordinal", name="uq_capability_attempt_ordinal"),
        UniqueConstraint("tenant_id", "id", name="uq_policy_capability_attempts_tenant_id_id"),
        CheckConstraint("ordinal >= 1", name="capability_attempt_ordinal_positive"),
        CheckConstraint(
            "status IN ('reserved','executing','succeeded','failed','unknown')",
            name="capability_attempt_status_valid",
        ),
        tenant_resource_fk(
            "invocation_id",
            "policy_capability_invocations",
            name="fk_capability_attempts_tenant_invocations",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_capability_attempts_tenant_runs"),
        tenant_resource_fk("job_id", "runtime_jobs", name="fk_capability_attempts_tenant_jobs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("capattempt")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy_capability_invocations.id"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyCapabilityResult(TimestampMixin, Base):
    __tablename__ = "policy_capability_results"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_policy_result_runtime_job_scope"),
        UniqueConstraint("invocation_id", name="uq_capability_result_invocation"),
        UniqueConstraint("tenant_id", "effect_identity", name="uq_capability_result_effect"),
        CheckConstraint(
            "status IN ('succeeded','failed','denied','unknown')",
            name="capability_result_status_valid",
        ),
        tenant_resource_fk(
            "invocation_id",
            "policy_capability_invocations",
            name="fk_capability_results_tenant_invocations",
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_capability_results_tenant_runs"),
        tenant_resource_fk("job_id", "runtime_jobs", name="fk_capability_results_tenant_jobs"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("capresult")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy_capability_invocations.id"), nullable=False
    )
    effect_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TurnGroup(TimestampMixin, Base):
    __tablename__ = "turn_groups"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_turn_group_runtime_job_scope"),
        UniqueConstraint("run_id", "decision_ordinal", name="uq_turn_group_run_decision"),
        CheckConstraint(
            "status IN ('open','completing','closed','aborted')",
            name="turn_group_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("turn"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_round: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_invocations: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolInvocation(TimestampMixin, Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_tool_invocation_runtime_job_scope"),
        UniqueConstraint("turn_group_id", "ordinal", name="uq_invocation_turn_ordinal"),
        CheckConstraint(
            "lifecycle IN ('received','validated','authorized','executing','terminal')",
            name="tool_invocation_lifecycle_valid",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('succeeded','invalid_input','forbidden_tool','denied','budget_exhausted',"
            "'no_progress','stale_fence','failed','timed_out','cancelled','unknown')",
            name="tool_invocation_outcome_valid",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "id",
            name="uq_tool_invocation_origin_v126",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("invocation")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    turn_group_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("turn_groups.id"), nullable=False
    )
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    logical_invocation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    outcome: Mapped[str | None] = mapped_column(String(32))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logical_time_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ToolObservation(TimestampMixin, Base):
    __tablename__ = "tool_observations"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_tool_observation_runtime_job_scope"),
        UniqueConstraint("invocation_id", name="uq_tool_observation_invocation"),
        CheckConstraint("attempt_index >= 1", name="tool_observation_attempt_positive"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "invocation_id",
            "id",
            name="uq_tool_observation_origin_v126",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("observation")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tool_invocations.id"), nullable=False
    )
    segment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ProviderRuntimeEvent(TimestampMixin, Base):
    __tablename__ = "provider_runtime_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('initialized','initialization_failed')",
            name="provider_runtime_event_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("provider_event")
    )
    service_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_call_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    runtime_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
