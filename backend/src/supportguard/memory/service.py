from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.graph import AgentState
from supportguard.contracts.context import worker_execution_context
from supportguard.contracts.freshness import ACCOUNT_SUBSCRIPTION_FRESHNESS
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    AuditEvent,
    KnowledgeDocument,
    KnowledgeIngestRun,
    SupportTicket,
    TicketSummary,
    TurnGroup,
)
from supportguard.db.session import ScopedSessionFactory
from supportguard.services.conversation_action_state import (
    ConversationActionStateProjector,
    ConversationActionStateV1,
    conversation_action_sources_from_mapping,
    project_conversation_action_state,
)
from supportguard.services.runtime_jobs import RuntimeConflict

TERMINAL_STATES = {"resolved", "rejected", "manual_takeover", "failed"}
ACTIVE_ACTION_PROJECTION_STATES = {
    "pending",
    "approved",
    "executing",
    "verification_pending",
}
MAX_ATTEMPTED_ACTIONS = 64
FRESHNESS: dict[str, tuple[str, timedelta]] = {
    "query_api_usage": ("api_usage_5m", timedelta(minutes=5)),
    "check_service_status": ("service_incident_2m", timedelta(minutes=2)),
    "query_account": (
        ACCOUNT_SUBSCRIPTION_FRESHNESS.policy,
        ACCOUNT_SUBSCRIPTION_FRESHNESS.lifetime,
    ),
    "query_subscription": (
        ACCOUNT_SUBSCRIPTION_FRESHNESS.policy,
        ACCOUNT_SUBSCRIPTION_FRESHNESS.lifetime,
    ),
    "query_billing_record": ("billing_observation_only", timedelta(seconds=0)),
}
RETRYABLE_MEMORY_REASONS = {
    "provider_failed",
    "tool_failed",
    "proposal_not_durable",
    "logical_degradation",
    "time_budget_exhausted",
}


def _as_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = fallback
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def follow_up_questions(
    *,
    terminal_state: str,
    finish_reason: str | None,
    automation_mode: str,
) -> list[str]:
    if automation_mode == "human_queue" and finish_reason == "manual_takeover":
        return ["durable_human_queue"]
    if terminal_state == "needs_clarification" or finish_reason == "needs_clarification":
        return ["clarification"]
    if finish_reason in RETRYABLE_MEMORY_REASONS:
        return ["retryable_failure"]
    return []


