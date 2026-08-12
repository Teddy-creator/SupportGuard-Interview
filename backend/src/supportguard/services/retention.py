from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.config import Settings
from supportguard.db.models import (
    ApiRequestTrace,
    AuditEvent,
    InboxDelivery,
    OutboxEvent,
    RuntimeJob,
)

PROTECTED = "protected_no_auto_delete"
ALLOWLISTED = "delete_allowlisted"

# This is intentionally exhaustive. A schema change without a manifest change fails closed.
RETENTION_MANIFEST: dict[str, str] = {
    name: PROTECTED
    for name in [
        "agent_call_attempts",
        "agent_events",
        "agent_runs",
        "api_key_metadata",
        "api_usage_buckets",
        "api_usage_snapshots",
        "approval_requests",
        "approval_snapshots",
        "approval_action_revisions",
        "approver_tenant_scopes",
        "audit_events",
        "billing_records",
        "business_actions",
        "checkpoint_commit_markers",
        "checkpoint_migrations",
        "checkpoint_task_identities",
        "checkpoint_thread_identities",
        "checkpoint_value_identities",
        "claim_records",
        "citation_bindings",
        "conversation_turns",
        "context_ledgers",
        "context_memberships",
        "customers",
        "escalation_records",
        "finalizer_payloads",
        "human_decisions",
        "incident_impacts",
        "memberships",
        "mutation_kill_switches",
        "plan_catalog",
        "policy_capability_attempts",
        "policy_capability_invocations",
        "policy_capability_results",
        "proposal_records",
        "proposal_withdrawals",
        "provider_runtime_events",
        "queue_delivery_audits",
        "raw_provider_decision_envelopes",
        "retrieval_traces",
        "runtime_jobs",
        "reconcile_intents",
        "redis_delivery_observations",
        "retention_trim_intents",
        "retention_trim_receipts",
        "service_incidents",
        "subscriptions",
        "support_tickets",
        "tenants",
        "ticket_messages",
        "ticket_summaries",
        "tool_invocations",
        "tool_observations",
        "tool_transport_attempts",
        "turn_groups",
        "users",
        "supportguard_control.database_identity",
        "supportguard_control.runtime_timing_snapshots",
        "supportguard_control.upgrade_attestations",
        "supportguard_control.upgrade_phase_events",
        "supportguard_control.upgrade_runs",
        "supportguard_control.writer_barrier_receipts",
    ]
}
RETENTION_MANIFEST.update(
    {
        name: ALLOWLISTED
        for name in [
            "api_request_traces",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoints",
            "idempotency_requests",
            "inbox_deliveries",
            "knowledge_chunks",
            "knowledge_documents",
            "knowledge_ingest_runs",
            "outbox_events",
            "service_instance_heartbeats",
        ]
    }
)
RETENTION_CLASSES = tuple(sorted(RETENTION_MANIFEST))


def validate_retention_schema(table_names: set[str]) -> None:
    if table_names != set(RETENTION_MANIFEST):
        missing = sorted(table_names - set(RETENTION_MANIFEST))
        stale = sorted(set(RETENTION_MANIFEST) - table_names)
        raise RuntimeError(f"retention_schema_unclassified={missing};stale={stale}")


@dataclass(frozen=True)
class RetentionReport:
    mode: str
    generated_at: datetime
    eligible: dict[str, int]
    deleted: dict[str, int]
    skipped: dict[str, int]
    blocking_dependencies: dict[str, str]
    protected: dict[str, int]


