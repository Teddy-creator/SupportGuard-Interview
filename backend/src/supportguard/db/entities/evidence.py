"""Evidence domain ORM entities."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
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
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supportguard.db.base import Base, TimestampMixin
from supportguard.db.entities.foundation import (
    new_id,
    runtime_job_scope_fk,
    tenant_resource_fk,
    tenant_run_scope_fk,
)


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_customer_tenant"),
        UniqueConstraint("tenant_id", "email", name="uq_customer_tenant_email"),
        UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    security_status: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("customer_id", name="uq_subscriptions_customer_id"),
        CheckConstraint("balance >= 0", name="balance_non_negative"),
        tenant_resource_fk("customer_id", "customers", name="fk_subscriptions_tenant_customers"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    plan: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    rpm_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    customer: Mapped[Customer] = relationship(foreign_keys=[customer_id])


class ApiUsageSnapshot(TimestampMixin, Base):
    __tablename__ = "api_usage_snapshots"
    __table_args__ = (
        Index("ix_api_usage_customer_observed", "customer_id", "observed_at"),
        CheckConstraint("concurrency_current >= 0", name="concurrency_non_negative"),
        tenant_resource_fk(
            "customer_id", "customers", name="fk_api_usage_snapshots_tenant_customers"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requests_last_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_current: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class ApiUsageBucket(TimestampMixin, Base):
    __tablename__ = "api_usage_buckets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "customer_id", "bucket_start", name="uq_api_usage_bucket_scope"
        ),
        CheckConstraint(
            "request_count >= 0 AND input_token_count >= 0 AND output_token_count >= 0 "
            "AND concurrency_peak >= 0 AND concurrency_end >= 0 AND source_version >= 1",
            name="api_usage_bucket_values_valid",
        ),
        tenant_resource_fk(
            "customer_id", "customers", name="fk_api_usage_buckets_tenant_customers"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("usage"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_peak: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_end: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)


class BillingRecord(TimestampMixin, Base):
    __tablename__ = "billing_records"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_billing_customer_created", "customer_id", "created_at"),
        tenant_resource_fk("customer_id", "customers", name="fk_billing_records_tenant_customers"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duplicate_of: Mapped[str | None] = mapped_column(String(64), ForeignKey("billing_records.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ServiceIncident(TimestampMixin, Base):
    __tablename__ = "service_incidents"
    __table_args__ = (Index("ix_incident_model_region", "model", "region", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiRequestTrace(TimestampMixin, Base):
    __tablename__ = "api_request_traces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_trace_tenant_request"),
        tenant_resource_fk(
            "customer_id", "customers", name="fk_api_request_traces_tenant_customers"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("trace"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    stage_latency_ms: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ApiKeyMetadata(TimestampMixin, Base):
    __tablename__ = "api_key_metadata"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key_id", name="uq_api_key_tenant_key"),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_api_key_tenant_fingerprint"),
        tenant_resource_fk("customer_id", "customers", name="fk_api_key_metadata_tenant_customers"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("keymeta"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_used_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PlanCatalog(TimestampMixin, Base):
    __tablename__ = "plan_catalog"
    __table_args__ = (
        UniqueConstraint("plan", "region", "version", name="uq_catalog_plan_region_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("catalog"))
    plan: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    min_rpm: Mapped[int] = mapped_column(Integer, nullable=False)
    max_rpm: Mapped[int] = mapped_column(Integer, nullable=False)
    min_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class IncidentImpact(TimestampMixin, Base):
    __tablename__ = "incident_impacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_trace_id", "incident_id", name="uq_incident_impact"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("impact"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    request_trace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("api_request_traces.id"), nullable=False
    )
    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("service_incidents.id"), nullable=False
    )
    impacted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    public_incident_ref: Mapped[str] = mapped_column(String(200), nullable=False)


class KnowledgeIngestRun(TimestampMixin, Base):
    __tablename__ = "knowledge_ingest_runs"
    __table_args__ = (
        Index(
            "uq_knowledge_single_active_snapshot",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
        UniqueConstraint("id", "index_version", name="uq_knowledge_ingest_id_index"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ingest"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pipeline_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pipeline_fingerprint: Mapped[str | None] = mapped_column(String(64))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "document_key", "version", "index_version", name="uq_document_version_index"
        ),
        UniqueConstraint(
            "id", "index_version", "ingest_run_id", name="uq_knowledge_document_snapshot"
        ),
        ForeignKeyConstraint(
            ["ingest_run_id", "index_version"],
            ["knowledge_ingest_runs.id", "knowledge_ingest_runs.index_version"],
            name="fk_knowledge_document_snapshot",
        ),
        Index("ix_document_active_filter", "status", "effective_from", "index_version"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="knowledge_document_effective_interval_nonempty",
        ),
        CheckConstraint(
            "effective_at = effective_from",
            name="knowledge_document_effective_alias_exact",
        ),
        Index(
            "ix_knowledge_document_temporal_family",
            "index_version",
            "document_family_key",
            "applicability_scope_hash",
            "effective_from",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("doc"))
    ingest_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    document_family_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applicability_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_level: Mapped[int] = mapped_column(Integer, nullable=False)
    applicable_plan: Mapped[str | None] = mapped_column(String(64))
    applicable_region: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="utf8-lf-nfc.v1"
    )


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_key", "index_version", name="uq_chunk_document_index"
        ),
        Index("ix_chunk_document_sequence", "document_id", "sequence"),
        UniqueConstraint("locator_hash", "index_version", name="uq_knowledge_chunk_locator_index"),
        ForeignKeyConstraint(
            ["document_id", "index_version", "ingest_run_id"],
            [
                "knowledge_documents.id",
                "knowledge_documents.index_version",
                "knowledge_documents.ingest_run_id",
            ],
            name="fk_knowledge_chunk_document_snapshot",
        ),
        ForeignKeyConstraint(
            ["ingest_run_id", "index_version"],
            ["knowledge_ingest_runs.id", "knowledge_ingest_runs.index_version"],
            name="fk_knowledge_chunk_ingest_snapshot",
        ),
        Index(
            "ix_knowledge_chunk_search_vector_gin",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("chunk"))
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ingest_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(128), nullable=False)
    section_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="source-locator.v2"
    )
    canonicalization_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="utf8-lf-nfc.v1"
    )
    chunker_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384).with_variant(JSON, "sqlite"))
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR().with_variant(Text, "sqlite"))


class RetrievalTrace(TimestampMixin, Base):
    __tablename__ = "retrieval_traces"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_retrieval_trace_runtime_job_scope"),
        CheckConstraint(
            "(origin_kind = 'agent_read_tool' AND run_id IS NOT NULL AND job_id IS NOT NULL "
            "AND segment_id IS NOT NULL AND logical_invocation_id IS NOT NULL "
            "AND tool_call_id IS NOT NULL AND fencing_token IS NOT NULL "
            "AND delivery_generation IS NOT NULL) OR "
            "(origin_kind = 'legacy_agent' AND run_id IS NOT NULL AND job_id IS NOT NULL "
            "AND segment_id IS NOT NULL AND logical_invocation_id IS NULL "
            "AND tool_call_id IS NULL AND fencing_token IS NULL "
            "AND delivery_generation IS NULL) OR "
            "(origin_kind IN ('offline','maintenance','future_eval') "
            "AND job_id IS NULL AND segment_id IS NULL AND logical_invocation_id IS NULL "
            "AND tool_call_id IS NULL AND fencing_token IS NULL "
            "AND delivery_generation IS NULL)",
            name="retrieval_trace_origin_binding_v124",
        ),
        UniqueConstraint(
            "logical_invocation_id", name="uq_retrieval_trace_logical_invocation_v126"
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "origin_job_id",
                "origin_marker_id",
                "origin_fencing_token",
            ],
            [
                "checkpoint_commit_markers.tenant_id",
                "checkpoint_commit_markers.run_id",
                "checkpoint_commit_markers.job_id",
                "checkpoint_commit_markers.id",
                "checkpoint_commit_markers.fencing_token",
            ],
            name="fk_retrieval_trace_origin_marker_v126",
        ),
        CheckConstraint(
            "(origin_kind <> 'agent_read_tool') OR "
            "(origin_job_id IS NOT NULL AND origin_marker_id IS NOT NULL AND "
            "origin_fencing_token IS NOT NULL AND origin_segment_ref IS NOT NULL)",
            name="retrieval_trace_origin_lineage_v126",
        ),
        CheckConstraint(
            "trace_status IN ('started','terminal_ok','terminal_error')",
            name="retrieval_trace_status_v126",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "origin_job_id",
            "logical_invocation_id",
            "id",
            name="uq_retrieval_trace_origin_v126",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("retrieval")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.id"))
    job_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("runtime_jobs.id"))
    segment_id: Mapped[str | None] = mapped_column(String(64))
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="agent_read_tool")
    logical_invocation_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("tool_invocations.id")
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    delivery_generation: Mapped[int | None] = mapped_column(Integer)
    origin_job_id: Mapped[str | None] = mapped_column(String(64))
    origin_marker_id: Mapped[str | None] = mapped_column(String(64))
    origin_fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    origin_segment_ref: Mapped[str | None] = mapped_column(String(64))
    terminal_transport_attempt_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("tool_transport_attempts.id")
    )
    trace_status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    error_digest: Mapped[str | None] = mapped_column(String(64))
    trace_logical_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_selector: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filter_contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    vector_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    keyword_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    rrf_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    pre_filter_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    selected_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    omission_decisions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    evidence_groups: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    eligibility_envelopes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    pipeline_contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    embedding_fingerprint: Mapped[str | None] = mapped_column(String(64))
    trace_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="retrieval-trace.v3"
    )
    pipeline_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_ingest_runs.id"), nullable=False
    )
    abstention_reason: Mapped[str | None] = mapped_column(String(128))
    runtime_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    __mapper_args__ = {"version_id_col": status_version}


class ContextLedger(TimestampMixin, Base):
    __tablename__ = "context_ledgers"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_context_ledger_runtime_job_scope"),
        UniqueConstraint("provider_attempt_id", name="uq_context_ledgers_provider_attempt_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "job_id",
            "provider_attempt_id",
            "id",
            name="uq_context_ledger_executor_v126",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("context"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    provider_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    serializer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_request_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    request_storage_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="hash_only"
    )
    sensitivity_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    component_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    token_preflight: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    runtime_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ContextMembership(TimestampMixin, Base):
    __tablename__ = "context_memberships"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_context_membership_run_scope"),
        UniqueConstraint(
            "context_ledger_id", "payload_ordinal", name="uq_context_membership_payload_ordinal"
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "origin_job_id",
                "origin_marker_id",
                "origin_fencing_token",
            ],
            [
                "checkpoint_commit_markers.tenant_id",
                "checkpoint_commit_markers.run_id",
                "checkpoint_commit_markers.job_id",
                "checkpoint_commit_markers.id",
                "checkpoint_commit_markers.fencing_token",
            ],
            name="fk_context_membership_origin_marker",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "executor_job_id",
                "executor_marker_id",
                "executor_fencing_token",
            ],
            [
                "checkpoint_commit_markers.tenant_id",
                "checkpoint_commit_markers.run_id",
                "checkpoint_commit_markers.job_id",
                "checkpoint_commit_markers.id",
                "checkpoint_commit_markers.fencing_token",
            ],
            name="fk_context_membership_executor_marker",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "executor_job_id", "provider_attempt_id"],
            [
                "agent_call_attempts.tenant_id",
                "agent_call_attempts.run_id",
                "agent_call_attempts.job_id",
                "agent_call_attempts.id",
            ],
            name="fk_context_membership_provider_executor",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "executor_job_id",
                "provider_attempt_id",
                "context_ledger_id",
            ],
            [
                "context_ledgers.tenant_id",
                "context_ledgers.run_id",
                "context_ledgers.job_id",
                "context_ledgers.provider_attempt_id",
                "context_ledgers.id",
            ],
            name="fk_context_membership_ledger_executor",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "origin_job_id",
            "id",
            "provider_attempt_id",
            "context_ledger_id",
            name="uq_context_membership_binding_parent",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cmem"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    origin_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_marker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_segment_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tool_invocations.id"), nullable=False
    )
    executor_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_marker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    context_ledger_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("context_ledgers.id"), nullable=False
    )
    payload_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json_pointer: Mapped[str] = mapped_column(String(500), nullable=False)
    serialized_evidence_fragment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_membership_root_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="context-membership.v1"
    )


class CitationBinding(TimestampMixin, Base):
    __tablename__ = "citation_bindings"
    __table_args__ = (
        tenant_run_scope_fk(name="fk_citation_binding_run_scope"),
        UniqueConstraint("membership_id", name="uq_citation_binding_membership"),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "origin_job_id",
                "membership_id",
                "provider_attempt_id",
                "context_ledger_id",
            ],
            [
                "context_memberships.tenant_id",
                "context_memberships.run_id",
                "context_memberships.origin_job_id",
                "context_memberships.id",
                "context_memberships.provider_attempt_id",
                "context_memberships.context_ledger_id",
            ],
            name="fk_citation_binding_membership_executor",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "origin_job_id", "tool_invocation_id"],
            [
                "tool_invocations.tenant_id",
                "tool_invocations.run_id",
                "tool_invocations.job_id",
                "tool_invocations.id",
            ],
            name="fk_citation_binding_invocation_origin",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "origin_job_id",
                "tool_invocation_id",
                "observation_id",
            ],
            [
                "tool_observations.tenant_id",
                "tool_observations.run_id",
                "tool_observations.job_id",
                "tool_observations.invocation_id",
                "tool_observations.id",
            ],
            name="fk_citation_binding_observation_origin",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "run_id",
                "origin_job_id",
                "tool_invocation_id",
                "retrieval_trace_id",
            ],
            [
                "retrieval_traces.tenant_id",
                "retrieval_traces.run_id",
                "retrieval_traces.origin_job_id",
                "retrieval_traces.logical_invocation_id",
                "retrieval_traces.id",
            ],
            name="fk_citation_binding_trace_origin",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("citation")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    origin_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("context_memberships.id"), nullable=False
    )
    observation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tool_observations.id"), nullable=False
    )
    tool_invocation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tool_invocations.id"), nullable=False
    )
    retrieval_trace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("retrieval_traces.id"), nullable=False
    )
    provider_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    context_ledger_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("context_ledgers.id"), nullable=False
    )
    selected_candidate_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_selector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="citation-binding.v1"
    )


class ClaimRecord(TimestampMixin, Base):
    __tablename__ = "claim_records"
    __table_args__ = (
        runtime_job_scope_fk(name="fk_claim_record_runtime_job_scope"),
        UniqueConstraint("run_id", "claim_hash", name="uq_claim_record_run_hash"),
        CheckConstraint("status = 'validated'", name="claim_record_status_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("claim"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("runtime_jobs.id"), nullable=False)
    provider_attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_call_attempts.id"), nullable=False
    )
    context_ledger_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("context_ledgers.id"), nullable=False
    )
    claim_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    answer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    support_refs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="validated")