def merge_attempted_actions(
    existing: list[dict[str, Any]],
    current_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge historical action snapshots by canonical resource identity.

    ``TicketSummary`` is historical compression, not query-time business
    truth.  The snapshots retained here are therefore explicitly
    non-authoritative and can only help preserve conversational references.
    A fresh ``ConversationActionStateV1`` is still required before any answer
    or action decision.
    """

    legacy_present = False
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in existing:
        identity = item.get("action_identity")
        if (
            item.get("schema_version") != "attempted-action-memory.v1"
            or not isinstance(identity, dict)
            or not all(
                isinstance(identity.get(key), str) and identity[key]
                for key in ("action_type", "resource_type", "resource_id")
            )
        ):
            legacy_present = True
            continue
        key = (
            str(identity["action_type"]),
            str(identity["resource_type"]),
            str(identity["resource_id"]),
        )
        merged[key] = dict(item)

    for payload in current_actions:
        projection = ConversationActionStateV1.model_validate(payload)
        key = (
            projection.action_type,
            projection.resource_type,
            projection.resource_id,
        )
        candidate = _attempted_action_snapshot(projection)
        previous = merged.get(key)
        if previous is None or _attempted_action_is_newer(candidate, previous):
            merged[key] = candidate

    newest_first = sorted(
        merged.values(),
        key=_attempted_action_display_order,
        reverse=True,
    )
    prioritized = [
        *[
            item
            for item in newest_first
            if item.get("projection_status") in ACTIVE_ACTION_PROJECTION_STATES
        ],
        *newest_first[:1],
        *newest_first,
    ]
    retained: list[dict[str, Any]] = []
    retained_identities: set[tuple[str, str, str]] = set()
    retained_limit = MAX_ATTEMPTED_ACTIONS - (1 if legacy_present else 0)
    for item in prioritized:
        identity = item["action_identity"]
        key = (
            str(identity["action_type"]),
            str(identity["resource_type"]),
            str(identity["resource_id"]),
        )
        if key in retained_identities:
            continue
        retained.append(item)
        retained_identities.add(key)
        if len(retained) >= retained_limit:
            break
    retained.sort(key=_attempted_action_display_order)

    return [
        *(
            [
                {
                    "schema_version": "attempted-action-memory-legacy.v1",
                    "legacy_record_present": True,
                    "historical": True,
                    "grants_action_authority": False,
                }
            ]
            if legacy_present
            else []
        ),
        *retained,
    ]


def _attempted_action_snapshot(
    projection: ConversationActionStateV1,
) -> dict[str, Any]:
    return {
        "schema_version": "attempted-action-memory.v1",
        "action_identity": {
            "action_type": projection.action_type,
            "resource_type": projection.resource_type,
            "resource_id": projection.resource_id,
        },
        "approval_id": projection.approval_id,
        "origin_run_id": projection.origin_run_id,
        "origin_turn_id": projection.origin_turn_id,
        "resource_version": projection.resource_version,
        "approval_status": projection.approval_status,
        "projection_status": projection.projection_status,
        "status_version": projection.status_version,
        "decision_class": projection.decision_class,
        "customer_safe_reason_code": projection.customer_safe_reason_code,
        "execution_state": projection.execution_state,
        "business_action_id": projection.business_action_id,
        "observed_at": _coerce_utc_instant(projection.updated_at).isoformat(),
        "source_event_id": projection.source_event_id,
        "source_event_hash": projection.source_event_hash,
        "historical": True,
        "grants_action_authority": False,
    }


def _attempted_action_is_newer(
    candidate: dict[str, Any],
    previous: dict[str, Any],
) -> bool:
    if candidate.get("approval_id") == previous.get("approval_id"):
        return (
            int(candidate.get("status_version") or 0),
            _coerce_utc_instant(candidate.get("observed_at")),
        ) >= (
            int(previous.get("status_version") or 0),
            _coerce_utc_instant(previous.get("observed_at")),
        )
    return _attempted_action_display_order(candidate) >= _attempted_action_display_order(previous)


def _attempted_action_display_order(
    item: dict[str, Any],
) -> tuple[datetime, int, str]:
    return (
        _coerce_utc_instant(item.get("observed_at")),
        int(item.get("status_version") or 0),
        str(item.get("approval_id") or ""),
    )


def _coerce_utc_instant(value: object) -> datetime:
    """Compare legacy naive and offset timestamps as one UTC timeline."""

    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    else:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class MemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist_summary(self, state: AgentState) -> TicketSummary:
        run_id = state.get("run_id")
        if not run_id:
            raise ValueError("memory requires a finalized source run")
        run = await self.session.get(AgentRun, run_id)
        if (
            run is None
            or run.status != "completed"
            or not run.canonical_checkpoint_hash
            or run.ticket_id != state["ticket_id"]
        ):
            raise ValueError("memory source run is not finalized canonical lineage")
        unfinished_turns = await self.session.scalar(
            select(func.count())
            .select_from(TurnGroup)
            .where(TurnGroup.run_id == run.id, TurnGroup.status != "closed")
        )
        if unfinished_turns:
            raise ValueError("memory source run contains an unclosed TurnGroup")
        event_watermark = await self.session.scalar(
            select(func.max(AgentEvent.ticket_sequence)).where(
                AgentEvent.ticket_id == run.ticket_id
            )
        )
        if event_watermark is None:
            raise ValueError("memory source run has no durable event watermark")
        summary = await self.session.scalar(
            select(TicketSummary).where(TicketSummary.ticket_id == state["ticket_id"])
        )
        if (
            summary is not None
            and summary.source_run_id == run.id
            and summary.canonical_checkpoint_hash == run.canonical_checkpoint_hash
            and summary.event_watermark == event_watermark
        ):
            return summary
        ticket = await self.session.get(SupportTicket, state["ticket_id"])
        if ticket is None:
            raise ValueError("ticket not found while persisting summary")
        final = state["final"]
        terminal = str(final["terminal_state"])
        ticket.status = terminal
        ticket.issue_type = str(state.get("classification", {}).get("issue_type", "unknown"))
        ticket.risk = str(state.get("classification", {}).get("risk", ticket.risk))
        ticket.final_response = str(final["answer"])
        now = datetime.now(UTC)
        used_chunks = set(final.get("knowledge_chunk_ids", []))
        used_business_sources = set(final.get("business_source_ids", []))
        confirmed_facts: list[dict[str, Any]] = []
        for evidence in state.get("evidence", []):
            if evidence.get("chunk_id") not in used_chunks:
                continue
            confirmed_facts.append(
                {
                    "fact_id": f"fact_{uuid4().hex}",
                    "customer_id": ticket.customer_id,
                    "fact_type": "knowledge_evidence",
                    "value": evidence.get("supporting_span", evidence.get("excerpt", "")),
                    "source_type": "knowledge_chunk",
                    "source_id": evidence["chunk_id"],
                    "observed_at": evidence.get("effective_at", now.isoformat()),
                    "confirmed_at": now.isoformat(),
                    "valid_until": None,
                    "freshness_policy": "versioned_knowledge",
                    "resource_version": evidence.get("version"),
                    "document_id": evidence.get("document_id"),
                    "index_version": evidence.get("index_version"),
                    "locator_hash": evidence.get("source_locator", {}).get("locator_hash"),
                    "supersedes_fact_id": None,
                    "status": "active",
                }
            )
        for observation in state.get("tool_observations", []):
            tool_name = str(observation.get("tool_name", "query_account"))
            policy, lifetime = FRESHNESS.get(tool_name, ("observation_only", timedelta(seconds=0)))
            observed_at = _as_datetime(observation.get("observed_at"), fallback=now)
            valid_until = observed_at + lifetime
            for source in observation.get("source_refs", []):
                if source.get("source_type") == "knowledge_chunk":
                    continue
                if source.get("source_id") not in used_business_sources:
                    continue
                source_observed = _as_datetime(source.get("observed_at"), fallback=observed_at)
                confirmed_facts.append(
                    {
                        "fact_id": f"fact_{uuid4().hex}",
                        "customer_id": ticket.customer_id,
                        "fact_type": tool_name,
                        "value": observation.get("data", self._legacy_observation(observation)),
                        "source_type": source["source_type"],
                        "source_id": source["source_id"],
                        "observed_at": source_observed.isoformat(),
                        "confirmed_at": now.isoformat(),
                        "valid_until": valid_until.isoformat(),
                        "freshness_policy": policy,
                        "resource_version": observation.get("resource_version"),
                        "supersedes_fact_id": None,
                        "status": "active" if valid_until > now else "expired",
                    }
                )
        await self._supersede_previous(ticket.customer_id, confirmed_facts)
        attempted = merge_attempted_actions(
            list(summary.attempted_actions) if summary is not None else [],
            await self._load_canonical_action_states(ticket),
        )
        source_refs = [
            {"source_type": item["source_type"], "source_id": item["source_id"]}
            for item in confirmed_facts
        ]
        values = {
            "issue_type": ticket.issue_type,
            "confirmed_facts": confirmed_facts,
            "attempted_actions": attempted,
            "open_questions": follow_up_questions(
                terminal_state=terminal,
                finish_reason=run.agent_finish_reason,
                automation_mode=ticket.automation_mode,
            ),
            "source_refs": source_refs,
            "source_run_id": run.id,
            "canonical_checkpoint_hash": run.canonical_checkpoint_hash,
            "event_watermark": event_watermark,
            "freshness_at": now,
            "expires_at": None,
        }
        if summary is None:
            summary = TicketSummary(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                customer_id=ticket.customer_id,
                **values,
            )
            self.session.add(summary)
        else:
            for key, value in values.items():
                setattr(summary, key, value)
        self.session.add(
            AuditEvent(
                tenant_id=ticket.tenant_id,
                ticket_id=ticket.id,
                customer_id=ticket.customer_id,
                event_type="ticket_finalized",
                actor_type="agent_runtime",
                actor_id=None,
                payload={"terminal_state": terminal, "summary_id": summary.id},
                trace_id=state["trace_id"],
                run_id=state.get("run_id"),
                created_at=now,
            )
        )
        await self.session.flush()
        return summary

    async def _load_canonical_action_states(
        self,
        ticket: SupportTicket,
    ) -> list[dict[str, Any]]:
        """Re-read action truth inside the finalizer transaction.

        In production the Worker remains unable to query ``runtime_jobs``
        directly.  The existing claim capability returns a customer-safe source
        bundle bound to the current fenced lease.  SQLite and privileged test
        paths use the same pure projector through its ORM adapter.
        """

        if self.session.get_bind().dialect.name != "postgresql":
            projections = await ConversationActionStateProjector(self.session).list_for_ticket(
                tenant_id=ticket.tenant_id,
                customer_id=ticket.customer_id,
                ticket_id=ticket.id,
            )
            return [item.model_dump(mode="json") for item in projections]

        execution = worker_execution_context.get()
        snapshot = await self.session.scalar(
            text("SELECT supportguard_worker_claim_job(:job_id,:owner)"),
            {
                "job_id": execution.job_id,
                "owner": execution.executor_service_principal,
            },
        )
        if (
            not isinstance(snapshot, dict)
            or str(snapshot.get("job_id", "")) != execution.job_id
            or str(snapshot.get("run_id", "")) != execution.run_id
            or str(snapshot.get("ticket_id", "")) != execution.ticket_id
            or str(snapshot.get("tenant_id", "")) != execution.tenant_id
            or int(snapshot.get("fencing_token", -1)) != execution.fencing_token
        ):
            raise RuntimeConflict("memory action state lease is stale")
        source_bundles = snapshot.get("conversation_action_sources")
        if not isinstance(source_bundles, list):
            raise RuntimeConflict("memory action state capability is unavailable")
        if any(not isinstance(item, dict) for item in source_bundles):
            raise RuntimeConflict("memory action state capability returned an invalid bundle")
        return [
            project_conversation_action_state(
                conversation_action_sources_from_mapping(item)
            ).model_dump(mode="json")
            for item in source_bundles
        ]

    async def _supersede_previous(
        self, customer_id: str, current_facts: list[dict[str, Any]]
    ) -> None:
        summaries = (
            await self.session.scalars(
                select(TicketSummary).where(TicketSummary.customer_id == customer_id)
            )
        ).all()
        for current in current_facts:
            if current["fact_type"] in {"knowledge_evidence", "query_billing_record"}:
                continue
            for summary in summaries:
                changed = False
                facts = list(summary.confirmed_facts)
                for previous in facts:
                    if (
                        previous.get("status") == "active"
                        and previous.get("fact_type") == current["fact_type"]
                        and previous.get("source_id") == current["source_id"]
                    ):
                        previous["status"] = "superseded"
                        current["supersedes_fact_id"] = previous.get("fact_id")
                        changed = True
                if changed:
                    summary.confirmed_facts = facts

    async def load_relevant_history(
        self, *, customer_id: str, issue_type: str, limit: int = 3
    ) -> list[TicketSummary]:
        statement = (
            select(TicketSummary)
            .join(AgentRun, AgentRun.id == TicketSummary.source_run_id)
            .join(SupportTicket, SupportTicket.id == TicketSummary.ticket_id)
            .where(
                TicketSummary.customer_id == customer_id,
                TicketSummary.issue_type == issue_type,
                SupportTicket.status.in_(TERMINAL_STATES),
                AgentRun.status == "completed",
                AgentRun.canonical_checkpoint_hash == TicketSummary.canonical_checkpoint_hash,
            )
            .order_by(TicketSummary.updated_at.desc())
            .limit(min(limit, 3))
        )
        summaries = list((await self.session.scalars(statement)).all())
        now = datetime.now(UTC)
        for summary in summaries:
            changed = False
            facts = list(summary.confirmed_facts)
            for fact in facts:
                if fact.get("status") == "active" and fact.get("fact_type") == "knowledge_evidence":
                    current_document = await self.session.scalar(
                        select(KnowledgeDocument.id)
                        .join(
                            KnowledgeIngestRun,
                            KnowledgeIngestRun.index_version == KnowledgeDocument.index_version,
                        )
                        .where(
                            KnowledgeIngestRun.is_active.is_(True),
                            KnowledgeIngestRun.status == "succeeded",
                            KnowledgeDocument.document_key == fact.get("document_id"),
                            KnowledgeDocument.version == fact.get("resource_version"),
                            KnowledgeDocument.index_version == fact.get("index_version"),
                            KnowledgeDocument.status == "active",
                        )
                    )
                    if current_document is None:
                        fact["status"] = "superseded"
                        changed = True
                valid_until = fact.get("valid_until")
                if (
                    fact.get("status") == "active"
                    and valid_until
                    and _as_datetime(valid_until, fallback=now) <= now
                ):
                    fact["status"] = "expired"
                    changed = True
            if changed:
                summary.confirmed_facts = facts
        await self.session.flush()
        return summaries

    @staticmethod
    def _legacy_observation(observation: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in observation.items()
            if key not in {"source_refs", "observed_at", "duration_ms"}
        }


class MemoryWriter:
    def __init__(self, factory: ScopedSessionFactory) -> None:
        self.factory = factory

    async def persist(self, state: AgentState) -> None:
        context = worker_execution_context.get()
        async with self.factory.worker(context) as session:
            await MemoryService(session).persist_summary(state)
            await session.commit()


class MemoryHistoryLoader:
    def __init__(self, factory: ScopedSessionFactory) -> None:
        self.factory = factory

    async def load(self, *, customer_id: str, issue_type: str) -> list[dict[str, Any]]:
        context = worker_execution_context.get()
        async with self.factory.worker(context) as session:
            summaries = await MemoryService(session).load_relevant_history(
                customer_id=customer_id,
                issue_type=issue_type,
            )
            await session.commit()
            return [
                {
                    "ticket_id": item.ticket_id,
                    "issue_type": item.issue_type,
                    "confirmed_facts": [
                        fact for fact in item.confirmed_facts if fact.get("status") == "active"
                    ],
                    "attempted_actions": item.attempted_actions,
                    "open_questions": item.open_questions,
                    "source_refs": item.source_refs,
                }
                for item in summaries
            ]