class RetentionService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def run(self, *, apply: bool) -> RetentionReport:
        now = datetime.now(UTC)
        delivery_cutoff = now - timedelta(days=self.settings.retention_delivery_days)
        trace_cutoff = now - timedelta(days=self.settings.retention_trace_days)
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            del delivery_cutoff, trace_cutoff
            plan = await self.session.scalar(
                text("SELECT supportguard_maintenance_plan_pg_retention()")
            )
            if not isinstance(plan, dict):
                raise RuntimeError("retention plan capability returned an invalid report")
            applied: dict[str, object] | None = None
            if apply:
                applied = await self.session.scalar(
                    text("SELECT supportguard_maintenance_apply_pg_retention(:plan_id)"),
                    {"plan_id": str(plan["plan_id"])},
                )
                if not isinstance(applied, dict):
                    raise RuntimeError("retention apply capability returned an invalid report")
            pg_eligible = {name: 0 for name in RETENTION_CLASSES}
            pg_deleted = {name: 0 for name in RETENTION_CLASSES}
            pg_skipped = {name: 0 for name in RETENTION_CLASSES}
            pg_protected = {name: 0 for name in RETENTION_CLASSES}
            plan_eligible = plan.get("eligible")
            if not isinstance(plan_eligible, dict):
                raise RuntimeError("retention plan omitted eligible counts")
            raw_eligible = {str(key): int(value) for key, value in plan_eligible.items()}
            for name in ("service_instance_heartbeats", "api_request_traces"):
                pg_eligible[name] = raw_eligible[name]
            for name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                pg_eligible[name] = raw_eligible["checkpoint_namespaces"]
            for name in (
                "knowledge_chunks",
                "knowledge_documents",
                "knowledge_ingest_runs",
            ):
                pg_eligible[name] = raw_eligible["knowledge_ingest_runs"]
            pg_eligible["idempotency_requests"] = raw_eligible["idempotency_requests"]
            if applied is not None:
                applied_deleted = applied.get("deleted")
                if not isinstance(applied_deleted, dict):
                    raise RuntimeError("retention apply omitted deleted counts")
                for key, value in applied_deleted.items():
                    pg_deleted[str(key)] = int(value)
            return RetentionReport(
                mode="apply" if apply else "dry-run",
                generated_at=now,
                eligible=pg_eligible,
                deleted=pg_deleted,
                skipped=pg_skipped,
                blocking_dependencies={
                    "outbox_events": "cross_store_trim_protocol_required",
                    "inbox_deliveries": "cross_store_trim_protocol_required",
                },
                protected=pg_protected,
            )
        terminal_jobs = select(RuntimeJob.id).where(RuntimeJob.status.in_({"succeeded", "dead"}))
        predicates = {
            "outbox_events": (
                OutboxEvent.created_at < delivery_cutoff,
                OutboxEvent.job_id.in_(terminal_jobs),
                OutboxEvent.published_at.is_not(None),
            ),
            "inbox_deliveries": (
                InboxDelivery.created_at < delivery_cutoff,
                InboxDelivery.job_id.in_(terminal_jobs),
                InboxDelivery.status == "acked",
            ),
            "api_request_traces": (ApiRequestTrace.created_at < trace_cutoff,),
        }
        models = {
            "outbox_events": OutboxEvent,
            "inbox_deliveries": InboxDelivery,
            "api_request_traces": ApiRequestTrace,
        }
        eligible: dict[str, int] = {name: 0 for name in RETENTION_CLASSES}
        deleted: dict[str, int] = {name: 0 for name in RETENTION_CLASSES}
        skipped: dict[str, int] = {name: 0 for name in RETENTION_CLASSES}
        protected: dict[str, int] = {name: 0 for name in RETENTION_CLASSES}
        for name, conditions in predicates.items():
            model = models[name]
            eligible[name] = int(
                await self.session.scalar(
                    select(func.count()).select_from(model).where(*conditions)
                )
                or 0
            )
            if apply and eligible[name]:
                await self.session.execute(delete(model).where(*conditions))
                deleted[name] = eligible[name]
            else:
                deleted[name] = 0
        report = RetentionReport(
            mode="apply" if apply else "dry-run",
            generated_at=now,
            eligible=eligible,
            deleted=deleted,
            skipped=skipped,
            blocking_dependencies={
                name: "dependency_policy_not_satisfied"
                for name, classification in RETENTION_MANIFEST.items()
                if classification == ALLOWLISTED and name not in predicates
            },
            protected=protected,
        )
        self.session.add(
            AuditEvent(
                event_type="retention_run",
                actor_type="maintenance",
                actor_id="supportguard-maintenance",
                payload={
                    "mode": report.mode,
                    "eligible": report.eligible,
                    "deleted": report.deleted,
                    "skipped": report.skipped,
                    "blocking_dependencies": report.blocking_dependencies,
                    "protected": report.protected,
                    "classification": RETENTION_MANIFEST,
                    "preserved": {
                        "agent_events": "full_ticket_hash_chain",
                    },
                },
                trace_id=f"retention:{now.isoformat()}",
            )
        )
        await self.session.flush()
        return report
