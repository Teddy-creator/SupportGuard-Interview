"""Audit domain ORM entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from supportguard.db.base import Base, TimestampMixin
from supportguard.db.entities.foundation import (
    new_id,
    run_ticket_customer_scope_fk,
    runtime_job_scope_fk,
    tenant_resource_fk,
    tenant_run_scope_fk,
)


class RuntimeTimingSnapshot(Base):
    __tablename__ = "runtime_timing_snapshots"
    __table_args__ = (
        Index(
            "uq_runtime_timing_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
        {"schema": "supportguard_control"},
    )

    timing_version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    max_job_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    redelivery_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    backlog_count_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    oldest_backlog_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DatabaseIdentity(Base):
    __tablename__ = "database_identity"
    __table_args__ = (
        UniqueConstraint("database_name", name="database_identity_database_name_key"),
        {"schema": "supportguard_control"},
    )

    database_uuid: Mapped[Any] = mapped_column(Uuid, primary_key=True)
    database_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UpgradeRun(Base):
    __tablename__ = "upgrade_runs"
    __table_args__ = (
        Index(
            "uq_upgrade_single_active",
            "database_uuid",
            unique=True,
            postgresql_where=text("phase <> 'attested'"),
        ),
        {"schema": "supportguard_control"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    database_uuid: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("supportguard_control.database_identity.database_uuid"), nullable=False
    )
    source_revision: Mapped[str] = mapped_column(Text, nullable=False)
    target_revision: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    actor_instance_id: Mapped[str] = mapped_column("actor_instance_ref", Text, nullable=False)
    preflight_manifest_hash: Mapped[str | None] = mapped_column(Text)
    external_backup_hash: Mapped[str | None] = mapped_column(Text)
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpgradePhaseEvent(Base):
    __tablename__ = "upgrade_phase_events"
    __table_args__ = (
        UniqueConstraint(
            "upgrade_run_id",
            "sequence",
            name="upgrade_phase_events_upgrade_run_id_sequence_key",
        ),
        {"schema": "supportguard_control"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    upgrade_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("supportguard_control.upgrade_runs.id"), nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UpgradeAttestation(Base):
    __tablename__ = "upgrade_attestations"
    __table_args__ = (
        UniqueConstraint("upgrade_run_id", name="upgrade_attestations_upgrade_run_id_key"),
        UniqueConstraint("attestation_hash", name="upgrade_attestations_attestation_hash_key"),
        {"schema": "supportguard_control"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    upgrade_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("supportguard_control.upgrade_runs.id"), nullable=False
    )
    schema_hash: Mapped[str] = mapped_column(Text, nullable=False)
    row_inventory_hash: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_locator_hash: Mapped[str] = mapped_column(Text, nullable=False)
    summary_lineage_hash: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WriterBarrierReceipt(Base):
    __tablename__ = "writer_barrier_receipts"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('dispatcher','worker','reconciler','maintenance')",
            name=conv("writer_barrier_receipts_operation_check"),
        ),
        CheckConstraint(
            "backend_pid > 0",
            name=conv("writer_barrier_receipts_backend_pid_check"),
        ),
        CheckConstraint(
            "(permitted AND denial_code IS NULL) OR (NOT permitted AND denial_code IS NOT NULL)",
            name=conv("writer_barrier_receipts_check"),
        ),
        CheckConstraint(
            "(drain_job_id IS NULL AND drain_run_id IS NULL "
            "AND drain_owner IS NULL AND drain_fencing_token IS NULL "
            "AND tenant_id IS NULL) OR "
            "(drain_job_id IS NOT NULL AND drain_run_id IS NOT NULL "
            "AND drain_owner IS NOT NULL AND drain_fencing_token > 0 "
            "AND tenant_id IS NOT NULL)",
            name=conv("writer_barrier_receipts_check1"),
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_writer_barrier_receipt_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "drain_run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name="fk_writer_barrier_receipt_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "drain_run_id", "drain_job_id"],
            ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
            name="fk_writer_barrier_receipt_job",
        ),
        Index(
            "ix_writer_barrier_receipts_live",
            "backend_pid",
            "session_nonce",
            postgresql_where=text("released_at IS NULL"),
        ),
        {"schema": "supportguard_control"},
    )

    session_nonce: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    backend_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    upgrade_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("supportguard_control.upgrade_runs.id")
    )
    fence_phase: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str | None] = mapped_column(Text)
    drain_job_id: Mapped[str | None] = mapped_column(Text)
    drain_run_id: Mapped[str | None] = mapped_column(Text)
    drain_owner: Mapped[str | None] = mapped_column(Text)
    drain_fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    permitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    denial_code: Mapped[str | None] = mapped_column(Text)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MutationKillSwitch(TimestampMixin, Base):
    __tablename__ = "mutation_kill_switches"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('refund','api_key_revocation','entitlement_change')",
            name="mutation_kill_switch_action_valid",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)


class IdempotencyRequest(TimestampMixin, Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "route",
            "idempotency_key",
            name="uq_idempotency_request_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("idem"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    route: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    resource_ids: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_class: Mapped[str | None] = mapped_column(String(64))


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("job_id", "delivery_generation", name="uq_outbox_job_generation"),
        UniqueConstraint("tenant_id", "run_id", "job_id", "id", name="uq_outbox_tenant_run_job_id"),
        Index("ix_outbox_publish_due", "published_at", "available_at"),
        tenant_resource_fk("job_id", "runtime_jobs", name="fk_outbox_events_tenant_runtime_jobs"),
        tenant_resource_fk("run_id", "agent_runs", name="fk_outbox_events_tenant_agent_runs"),
        runtime_job_scope_fk(name="fk_outbox_event_runtime_job_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id", "superseded_by_delivery_id"],
            [
                "outbox_events.tenant_id",
                "outbox_events.run_id",
                "outbox_events.job_id",
                "outbox_events.id",
            ],
            name="fk_outbox_superseded_by_same_job",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "uq_outbox_current_job",
            "job_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
            sqlite_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("outbox"))
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    redis_message_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="runtime-job.v1"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_delivery_id: Mapped[str | None] = mapped_column(Text)
    delivery_state_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class InboxDelivery(TimestampMixin, Base):
    __tablename__ = "inbox_deliveries"
    __table_args__ = (
        UniqueConstraint("consumer_group", "delivery_id", name="uq_inbox_group_delivery"),
        Index("ix_inbox_job", "job_id"),
        CheckConstraint(
            "status IN ('received','claimed','acked','rejected')", name="inbox_status_valid"
        ),
        tenant_resource_fk(
            "job_id", "runtime_jobs", name="fk_inbox_deliveries_tenant_runtime_jobs"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inbox"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redis_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consumer_group: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    outcome: Mapped[str | None] = mapped_column(String(64))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReconcileIntent(TimestampMixin, Base):
    __tablename__ = "reconcile_intents"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_reconcile_intent_run_scope"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "id",
            name="reconcile_intents_tenant_id_run_id_job_id_id_key",
        ),
        UniqueConstraint(
            "job_id",
            "delivery_generation",
            "reason",
            "intent_sequence",
            name="reconcile_intents_job_id_delivery_generation_reason_intent__key",
        ),
        UniqueConstraint("observation_nonce", name="reconcile_intents_observation_nonce_key"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id"],
            ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
            name="fk_reconcile_intent_job",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id", "outbox_id"],
            [
                "outbox_events.tenant_id",
                "outbox_events.run_id",
                "outbox_events.job_id",
                "outbox_events.id",
            ],
            name="fk_reconcile_intent_outbox",
        ),
        Index(
            "uq_reconcile_active_intent",
            "job_id",
            "delivery_generation",
            "reason",
            unique=True,
            postgresql_where=text("status IN ('prepared','observed')"),
            sqlite_where=text("status IN ('prepared','observed')"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("rintent"))
    tenant_id: Mapped[str] = mapped_column(Text, ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    outbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prepared_job_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prepared_delivery_state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prepared_inbox_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prepared_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expected_stream_payload_hash: Mapped[str | None] = mapped_column(String(64))
    observation_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(Text)


class RedisDeliveryObservation(TimestampMixin, Base):
    __tablename__ = "redis_delivery_observations"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_redis_observation_run_scope"),
        runtime_job_scope_fk(name="fk_redis_observation_runtime_job_scope"),
        UniqueConstraint(
            "reconcile_intent_id",
            "observation_hash",
            name="redis_delivery_observations_reconcile_intent_id_observation_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id", "reconcile_intent_id"],
            [
                "reconcile_intents.tenant_id",
                "reconcile_intents.run_id",
                "reconcile_intents.job_id",
                "reconcile_intents.id",
            ],
            name="fk_redis_observation_intent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id", "outbox_id"],
            [
                "outbox_events.tenant_id",
                "outbox_events.run_id",
                "outbox_events.job_id",
                "outbox_events.id",
            ],
            name="fk_redis_observation_outbox",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("redisobs"))
    tenant_id: Mapped[str] = mapped_column(Text, ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    reconcile_intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    outbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_hash: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RetentionTrimIntent(TimestampMixin, Base):
    __tablename__ = "retention_trim_intents"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_retention_trim_intent_run_scope"),
        UniqueConstraint("tenant_id", "id", name="uq_retention_trim_intent_tenant_id"),
        UniqueConstraint(
            "stream",
            "redis_message_locator",
            name="uq_retention_trim_stream_message",
        ),
        CheckConstraint(
            "status IN ('planned','authorized','redis_trimmed','finalized',"
            "'aborted','unknown_trim_state')",
            name="retention_trim_intent_status_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "job_id"],
            ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
            name="fk_retention_trim_intent_job",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # These are immutable post-trim locators, not live relational identities.
    # Keep the Python names used by the retention protocol while giving the
    # physical columns non-``*_id`` names so the database reference contract
    # cannot mistake them for missing foreign keys after the source rows are
    # intentionally removed.
    outbox_id: Mapped[str] = mapped_column("outbox_locator", String(64), nullable=False)
    stream: Mapped[str] = mapped_column(String(256), nullable=False)
    redis_message_id: Mapped[str] = mapped_column(
        "redis_message_locator", String(64), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    group_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    eligibility_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dependency_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redis_trimmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aborted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_receipt_hash: Mapped[str | None] = mapped_column(String(64))


class RetentionTrimReceipt(TimestampMixin, Base):
    __tablename__ = "retention_trim_receipts"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_retention_trim_receipt_intent"),
        ForeignKeyConstraint(
            ["tenant_id", "intent_id"],
            ["retention_trim_intents.tenant_id", "retention_trim_intents.id"],
            name="fk_retention_trim_receipt_intent",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redis_message_id: Mapped[str] = mapped_column(
        "redis_message_locator", String(64), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    group_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redis_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QueueDeliveryAudit(TimestampMixin, Base):
    """Durable evidence for poison, rejected, and reclaimed Redis deliveries."""

    __tablename__ = "queue_delivery_audits"
    __table_args__ = (
        Index("ix_queue_audit_redis_message", "redis_message_id"),
        Index(
            "uq_queue_maintenance_trim_message",
            "redis_message_id",
            "consumer_group",
            unique=True,
            postgresql_where=text("consumer_group = 'maintenance-trim'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("qaudit"))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(String(64))
    delivery_id: Mapped[str | None] = mapped_column(String(64))
    redis_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consumer_group: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ServiceInstanceHeartbeat(TimestampMixin, Base):
    __tablename__ = "service_instance_heartbeats"
    __table_args__ = (
        ForeignKeyConstraint(
            ["timing_version"],
            ["supportguard_control.runtime_timing_snapshots.timing_version"],
            name="fk_service_heartbeat_timing_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timing_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    runtime_config_hash: Mapped[str] = mapped_column(
        Text, nullable=False, default="settings-fixture"
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        run_ticket_customer_scope_fk(name="fk_audit_run_ticket_customer_scope"),
        Index("ix_audit_ticket_created", "ticket_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("audit"))
    tenant_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("tenants.id"))
    ticket_id: Mapped[str | None] = mapped_column(String(64))
    customer_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column("actor_ref", String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
