"""Cross-domain metadata constraints registered after all entities load."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from supportguard.db.base import Base
from supportguard.db.entities.foundation import tenant_resource_fk

_REGISTRATION_KEY = "supportguard_cross_domain_constraints_registered"
if Base.metadata.info.get(_REGISTRATION_KEY):
    raise RuntimeError("cross-domain ORM constraints were registered more than once")
Base.metadata.info[_REGISTRATION_KEY] = True

# v1.2.4 tenant safety: every reference between tenant-owned rows is backed by the
# tenant discriminator as well as the resource id. The original scalar FKs remain
# temporarily for backward-compatible constraint names; these composite constraints
# are the authoritative cross-tenant boundary and are mirrored by the Alembic revision.
_V124_TENANT_PARENT_TABLES = (
    "billing_records",
    "api_request_traces",
    "ticket_messages",
    "proposal_records",
    "checkpoint_commit_markers",
    "agent_call_attempts",
    "turn_groups",
    "tool_invocations",
    "context_ledgers",
)
for _table_name in _V124_TENANT_PARENT_TABLES:
    Base.metadata.tables[_table_name].append_constraint(
        UniqueConstraint(
            "tenant_id",
            "id",
            name=f"uq_{_table_name}_tenant_id_id_v124",
        )
    )

_V124_TENANT_REFERENCES = (
    ("billing_records", "duplicate_of", "billing_records"),
    ("incident_impacts", "request_trace_id", "api_request_traces"),
    ("agent_runs", "message_id", "ticket_messages"),
    ("approval_requests", "proposal_id", "proposal_records"),
    ("approval_requests", "run_id", "agent_runs"),
    ("approval_requests", "marker_id", "checkpoint_commit_markers"),
    ("runtime_jobs", "approval_id", "approval_requests"),
    ("checkpoint_commit_markers", "job_id", "runtime_jobs"),
    ("agent_call_attempts", "job_id", "runtime_jobs"),
    ("raw_provider_decision_envelopes", "job_id", "runtime_jobs"),
    ("raw_provider_decision_envelopes", "provider_attempt_id", "agent_call_attempts"),
    ("policy_capability_invocations", "job_id", "runtime_jobs"),
    ("turn_groups", "run_id", "agent_runs"),
    ("turn_groups", "job_id", "runtime_jobs"),
    ("tool_invocations", "run_id", "agent_runs"),
    ("tool_invocations", "job_id", "runtime_jobs"),
    ("tool_invocations", "turn_group_id", "turn_groups"),
    ("tool_observations", "run_id", "agent_runs"),
    ("tool_observations", "job_id", "runtime_jobs"),
    ("tool_observations", "invocation_id", "tool_invocations"),
    ("finalizer_payloads", "run_id", "agent_runs"),
    ("finalizer_payloads", "job_id", "runtime_jobs"),
    ("finalizer_payloads", "marker_id", "checkpoint_commit_markers"),
    ("retrieval_traces", "run_id", "agent_runs"),
    ("retrieval_traces", "job_id", "runtime_jobs"),
    ("retrieval_traces", "logical_invocation_id", "tool_invocations"),
    ("context_ledgers", "run_id", "agent_runs"),
    ("context_ledgers", "job_id", "runtime_jobs"),
    ("context_ledgers", "provider_attempt_id", "agent_call_attempts"),
    ("claim_records", "run_id", "agent_runs"),
    ("claim_records", "job_id", "runtime_jobs"),
    ("claim_records", "provider_attempt_id", "agent_call_attempts"),
    ("claim_records", "context_ledger_id", "context_ledgers"),
)


def _v124_fk_name(child: str, local_id: str, parent: str) -> str:
    return f"fk_v124_{child[:20]}_{local_id[:16]}_{parent[:16]}"


for _child_name, _local_id, _parent_name in _V124_TENANT_REFERENCES:
    Base.metadata.tables[_child_name].append_constraint(
        tenant_resource_fk(
            _local_id,
            _parent_name,
            name=_v124_fk_name(_child_name, _local_id, _parent_name),
        )
    )


# v1.2.6 reference closure. These constraints intentionally mirror the
# b126c0a1d003 PostgreSQL catalog instead of treating ``*_id`` values as labels.
for _table_name, _columns, _name in (
    ("agent_events", ("tenant_id", "run_id", "id"), "uq_agent_events_tenant_run_id"),
    (
        "agent_events",
        ("tenant_id", "ticket_id", "run_id", "customer_id", "id"),
        "uq_agent_events_tenant_ticket_run_customer_id",
    ),
    (
        "agent_events",
        ("tenant_id", "ticket_id", "customer_id", "id"),
        "uq_agent_events_tenant_ticket_customer_id",
    ),
    ("agent_events", ("tenant_id", "id"), "uq_agent_events_tenant_id"),
    (
        "agent_runs",
        ("tenant_id", "id", "ticket_id", "customer_id"),
        "uq_agent_runs_tenant_id_ticket_customer",
    ),
    (
        "support_tickets",
        ("tenant_id", "id", "customer_id"),
        "uq_ticket_tenant_id_customer",
    ),
):
    Base.metadata.tables[_table_name].append_constraint(UniqueConstraint(*_columns, name=_name))

for _child, _local, _parent, _remote, _name in (
    (
        "agent_events",
        ("tenant_id", "run_id", "previous_event_id"),
        "agent_events",
        ("tenant_id", "run_id", "id"),
        "fk_agent_events_previous_same_run",
    ),
    (
        "ticket_summaries",
        ("tenant_id", "source_run_id", "ticket_id", "customer_id"),
        "agent_runs",
        ("tenant_id", "id", "ticket_id", "customer_id"),
        "fk_ticket_summary_source_run_domain",
    ),
    (
        "queue_delivery_audits",
        ("tenant_id",),
        "tenants",
        ("id",),
        "fk_queue_audit_tenant",
    ),
    (
        "queue_delivery_audits",
        ("tenant_id", "job_id"),
        "runtime_jobs",
        ("tenant_id", "id"),
        "fk_queue_audit_job_tenant",
    ),
    (
        "audit_events",
        ("tenant_id", "customer_id"),
        "customers",
        ("tenant_id", "id"),
        "fk_audit_customer_domain",
    ),
    (
        "audit_events",
        ("tenant_id", "ticket_id"),
        "support_tickets",
        ("tenant_id", "id"),
        "fk_audit_ticket_domain",
    ),
    (
        "audit_events",
        ("tenant_id", "run_id"),
        "agent_runs",
        ("tenant_id", "id"),
        "fk_audit_run_domain",
    ),
    (
        "audit_events",
        ("tenant_id", "ticket_id", "customer_id"),
        "support_tickets",
        ("tenant_id", "id", "customer_id"),
        "fk_audit_ticket_customer_domain",
    ),
    (
        "approval_requests",
        ("tenant_id", "ticket_id", "run_id", "customer_id", "expected_ticket_head_event_id"),
        "agent_events",
        ("tenant_id", "ticket_id", "run_id", "customer_id", "id"),
        "fk_approval_expected_event_domain",
    ),
    (
        "checkpoint_commit_markers",
        ("tenant_id", "run_id", "expected_ticket_head_event_id"),
        "agent_events",
        ("tenant_id", "run_id", "id"),
        "fk_marker_expected_event_same_run",
    ),
    (
        "business_actions",
        ("tenant_id", "ticket_id", "customer_id", "canonical_event_id"),
        "agent_events",
        ("tenant_id", "ticket_id", "customer_id", "id"),
        "fk_business_action_canonical_event_domain",
    ),
    (
        "human_decisions",
        ("tenant_id", "canonical_event_id"),
        "agent_events",
        ("tenant_id", "id"),
        "fk_human_decision_canonical_event_tenant",
    ),
):
    Base.metadata.tables[_child].append_constraint(
        ForeignKeyConstraint(
            list(_local),
            [f"{_parent}.{column}" for column in _remote],
            name=_name,
            deferrable=_name
            in {
                "fk_agent_events_previous_same_run",
                "fk_approval_expected_event_domain",
                "fk_marker_expected_event_same_run",
                "fk_business_action_canonical_event_domain",
                "fk_human_decision_canonical_event_tenant",
            },
            initially=(
                "DEFERRED"
                if _name
                in {
                    "fk_agent_events_previous_same_run",
                    "fk_approval_expected_event_domain",
                    "fk_marker_expected_event_same_run",
                    "fk_business_action_canonical_event_domain",
                    "fk_human_decision_canonical_event_tenant",
                }
                else None
            ),
            use_alter=True,
        )
    )

Base.metadata.tables["queue_delivery_audits"].append_constraint(
    CheckConstraint("job_id IS NULL OR tenant_id IS NOT NULL", name="queue_audit_job_shape")
)
Base.metadata.tables["audit_events"].append_constraint(
    CheckConstraint(
        "tenant_id IS NOT NULL OR (ticket_id IS NULL AND customer_id IS NULL AND run_id IS NULL)",
        name="audit_domain_nullable_shape",
    )
)
