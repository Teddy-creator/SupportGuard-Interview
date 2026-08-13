"""Frozen v1.2.6 database reference classifications.

The specification owns this list.  It is deliberately table-and-column specific:
unknown ``*_id`` columns fail the catalog gate instead of being inferred by name.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from supportguard.db.security_contract import (
    CURRENT_INTERVIEW_DATABASE_REVISION,
    LEGACY_FINAL_DATABASE_HEAD,
)


@dataclass(frozen=True, slots=True)
class CompositeForeignKeyContract:
    schema: str
    table: str
    name: str
    local_columns: tuple[str, ...]
    parent_schema: str
    parent_table: str
    remote_columns: tuple[str, ...]
    nullable_policy: str = "required"


@dataclass(frozen=True, slots=True)
class ReferenceSurfaceFingerprint:
    count: int
    sha256: str


# Independent v1.2.13 pg_catalog baseline. These values cover exactly the 64
# application/control tables represented by ORM metadata at migration head d083;
# extension/system objects have their own baseline below.
V1213_REFERENCE_SURFACE = {
    "tables": ReferenceSurfaceFingerprint(
        64, "940c3fdf9bb338592bd5a54191eae721ae97d42cf564e722442991c3e0d590ce"
    ),
    "columns": ReferenceSurfaceFingerprint(
        907, "5c3e0331bf244765ecf660a25189c983aab6611fb69b0e072c7961eba2409eca"
    ),
    "foreign_keys": ReferenceSurfaceFingerprint(
        310, "cbe3d9cf5ebf48594484b0fc254cda55d6741a12f30685b2de687dc211130977"
    ),
    "checks": ReferenceSurfaceFingerprint(
        59, "286f6871e1e59c6c9d86d3637bdc3f780839bb21aa0ddf3f3d1f9c31d0ad798b"
    ),
    "uniques": ReferenceSurfaceFingerprint(
        169, "6eafdd7c3474d99193cc63744cf9cbcb0532065b6ec40080cb71c5861d38e80c"
    ),
    "indexes": ReferenceSurfaceFingerprint(
        194, "7c61d7bf583af27b39aa7cd68e4e7780ca5dacc500a387012eb1dc010b76b19f"
    ),
}
V1213_EXTENSION_BASELINE = ReferenceSurfaceFingerprint(
    1, "897312ba0c2d52416799db1ecfe226bcb439900c5cb00edc23d4b191b7515c35"
)
V1213_DATABASE_CODE_SURFACE = {
    "functions": ReferenceSurfaceFingerprint(
        95, "90d30c869a1f1a971e9bc278ded7ede61dcbc3e953368ef62aac05ce04e2d07b"
    ),
    "triggers": ReferenceSurfaceFingerprint(
        104, "31bd08ec06abf52af4622a76be94b78d594eff5e6788f5a39cfb4414de498160"
    ),
    "acl": ReferenceSurfaceFingerprint(
        2277, "1aad176c43187269bd87e9190943ff92594d236bc0bffb35f3d6474c0bea1f18"
    ),
}

# Keep the v1.2.13 fingerprints above immutable.  ``b207`` remains the exact
# final identity of the archived migration chain; the independently rooted
# Interview baseline is the only schema identity accepted by current Runtime.
LEGACY_PRODUCT_DATABASE_HEAD = LEGACY_FINAL_DATABASE_HEAD
CURRENT_PRODUCT_DATABASE_HEAD = CURRENT_INTERVIEW_DATABASE_REVISION
V14_DATABASE_CODE_SURFACE = {
    **V1213_DATABASE_CODE_SURFACE,
    "functions": ReferenceSurfaceFingerprint(
        95, "ced15ba471240eed6770f4f26af7ef686162218747a632c64b80fa43238c3097"
    ),
}

# Complete current-head catalog fingerprint. Historical v1.2.13 and v1.4
# fingerprints above remain immutable evidence; current-head checks must not
# compose those older table/column fingerprints with newer function metadata.
CURRENT_DATABASE_SURFACE = {
    "tables": ReferenceSurfaceFingerprint(
        66, "d3c4df318d8863f3710dfed7d5c1142956695fac21ef228ff608f93c09cf99fd"
    ),
    "columns": ReferenceSurfaceFingerprint(
        952, "b322ef28e2d68f32dfe86511fe2677f4ca7591e714eb23d90714d4fba5b3ff53"
    ),
    "foreign_keys": ReferenceSurfaceFingerprint(
        327, "0ff5b7004e1399949aa0ddfe91c1a936f0aab8e651a961c392ceef366a6955cd"
    ),
    "checks": ReferenceSurfaceFingerprint(
        65, "fdab2c0bcca55eadc0ece21d731563f1590e129fa4e69f107dfe9612c730c619"
    ),
    "uniques": ReferenceSurfaceFingerprint(
        184, "2153cf586a8b3ad29472020e7981e41d19fd5e2d0611365c4f295745ef91e3cc"
    ),
    "indexes": ReferenceSurfaceFingerprint(
        219, "4d6631b3a2358f6e175b0026c882fb913c89e960c00a16b7fba3d9cc18a5c574"
    ),
    "functions": ReferenceSurfaceFingerprint(
        129, "07afb6b85b21d993ddf8da76d3536185620259bcb26a87c0d4fe33991a7b26f0"
    ),
    "triggers": ReferenceSurfaceFingerprint(
        111, "4cf2701649684d396f52f58566ccba6883e434e33decf6102a79b5921e59f86f"
    ),
    "acl": ReferenceSurfaceFingerprint(
        2513, "6bc78de5adc338b48c1d329873310378ed8583348b09d8e9045278ee4fb452ed"
    ),
}


@dataclass(frozen=True, slots=True)
class ReferenceViolation:
    constraint_name: str
    table: str
    violation_count: int
    samples: tuple[tuple[str | None, ...], ...]


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def find_v1213_reference_violations(
    connection: AsyncConnection,
) -> tuple[ReferenceViolation, ...]:
    """Read-only d077 preflight with bounded, exact local-key samples."""

    violations: list[ReferenceViolation] = []
    for contract in V1213_FROZEN_REFERENCE_FKS:
        child_name = f"{contract.schema}.{contract.table}"
        parent_name = f"{contract.parent_schema}.{contract.parent_table}"
        if await connection.scalar(text("SELECT to_regclass(:name)"), {"name": child_name}) is None:
            continue
        local_select = ",".join(f"c.{_quoted(column)}::text" for column in contract.local_columns)
        present = " AND ".join(
            f"c.{_quoted(column)} IS NOT NULL" for column in contract.local_columns
        )
        join = " AND ".join(
            f"p.{_quoted(remote)}=c.{_quoted(local)}"
            for local, remote in zip(contract.local_columns, contract.remote_columns, strict=True)
        )
        query = text(
            f"SELECT count(*) OVER(),{local_select} "  # noqa: S608  # nosec B608
            f"FROM {_quoted(contract.schema)}.{_quoted(contract.table)} c "
            f"WHERE {present} AND NOT EXISTS (SELECT 1 FROM "
            f"{_quoted(contract.parent_schema)}.{_quoted(contract.parent_table)} p "
            f"WHERE {join}) ORDER BY c.ctid LIMIT 10"
        )
        rows = (await connection.execute(query)).all()
        if rows:
            violations.append(
                ReferenceViolation(
                    constraint_name=contract.name,
                    table=child_name,
                    violation_count=int(rows[0][0]),
                    samples=tuple(
                        tuple(None if value is None else str(value) for value in row[1:])
                        for row in rows
                    ),
                )
            )
        if (
            await connection.scalar(text("SELECT to_regclass(:name)"), {"name": parent_name})
            is None
        ):
            raise RuntimeError(f"v1213_reference_parent_missing:{parent_name}")
    return tuple(violations)


_RUNTIME_JOB_SCOPE = (
    ("agent_call_attempts", "fk_agent_attempt_runtime_job_scope"),
    ("checkpoint_commit_markers", "fk_marker_runtime_job_scope"),
    ("claim_records", "fk_claim_record_runtime_job_scope"),
    ("context_ledgers", "fk_context_ledger_runtime_job_scope"),
    ("finalizer_payloads", "fk_finalizer_payload_runtime_job_scope"),
    ("outbox_events", "fk_outbox_event_runtime_job_scope"),
    ("policy_capability_attempts", "fk_policy_attempt_runtime_job_scope"),
    ("policy_capability_invocations", "fk_policy_invocation_runtime_job_scope"),
    ("policy_capability_results", "fk_policy_result_runtime_job_scope"),
    ("raw_provider_decision_envelopes", "fk_raw_decision_runtime_job_scope"),
    ("redis_delivery_observations", "fk_redis_observation_runtime_job_scope"),
    ("retrieval_traces", "fk_retrieval_trace_runtime_job_scope"),
    ("tool_invocations", "fk_tool_invocation_runtime_job_scope"),
    ("tool_observations", "fk_tool_observation_runtime_job_scope"),
    ("tool_transport_attempts", "fk_transport_attempt_runtime_job_scope"),
    ("turn_groups", "fk_turn_group_runtime_job_scope"),
)
_RUN_SCOPE = (
    ("approval_snapshots", "fk_approval_snapshot_run_scope"),
    ("citation_bindings", "fk_citation_binding_run_scope"),
    ("context_memberships", "fk_context_membership_run_scope"),
    ("reconcile_intents", "fk_reconcile_intent_run_scope"),
    ("redis_delivery_observations", "fk_redis_observation_run_scope"),
    ("retention_trim_intents", "fk_retention_trim_intent_run_scope"),
)
_TICKET_CUSTOMER_SCOPE = (
    ("agent_runs", "fk_agent_run_ticket_customer_scope"),
    ("agent_events", "fk_agent_event_ticket_customer_scope"),
    ("approval_requests", "fk_approval_ticket_customer_scope"),
    ("approval_snapshots", "fk_approval_snapshot_ticket_customer_scope"),
    ("business_actions", "fk_business_action_ticket_customer_scope"),
    ("escalation_records", "fk_escalation_ticket_customer_scope"),
    ("ticket_summaries", "fk_ticket_summary_ticket_customer_scope"),
)
_RUN_DOMAIN_SCOPE = (
    ("agent_events", "fk_agent_event_run_domain_scope"),
    ("approval_requests", "fk_approval_run_domain_scope"),
    ("approval_snapshots", "fk_approval_snapshot_run_domain_scope"),
    ("audit_events", "fk_audit_run_ticket_customer_scope"),
)


V1213_FROZEN_REFERENCE_FKS = (
    CompositeForeignKeyContract(
        "supportguard_control",
        "writer_barrier_receipts",
        "fk_writer_barrier_receipt_tenant",
        ("tenant_id",),
        "public",
        "tenants",
        ("id",),
        "writer_all_or_none",
    ),
    CompositeForeignKeyContract(
        "supportguard_control",
        "writer_barrier_receipts",
        "fk_writer_barrier_receipt_run",
        ("tenant_id", "drain_run_id"),
        "public",
        "agent_runs",
        ("tenant_id", "id"),
        "writer_all_or_none",
    ),
    CompositeForeignKeyContract(
        "supportguard_control",
        "writer_barrier_receipts",
        "fk_writer_barrier_receipt_job",
        ("tenant_id", "drain_run_id", "drain_job_id"),
        "public",
        "runtime_jobs",
        ("tenant_id", "run_id", "id"),
        "writer_all_or_none",
    ),
    *(
        CompositeForeignKeyContract(
            "public",
            table,
            name,
            ("tenant_id", "run_id", "job_id"),
            "public",
            "runtime_jobs",
            ("tenant_id", "run_id", "id"),
            "origin_discriminated" if table == "retrieval_traces" else "required",
        )
        for table, name in _RUNTIME_JOB_SCOPE
    ),
    *(
        CompositeForeignKeyContract(
            "public",
            table,
            name,
            ("tenant_id", "run_id"),
            "public",
            "agent_runs",
            ("tenant_id", "id"),
        )
        for table, name in _RUN_SCOPE
    ),
    *(
        CompositeForeignKeyContract(
            "public",
            table,
            name,
            ("tenant_id", "ticket_id", "customer_id"),
            "public",
            "support_tickets",
            ("tenant_id", "id", "customer_id"),
        )
        for table, name in _TICKET_CUSTOMER_SCOPE
    ),
    *(
        CompositeForeignKeyContract(
            "public",
            table,
            name,
            ("tenant_id", "run_id", "ticket_id", "customer_id"),
            "public",
            "agent_runs",
            ("tenant_id", "id", "ticket_id", "customer_id"),
            (
                "optional_run"
                if table == "approval_requests"
                else "audit_domain"
                if table == "audit_events"
                else "required"
            ),
        )
        for table, name in _RUN_DOMAIN_SCOPE
    ),
)

# v1.5.12 adds current-head aggregate bindings without rewriting the immutable
# v1.2.13 denominator above.  Current-head reference gates consume the combined
# graph; historical d077 tests continue to consume exactly the frozen 36.
V1512_REFERENCE_FKS = (
    CompositeForeignKeyContract(
        "public",
        "approval_requests",
        "fk_approval_origin_turn_scope",
        ("tenant_id", "origin_turn_id", "ticket_id", "run_id"),
        "public",
        "conversation_turns",
        ("tenant_id", "id", "ticket_id", "run_id"),
    ),
    CompositeForeignKeyContract(
        "public",
        "runtime_jobs",
        "fk_runtime_jobs_tenant_support_tickets",
        ("tenant_id", "ticket_id"),
        "public",
        "support_tickets",
        ("tenant_id", "id"),
    ),
    CompositeForeignKeyContract(
        "public",
        "runtime_jobs",
        "fk_runtime_jobs_run_ticket_scope",
        ("tenant_id", "run_id", "ticket_id"),
        "public",
        "agent_runs",
        ("tenant_id", "id", "ticket_id"),
    ),
    CompositeForeignKeyContract(
        "public",
        "runtime_jobs",
        "fk_runtime_jobs_approval_resume_scope",
        ("tenant_id", "approval_id", "run_id", "ticket_id"),
        "public",
        "approval_requests",
        ("tenant_id", "id", "run_id", "ticket_id"),
        "optional_approval",
    ),
)

CURRENT_REFERENCE_FKS = (*V1213_FROZEN_REFERENCE_FKS, *V1512_REFERENCE_FKS)

EXTERNAL_ID_ALLOWLIST = frozenset(
    {
        "agent_events.causation_id",
        "agent_events.correlation_id",
        "agent_events.idempotency_id",
        "agent_events.tool_call_id",
        "agent_runs.canonical_checkpoint_id",
        "agent_runs.checkpoint_id",
        "api_key_metadata.key_id",
        "api_request_traces.request_id",
        "approval_requests.approver_id",
        "approval_requests.checkpoint_id",
        "approval_requests.resource_id",
        "audit_events.trace_id",
        "business_actions.resource_id",
        "checkpoint_commit_markers.canonical_parent_id",
        "checkpoint_commit_markers.final_checkpoint_id",
        "proposal_withdrawals.actor_id",
        "human_decisions.actor_id",
        "idempotency_requests.principal_id",
        "inbox_deliveries.delivery_id",
        "inbox_deliveries.redis_message_id",
        "outbox_events.delivery_id",
        "outbox_events.redis_message_id",
        "policy_capability_invocations.segment_id",
        "proposal_records.resource_id",
        "provider_runtime_events.service_instance_id",
        "queue_delivery_audits.delivery_id",
        "queue_delivery_audits.redis_message_id",
        "raw_provider_decision_envelopes.segment_id",
        "retrieval_traces.segment_id",
        "retrieval_traces.tool_call_id",
        "tool_invocations.logical_invocation_id",
        "tool_invocations.provider_tool_call_id",
        "tool_invocations.segment_id",
        "tool_observations.segment_id",
        "turn_groups.segment_id",
    }
)
