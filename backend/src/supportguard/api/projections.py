from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select

from supportguard.api.approval_projection import (
    project_approval_detail,
)
from supportguard.api.contracts import (
    TICKET_EVENT_LIMIT,
    TICKET_EVIDENCE_LIMIT,
    TICKET_FACT_LIMIT,
    TICKET_MESSAGE_LIMIT,
    TICKET_TIMELINE_LIMIT,
    ConversationActionResponse,
    CustomerActionPayloadResponse,
    CustomerEntitlementTargetResponse,
)
from supportguard.api.sse import _safe_public_event_payload
from supportguard.contracts.public_failures import classify_public_failure
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    CitationBinding,
    ClaimRecord,
    ConversationTurn,
    HumanDecision,
    KnowledgeChunk,
    KnowledgeDocument,
    ProposalRecord,
    RetrievalTrace,
    RuntimeJob,
    Subscription,
    SupportTicket,
    TicketMessage,
    TicketSummary,
    ToolObservation,
)
from supportguard.db.session import ScopedAsyncSession
from supportguard.services.conversation_action_state import (
    ConversationActionStateProjectionError,
    ConversationActionStateProjector,
    ConversationActionStateV1,
    conversation_action_sources_from_mapping,
    project_conversation_action_state,
)
from supportguard.services.runtime_jobs import RuntimeConflict
from supportguard.services.turn_results import activity_label


def _upgrade_unavailable() -> JSONResponse:
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "upgrade_in_progress",
    )


def _accepted_provider_identity(request: Request) -> tuple[str, str, str]:
    runtime = getattr(request.app.state, "runtime", None)
    provider = getattr(runtime, "provider", None)
    if provider is not None:
        return (
            str(provider.model),
            str(provider.mode),
            str(provider.tool_call_mode),
        )
    settings = request.app.state.settings
    if settings.app_env == "test" or settings.demo_fake_provider:
        return "deterministic-fake", "fake", "native_fixture"
    return settings.llm_model, "production", "native"


def _public_event_projection(
    value: AgentEvent | dict[str, Any],
    *,
    inspector: bool = False,
) -> dict[str, Any]:
    """Return the complete and only public event shape.

    Durable AgentEvent payloads are an internal trace contract.  Customer
    surfaces only receive bounded refresh metadata; the technical inspector
    gets a separate, still allowlisted explanation projection.
    """

    def field(name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name)

    raw_payload = field("payload")
    event_id = field("id")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 64:
        raise RuntimeError("public_event_identity_invalid")
    payload = (
        _inspector_event_payload(raw_payload)
        if inspector and isinstance(raw_payload, dict)
        else _safe_public_event_payload(raw_payload)
    )
    return {
        "id": event_id,
        "event_type": str(field("event_type")),
        "payload": payload,
        "run_id": str(field("run_id")),
        "ticket_sequence": int(field("ticket_sequence")),
        "run_sequence": int(field("run_sequence")),
        "step_index": int(field("step_index")),
        "tool_round": int(field("tool_round")),
        "status": str(field("status")),
        "created_at": field("created_at"),
    }


_PUBLIC_RUNTIME_STRING_FIELDS = {
    "model",
    "provider",
    "provider_mode",
    "tool_call_mode",
    "prompt_version",
    "schema_version",
    "context_assembly_version",
    "knowledge_index_contract",
    "attempt_status",
    "source",
}
_PUBLIC_RUNTIME_COUNT_FIELDS = {
    "provider_transport_attempts",
    "provider_retry_count",
}
_PUBLIC_FINISH_REASONS = {
    "action_state_answer",
    "answered",
    "credential_redaction_guidance",
    "evidence_freshness_insufficient",
    "executed",
    "failed",
    "manual_takeover",
    "needs_clarification",
    "out_of_scope",
    "proposal_created",
    "proposed",
    "refused",
    "rejected",
    "requested_action_unresolved",
    "terminal_business_outcome",
    "withdrawn",
}
_PUBLIC_RUN_STATUSES = {
    "accepted",
    "awaiting_approval",
    "completed",
    "failed",
    "interrupted",
    "queued",
    "running",
}
_PUBLIC_JOB_STATUSES = {
    "blocked",
    "cancelled",
    "completed",
    "dead",
    "failed",
    "leased",
    "pending",
    "queued",
    "running",
    "succeeded",
}
_PUBLIC_JOB_OUTCOMES = {
    "completed",
    "executed",
    "rejected",
    "resolved",
    "succeeded",
    "withdrawn",
}


def _safe_runtime_provenance(value: object) -> dict[str, Any]:
    """Expose semantic runtime identity without hashes, IDs, or raw metadata."""

    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for key in _PUBLIC_RUNTIME_STRING_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 128:
            projected[key] = item
    for key in _PUBLIC_RUNTIME_COUNT_FIELDS:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 100:
            projected[key] = item
    return projected


def _public_finish_reason(value: object, *, failed: bool) -> str | None:
    if isinstance(value, str) and value in _PUBLIC_FINISH_REASONS:
        return value
    return "failed" if failed else None


def _public_run_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Project a CustomerRun through one strict public allowlist."""

    raw_error = value.get("error_code")
    existing_failure = value.get("failure_category")
    failure_category = classify_public_failure(raw_error)
    if (
        failure_category is None
        and isinstance(existing_failure, str)
        and existing_failure in {"api_request", "provider", "tool", "runtime"}
    ):
        failure_category = cast(
            Literal["api_request", "provider", "tool", "runtime"],
            existing_failure,
        )
    raw_status = value.get("status")
    status_value = (
        raw_status
        if isinstance(raw_status, str) and raw_status in _PUBLIC_RUN_STATUSES
        else "unknown"
    )
    failed = status_value == "failed" or failure_category is not None
    configured = value.get("configured_runtime")
    actual = value.get("actual_runtime")
    job = value.get("job")
    budgets = value.get("budgets")
    safe_budgets = {
        key: int(item)
        for key in ("tool_rounds", "tool_attempts", "llm_calls")
        if isinstance((item := budgets.get(key) if isinstance(budgets, dict) else None), int)
        and not isinstance(item, bool)
        and 0 <= item <= 1_000_000
    }
    for key in ("tool_rounds", "tool_attempts", "llm_calls"):
        safe_budgets.setdefault(key, 0)
    safe_job: dict[str, Any] | None = None
    if isinstance(job, dict):
        raw_job_status = job.get("status")
        job_status = (
            raw_job_status
            if isinstance(raw_job_status, str) and raw_job_status in _PUBLIC_JOB_STATUSES
            else "unknown"
        )
        raw_job_outcome = job.get("outcome")
        has_error = bool(
            job.get("has_error")
            or job.get("last_error") is not None
            or raw_error is not None
            or job_status in {"dead", "failed"}
        )
        job_outcome = (
            "failed"
            if has_error
            else raw_job_outcome
            if isinstance(raw_job_outcome, str) and raw_job_outcome in _PUBLIC_JOB_OUTCOMES
            else "completed"
            if job_status in {"completed", "succeeded"}
            else None
        )
        safe_job = {
            "status": job_status,
            "outcome": job_outcome,
            "has_error": has_error,
        }
    return {
        "id": str(value.get("id", "")),
        "ticket_id": (str(value["ticket_id"]) if value.get("ticket_id") is not None else None),
        "status": status_value,
        "status_version": (
            int(value["status_version"])
            if isinstance(value.get("status_version"), int)
            and not isinstance(value.get("status_version"), bool)
            else 0
        ),
        "finish_reason": _public_finish_reason(value.get("finish_reason"), failed=failed),
        "model": str(value.get("model", "unknown"))[:128],
        "provider_mode": str(value.get("provider_mode", "unknown"))[:128],
        "tool_call_mode": str(value.get("tool_call_mode", "unknown"))[:128],
        "configured_runtime": _safe_runtime_provenance(configured),
        "actual_runtime": (_safe_runtime_provenance(actual) if isinstance(actual, dict) else None),
        "knowledge_index_version": (
            str(value["knowledge_index_version"])[:128]
            if value.get("knowledge_index_version") is not None
            else None
        ),
        "failure_category": failure_category,
        "created_at": value.get("created_at"),
        "completed_at": value.get("completed_at"),
        "budgets": safe_budgets,
        "job": safe_job,
    }


async def _run_projection(
    session: ScopedAsyncSession,
    run: AgentRun,
) -> dict[str, Any]:
    job = await session.scalar(
        select(RuntimeJob)
        .where(RuntimeJob.run_id == run.id)
        .order_by(RuntimeJob.created_at.desc(), RuntimeJob.id.desc())
        .limit(1)
    )
    actual_attempt = await session.scalar(
        select(AgentCallAttempt)
        .where(
            AgentCallAttempt.run_id == run.id,
            AgentCallAttempt.call_kind == "llm",
        )
        .order_by(
            AgentCallAttempt.ordinal.desc(),
            AgentCallAttempt.created_at.desc(),
            AgentCallAttempt.id.desc(),
        )
        .limit(1)
    )
    configured_runtime = {
        "model": run.model,
        "provider_mode": run.provider_mode,
        "tool_call_mode": run.tool_call_mode,
        "source": "command_acceptance",
    }
    actual_runtime = (
        None
        if actual_attempt is None
        else {
            **_safe_runtime_provenance(actual_attempt.runtime_provenance),
            "attempt_status": actual_attempt.status,
            "source": "agent_call_attempt",
        }
    )
    return _public_run_projection(
        {
            "id": run.id,
            "ticket_id": run.ticket_id,
            "status": run.status,
            "status_version": run.status_version,
            "finish_reason": run.agent_finish_reason,
            "model": run.model,
            "provider_mode": run.provider_mode,
            "tool_call_mode": run.tool_call_mode,
            "configured_runtime": configured_runtime,
            "actual_runtime": actual_runtime,
            "knowledge_index_version": run.knowledge_index_version,
            "error_code": run.error_code,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "budgets": {
                "tool_rounds": run.tool_rounds,
                "tool_attempts": run.tool_attempts,
                "llm_calls": run.llm_calls,
            },
            "job": None
            if job is None
            else {
                "status": job.status,
                "outcome": job.outcome,
                "has_error": job.last_error is not None,
            },
        }
    )


def _inspector_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Allowlist customer-safe trace metadata; never return raw event payloads."""

    projected: dict[str, Any] = {}
    for key in ("tool_name", "freshness_status", "route", "action_type"):
        value = payload.get(key)
        if isinstance(value, str):
            projected[key] = value
    source_count = payload.get("source_count")
    if isinstance(source_count, int) and not isinstance(source_count, bool):
        projected["source_count"] = source_count
    for key in ("injected_tool_allowlist", "injected_tools"):
        value = payload.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            projected[key] = value[:5]
    remaining = payload.get("remaining_budget")
    if isinstance(remaining, dict):
        projected["remaining_budget"] = {
            key: value
            for key in ("llm_calls", "tool_rounds", "tool_attempts")
            if isinstance((value := remaining.get(key)), int) and not isinstance(value, bool)
        }
    if "error_code" in payload or payload.get("failure_recorded") is True:
        projected["failure_recorded"] = True
    if "agent_finish_reason" in payload or payload.get("stop_condition_recorded") is True:
        projected["stop_condition_recorded"] = True
    return projected


async def _sqlite_run_inspector(
    session: ScopedAsyncSession,
    *,
    customer_id: str,
    conversation_id: str,
    turn_id: str,
    message_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    """Build one exact, customer-scoped historical run projection."""

    ticket = await session.scalar(
        select(SupportTicket).where(
            SupportTicket.id == conversation_id,
            SupportTicket.customer_id == customer_id,
        )
    )
    if ticket is None:
        return None
    turn = await session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.id == turn_id,
            ConversationTurn.ticket_id == ticket.id,
            ConversationTurn.run_id == run_id,
        )
    )
    if turn is None:
        return None
    run = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.ticket_id == ticket.id,
            AgentRun.customer_id == customer_id,
            AgentRun.turn_id == turn.id,
        )
    )
    if run is None:
        return None
    message = await session.scalar(
        select(TicketMessage).where(
            TicketMessage.id == message_id,
            TicketMessage.ticket_id == ticket.id,
            TicketMessage.turn_id == turn.id,
            TicketMessage.run_id == run.id,
            TicketMessage.message_kind == "assistant",
        )
    )
    if message is None:
        return None
    events = (
        await session.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.ticket_id == ticket.id,
                AgentEvent.run_id == run.id,
                AgentEvent.visibility == "customer",
            )
            .order_by(AgentEvent.ticket_sequence, AgentEvent.id)
            .limit(TICKET_EVENT_LIMIT)
        )
    ).all()
    citations = [
        item
        for item in await _published_knowledge_sources(session, run.id)
        if item.get("message_id") == message.id
    ]
    return {
        "message_id": message.id,
        "turn_id": turn.id,
        "run_id": run.id,
        "run": await _run_projection(session, run),
        "timeline": [_public_event_projection(item, inspector=True) for item in events],
        "knowledge_sources": [
            item for item in citations if (item.get("source_type") or "knowledge") == "knowledge"
        ],
        "business_facts": [
            item for item in citations if item.get("source_type") == "business_fact"
        ],
    }


async def _published_knowledge_sources(
    session: ScopedAsyncSession,
    run_id: str,
) -> list[dict[str, Any]]:
    """Project only sources bound to validated claims from the current Run."""

    claims = (
        await session.scalars(
            select(ClaimRecord)
            .where(ClaimRecord.run_id == run_id, ClaimRecord.status == "validated")
            .order_by(ClaimRecord.created_at, ClaimRecord.id)
        )
    ).all()
    binding_ids = list(
        dict.fromkeys(
            str(binding_id)
            for claim in claims
            for binding_id in claim.support_refs.get("citation_binding_ids", [])
            if binding_id
        )
    )
    bindings = []
    if binding_ids:
        bindings = list(
            (
                await session.scalars(
                    select(CitationBinding)
                    .where(
                        CitationBinding.run_id == run_id,
                        CitationBinding.id.in_(binding_ids),
                    )
                    .order_by(CitationBinding.id)
                    .limit(TICKET_EVIDENCE_LIMIT + 1)
                )
            ).all()
        )
    business_source_ids = list(
        dict.fromkeys(
            str(source_id)
            for claim in claims
            for source_id in claim.support_refs.get("observation_source_ids", [])
            if source_id
        )
    )
    all_observations = list(
        (
            await session.scalars(
                select(ToolObservation)
                .where(ToolObservation.run_id == run_id)
                .order_by(ToolObservation.created_at, ToolObservation.id)
            )
        ).all()
    )
    observations = {observation.id: observation for observation in all_observations}
    claims_by_binding: dict[str, list[ClaimRecord]] = {}
    claims_by_source: dict[str, list[ClaimRecord]] = {}
    for claim in claims:
        for binding_id in claim.support_refs.get("citation_binding_ids", []):
            claims_by_binding.setdefault(str(binding_id), []).append(claim)
        for source_id in claim.support_refs.get("observation_source_ids", []):
            claims_by_source.setdefault(str(source_id), []).append(claim)
    projected: list[dict[str, Any]] = []
    assistant_message_id = await session.scalar(
        select(TicketMessage.id)
        .where(
            TicketMessage.run_id == run_id,
            TicketMessage.message_kind == "assistant",
            TicketMessage.publication_key == f"assistant:{run_id}",
        )
        .limit(1)
    )
    if assistant_message_id is None:
        # A Runtime failure message may also use ``message_kind=assistant``.
        # Claims belong only to the canonical answer publication for this Run;
        # never attach them to a later failure/fallback message by position.
        return []
    for binding in bindings:
        observation = observations.get(binding.observation_id)
        evidence = (
            observation.payload.get("data", {}).get("evidence", [])
            if observation is not None
            else []
        )
        if not isinstance(evidence, list):
            continue
        matches = [
            item
            for item in evidence
            if isinstance(item, dict)
            and isinstance(item.get("source_locator"), dict)
            and item["source_locator"].get("locator_hash") == binding.locator_hash
        ]
        if len(matches) != 1:
            continue
        for claim in claims_by_binding.get(binding.id, []):
            projected.append(
                {
                    **matches[0],
                    "source_type": "knowledge",
                    "citation_binding_id": binding.id,
                    "claim_id": claim.id,
                    "message_id": assistant_message_id,
                    "claim_summary": claim.claim_text,
                    "binding_purpose": "answer_claim",
                }
            )
    for observation in all_observations:
        payload = observation.payload
        source_refs = payload.get("source_refs", [])
        if not isinstance(source_refs, list):
            continue
        matching_refs = [
            item
            for item in source_refs
            if isinstance(item, dict) and str(item.get("source_id")) in business_source_ids
        ]
        data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
        resource_version = (
            payload.get("resource_version") or data.get("version") or data.get("business_version")
        )
        for source_ref in matching_refs:
            source_id = str(source_ref["source_id"])
            for claim in claims_by_source.get(source_id, []):
                projected.append(
                    {
                        "source_type": "business_fact",
                        "document_id": source_id,
                        "observation_source_id": source_id,
                        "version": (
                            str(resource_version) if resource_version is not None else None
                        ),
                        "title": _business_fact_title(str(payload.get("tool_name", ""))),
                        "section_path": "当前业务事实",
                        "supporting_span": claim.claim_text,
                        "claim_id": claim.id,
                        "message_id": assistant_message_id,
                        "claim_summary": claim.claim_text,
                        "observed_at": payload.get("observed_at") or source_ref.get("observed_at"),
                        "freshness": payload.get("freshness_status"),
                        "status": payload.get("status"),
                        "fact_summary": _safe_business_fact_summary(data),
                        "binding_purpose": "answer_claim",
                    }
                )
    return projected


def _business_fact_title(tool_name: str) -> str:
    return {
        "query_account": "客户账户状态",
        "query_api_usage": "API 使用情况",
        "query_service_status": "服务状态",
        "query_incident_impact": "事故影响",
        "query_request_trace": "请求追踪结果",
        "query_billing_record": "账单状态",
        "query_api_key_metadata": "API Key 元数据",
        "query_subscription": "套餐与配额",
    }.get(tool_name, "实时业务事实")


def _safe_business_fact_summary(data: dict[str, Any]) -> dict[str, Any]:
    safe_keys = {
        "status",
        "plan",
        "region",
        "currency",
        "amount",
        "limit",
        "current_limit",
        "requested_limit",
        "window",
        "request_status",
        "impact",
        "service",
    }
    return {key: value for key, value in data.items() if key in safe_keys}


def _bounded_ticket_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the public aggregation contract even against an older DB head."""

    limits = {
        "messages": TICKET_MESSAGE_LIMIT,
        "timeline": TICKET_TIMELINE_LIMIT,
        "knowledge_sources": TICKET_EVIDENCE_LIMIT,
        "business_facts": TICKET_FACT_LIMIT,
    }
    existing = payload.get("aggregation")
    existing_windows = existing if isinstance(existing, dict) else {}
    bounded = dict(payload)
    raw_messages = payload.get("messages", [])
    bounded["messages"] = [
        {key: item[key] for key in ("id", "role", "content", "created_at") if key in item}
        for item in raw_messages
        if isinstance(item, dict)
    ]
    raw_timeline = payload.get("timeline", [])
    bounded["timeline"] = [
        _public_event_projection(item) for item in raw_timeline if isinstance(item, dict)
    ]
    raw_facts = payload.get("business_facts", [])
    bounded["business_facts"] = [
        {
            **{key: item[key] for key in ("tool_name", "status", "observed_at") if key in item},
            "fact_summary": _safe_business_fact_summary(
                cast(dict[str, Any], item["fact_summary"])
                if isinstance(item.get("fact_summary"), dict)
                else cast(dict[str, Any], item["data"])
                if isinstance(item.get("data"), dict)
                else {}
            ),
        }
        for item in raw_facts
        if isinstance(item, dict)
    ]
    raw_summary = payload.get("summary")
    if isinstance(raw_summary, dict):
        bounded["summary"] = {
            key: raw_summary[key]
            for key in (
                "confirmed_facts",
                "attempted_actions",
                "open_questions",
                "freshness_at",
                "expires_at",
            )
            if key in raw_summary
        }
    raw_action = payload.get("business_action")
    if isinstance(raw_action, dict):
        bounded["business_action"] = {
            key: raw_action[key]
            for key in (
                "id",
                "status",
                "action_type",
                "resource_id",
                "resource_version",
                "created_at",
            )
            if key in raw_action
        }
    raw_run = payload.get("latest_run")
    if isinstance(raw_run, dict):
        bounded["latest_run"] = _public_run_projection(raw_run)
    windows: dict[str, dict[str, Any]] = {}
    for field, limit in limits.items():
        raw_items = bounded.get(field, [])
        items = raw_items if isinstance(raw_items, list) else []
        selected = items[-limit:]
        prior = existing_windows.get(field, {})
        prior_window = prior if isinstance(prior, dict) else {}
        total = int(prior_window.get("total", len(items)))
        has_more = bool(prior_window.get("has_more", total > len(selected)))
        total_is_exact = bool(prior_window.get("total_is_exact", not has_more))
        boundary = prior_window.get("boundary")
        if boundary is None and has_more and selected:
            first = selected[0]
            if isinstance(first, dict):
                boundary = next(
                    (
                        str(first[key])
                        for key in (
                            "id",
                            "ticket_sequence",
                            "citation_binding_id",
                            "chunk_id",
                            "observed_at",
                        )
                        if first.get(key) is not None
                    ),
                    None,
                )
        bounded[field] = selected
        windows[field] = {
            "limit": limit,
            "returned": len(selected),
            "total": total,
            "total_is_exact": total_is_exact,
            "has_more": has_more,
            "boundary": str(boundary) if boundary is not None else None,
        }
    bounded["aggregation"] = windows
    latest_run = bounded.get("latest_run")
    if isinstance(latest_run, dict) and not latest_run.get("knowledge_index_version"):
        source_versions = {
            str(item["index_version"])
            for item in bounded.get("knowledge_sources", [])
            if isinstance(item, dict) and item.get("index_version")
        }
        if len(source_versions) == 1:
            bounded["latest_run"] = {
                **latest_run,
                "knowledge_index_version": next(iter(source_versions)),
            }
    return bounded


async def _sqlite_ticket_projection(
    session: ScopedAsyncSession,
    ticket: SupportTicket,
) -> dict[str, Any]:
    title_message = await session.scalar(
        select(TicketMessage)
        .where(
            TicketMessage.ticket_id == ticket.id,
            TicketMessage.role.in_(("user", "customer")),
        )
        .order_by(TicketMessage.created_at, TicketMessage.id)
        .limit(1)
    )
    messages = list(
        reversed(
            (
                await session.scalars(
                    select(TicketMessage)
                    .where(TicketMessage.ticket_id == ticket.id)
                    .order_by(
                        TicketMessage.conversation_sequence.desc(),
                        TicketMessage.created_at.desc(),
                        TicketMessage.id.desc(),
                    )
                    .limit(TICKET_MESSAGE_LIMIT + 1)
                )
            ).all()
        )
    )
    summary = await session.scalar(
        select(TicketSummary).where(TicketSummary.ticket_id == ticket.id)
    )
    latest_run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.ticket_id == ticket.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    events = list(
        reversed(
            (
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.ticket_id == ticket.id,
                        AgentEvent.run_id
                        == (latest_run.id if latest_run is not None else "<none>"),
                        AgentEvent.visibility == "customer",
                    )
                    .order_by(AgentEvent.ticket_sequence.desc())
                    .limit(TICKET_TIMELINE_LIMIT + 1)
                )
            ).all()
        )
    )
    knowledge_sources = (
        await _published_knowledge_sources(session, latest_run.id) if latest_run is not None else []
    )
    business_facts: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "tool_observation":
            continue
        payload = event.payload
        if payload.get("tool_name") != "search_knowledge":
            business_facts.append(
                {
                    "tool_name": payload.get("tool_name"),
                    "status": payload.get("status"),
                    "observed_at": payload.get("observed_at"),
                    "source_refs": payload.get("source_refs", []),
                    "data": payload.get("data", {}),
                }
            )
    approval = await session.scalar(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.ticket_id == ticket.id,
            ApprovalRequest.run_id == (latest_run.id if latest_run is not None else "<none>"),
        )
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        .limit(1)
    )
    action = (
        await session.scalar(
            select(BusinessAction)
            .where(BusinessAction.approval_id == approval.id)
            .order_by(BusinessAction.created_at.desc(), BusinessAction.id.desc())
            .limit(1)
        )
        if approval is not None
        else None
    )
    appendable = ticket.status in {"open", "needs_clarification"}
    title = (
        " ".join(title_message.content.split())[:80] if title_message is not None else "未命名工单"
    )
    return {
        "id": ticket.id,
        "title": title,
        "status": ticket.status,
        "issue_type": ticket.issue_type,
        "risk": ticket.risk,
        "version": ticket.version,
        # A follow-up Run can be queued while the ticket row still carries the
        # previous Run's response. Publish an answer only when the latest Run
        # has completed, otherwise the product surface would present stale
        # evidence as the current result.
        "final_response": (
            ticket.final_response
            if latest_run is not None and latest_run.status == "completed"
            else None
        ),
        "appendable": appendable,
        "allowed_actions": (
            ["append_message", "new_ticket"] if appendable else ["new_ticket_from_context"]
        ),
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "messages": [
            {
                "id": item.id,
                "role": "customer" if item.role == "user" else item.role,
                "content": item.content,
                "source_refs": item.source_refs,
                "created_at": item.created_at,
            }
            for item in messages
        ],
        "summary": None
        if summary is None
        else {
            "confirmed_facts": summary.confirmed_facts,
            "attempted_actions": summary.attempted_actions,
            "open_questions": summary.open_questions,
            "source_refs": summary.source_refs,
            "freshness_at": summary.freshness_at,
            "expires_at": summary.expires_at,
        },
        "latest_run": (
            await _run_projection(session, latest_run) if latest_run is not None else None
        ),
        "timeline": [_public_event_projection(item) for item in events],
        "knowledge_sources": knowledge_sources,
        "business_facts": business_facts,
        "approval": None
        if approval is None
        else {
            "id": approval.id,
            "status": approval.status,
            "action_type": approval.action_type,
        },
        "business_action": None
        if action is None
        else {
            "id": action.id,
            "status": action.status,
            "action_type": action.action_type,
            "resource_id": action.resource_id,
            "resource_version": action.resource_version,
            "result": action.result,
            "created_at": action.created_at,
        },
    }


async def _sqlite_approval_projection(
    session: ScopedAsyncSession,
    approval: ApprovalRequest,
    *,
    testing: bool,
) -> dict[str, Any]:
    action_state = await ConversationActionStateProjector(session).get_for_approval(
        tenant_id=approval.tenant_id,
        customer_id=approval.customer_id,
        approval_id=approval.id,
    )
    if action_state is None:
        raise ConversationActionStateProjectionError("approval action state is missing")
    run = await session.get(AgentRun, approval.run_id) if approval.run_id else None
    proposal = (
        await session.get(ProposalRecord, approval.proposal_id) if approval.proposal_id else None
    )
    snapshot = await session.scalar(
        select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval.id)
    )
    marker = (
        await session.get(CheckpointCommitMarker, approval.marker_id)
        if approval.marker_id
        else None
    )
    legacy_actionable = bool(
        approval.status == "pending"
        and run is not None
        and run.status == "interrupted"
        and run.checkpoint_stage == "awaiting_approval"
        and run.checkpoint_id == approval.checkpoint_id
    )
    actionable = bool(
        legacy_actionable
        and proposal is not None
        and proposal.status == "bound"
        and marker is not None
        and marker.status == "finalized"
        and run is not None
        and run.canonical_checkpoint_ns == approval.canonical_checkpoint_ns
        and run.canonical_checkpoint_hash == approval.canonical_checkpoint_hash
    )
    decision = await session.scalar(
        select(HumanDecision).where(HumanDecision.approval_id == approval.id)
    )
    action = await session.scalar(
        select(BusinessAction)
        .where(BusinessAction.approval_id == approval.id)
        .order_by(BusinessAction.created_at.desc(), BusinessAction.id.desc())
        .limit(1)
    )
    resume_job = await session.scalar(
        select(RuntimeJob)
        .where(
            RuntimeJob.run_id == approval.run_id,
            RuntimeJob.approval_id == approval.id,
            RuntimeJob.kind == "approval_resume",
        )
        .order_by(RuntimeJob.created_at.desc(), RuntimeJob.id.desc())
        .limit(1)
    )
    ticket = await session.get(SupportTicket, approval.ticket_id)
    selected_revision = (
        await session.get(ApprovalActionRevision, approval.selected_revision_id)
        if approval.selected_revision_id
        else None
    )
    selected_revision_is_valid = bool(
        selected_revision is not None
        and selected_revision.approval_id == approval.id
        and selected_revision.revision_number == approval.selected_revision_number
    )
    requested_payload = (
        selected_revision.action_payload
        if selected_revision_is_valid and selected_revision is not None
        else approval.action_payload
    )
    original_request = await session.scalar(
        select(TicketMessage.content)
        .join(
            ConversationTurn,
            and_(
                ConversationTurn.tenant_id == TicketMessage.tenant_id,
                ConversationTurn.customer_message_id == TicketMessage.id,
            ),
        )
        .where(
            ConversationTurn.tenant_id == approval.tenant_id,
            ConversationTurn.id == approval.origin_turn_id,
            ConversationTurn.ticket_id == approval.ticket_id,
            ConversationTurn.run_id == approval.run_id,
            TicketMessage.ticket_id == approval.ticket_id,
            TicketMessage.message_kind == "customer",
        )
        .limit(1)
    )
    evidence_summaries: list[dict[str, str]] = []
    citation_binding_refs = (
        [item for item in snapshot.citation_binding_refs[:20] if isinstance(item, str) and item]
        if snapshot is not None and isinstance(snapshot.citation_binding_refs, list)
        else []
    )
    if citation_binding_refs:
        evidence_rows = (
            await session.execute(
                select(
                    CitationBinding,
                    RetrievalTrace,
                    ToolObservation.payload,
                )
                .join(
                    RetrievalTrace,
                    and_(
                        RetrievalTrace.tenant_id == CitationBinding.tenant_id,
                        RetrievalTrace.run_id == CitationBinding.run_id,
                        RetrievalTrace.origin_job_id == CitationBinding.origin_job_id,
                        RetrievalTrace.logical_invocation_id == CitationBinding.tool_invocation_id,
                        RetrievalTrace.id == CitationBinding.retrieval_trace_id,
                    ),
                )
                .join(
                    ToolObservation,
                    and_(
                        ToolObservation.tenant_id == CitationBinding.tenant_id,
                        ToolObservation.run_id == CitationBinding.run_id,
                        ToolObservation.job_id == CitationBinding.origin_job_id,
                        ToolObservation.invocation_id == CitationBinding.tool_invocation_id,
                        ToolObservation.id == CitationBinding.observation_id,
                        ToolObservation.status == "ok",
                    ),
                )
                .where(
                    CitationBinding.tenant_id == approval.tenant_id,
                    CitationBinding.run_id == approval.run_id,
                    CitationBinding.id.in_(citation_binding_refs),
                )
            )
        ).all()
        evidence_by_binding: dict[str, dict[str, str]] = {}
        for binding, trace, observation_payload in evidence_rows:
            if (
                not isinstance(observation_payload, dict)
                or observation_payload.get("tool_name") != "search_knowledge"
            ):
                continue
            ordinal = binding.selected_candidate_ordinal
            selected_candidates = trace.selected_candidates
            if (
                not isinstance(selected_candidates, list)
                or ordinal < 0
                or ordinal >= len(selected_candidates)
                or not isinstance(selected_candidates[ordinal], dict)
            ):
                continue
            chunk_key = selected_candidates[ordinal].get("chunk_id")
            if not isinstance(chunk_key, str) or not chunk_key:
                continue
            chunk_row = (
                await session.execute(
                    select(KnowledgeChunk, KnowledgeDocument)
                    .join(
                        KnowledgeDocument,
                        and_(
                            KnowledgeDocument.id == KnowledgeChunk.document_id,
                            KnowledgeDocument.index_version == KnowledgeChunk.index_version,
                            KnowledgeDocument.ingest_run_id == KnowledgeChunk.ingest_run_id,
                        ),
                    )
                    .where(
                        KnowledgeChunk.chunk_key == chunk_key,
                        KnowledgeChunk.index_version == trace.index_version,
                        KnowledgeChunk.ingest_run_id == trace.corpus_snapshot_id,
                    )
                    .limit(1)
                )
            ).one_or_none()
            if chunk_row is None:
                continue
            chunk, document = chunk_row
            raw_freshness = observation_payload.get("freshness_status")
            evidence_by_binding[str(binding.id)] = {
                "title": str(document.title)[:300],
                "section_path": str(chunk.section_path)[:500],
                "version": str(document.version)[:64],
                "freshness": (
                    "current"
                    if raw_freshness == "fresh"
                    else "changed_since_proposal"
                    if raw_freshness == "stale"
                    else "unavailable"
                ),
            }
        evidence_summaries = [
            evidence_by_binding[binding_id]
            for binding_id in citation_binding_refs
            if binding_id in evidence_by_binding
        ]
    resource_facts: dict[str, Any] = {}
    if approval.action_type == "refund":
        billing = await session.scalar(
            select(BillingRecord).where(
                BillingRecord.id == approval.resource_id,
                BillingRecord.tenant_id == approval.tenant_id,
                BillingRecord.customer_id == approval.customer_id,
            )
        )
        if billing is not None:
            resource_facts = {
                "status": billing.status,
                "amount": str(billing.amount),
                "currency": billing.currency,
                "duplicate_of": billing.duplicate_of,
                "version": billing.version,
            }
    elif approval.action_type == "api_key_revocation":
        api_key = await session.scalar(
            select(ApiKeyMetadata).where(
                ApiKeyMetadata.key_id == approval.resource_id,
                ApiKeyMetadata.tenant_id == approval.tenant_id,
                ApiKeyMetadata.customer_id == approval.customer_id,
            )
        )
        if api_key is not None:
            resource_facts = {
                "status": api_key.status,
                "version": api_key.version,
            }
    elif approval.action_type == "entitlement_change":
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.id == approval.resource_id,
                Subscription.tenant_id == approval.tenant_id,
                Subscription.customer_id == approval.customer_id,
            )
        )
        if subscription is not None:
            resource_facts = {
                "status": subscription.status,
                "plan": subscription.plan,
                "rpm_limit": subscription.rpm_limit,
                "concurrency_limit": subscription.concurrency_limit,
                "version": subscription.version,
            }
    effective_actionable = (
        legacy_actionable if testing else actionable
    ) and action_state.actionable
    payload: dict[str, Any] = {
        "id": approval.id,
        "ticket_id": approval.ticket_id,
        "status": approval.status,
        "action_type": approval.action_type,
        "resource_type": approval.resource_type,
        "resource_id": approval.resource_id,
        "origin_turn_id": approval.origin_turn_id,
        "requested_change": {
            key: requested_payload[key]
            for key in ("change_type", "current", "target")
            if key in requested_payload
        }
        | (
            {"refund_reason": requested_payload["refund_reason"]}
            if selected_revision_is_valid and "refund_reason" in requested_payload
            else {}
        ),
        "original_request": (
            str(original_request)[:8000] if original_request is not None else None
        ),
        "evidence_summaries": evidence_summaries,
        "resource_facts": resource_facts,
        "risk": None if ticket is None else ticket.risk,
        "business_version": approval.business_version,
        "status_version": approval.status_version,
        "actionable": effective_actionable,
        "proposal_summary": None
        if proposal is None
        else {
            "status": proposal.status,
            "resource_version": proposal.resource_version,
        },
        "snapshot_summary": None
        if snapshot is None
        else {
            "resource_version": snapshot.resource_version,
            "policy_bound": bool(snapshot.policy_binding),
            "citation_count": len(snapshot.citation_binding_refs),
        },
        "ticket_summary": None
        if ticket is None
        else {
            "status": ticket.status,
        },
        "human_decision_summary": None
        if decision is None
        else {
            "decision": decision.decision,
            "created_at": decision.created_at,
        },
        "resume_job_summary": None
        if resume_job is None
        else {
            "status": resume_job.status,
            "verification_pending": (
                resume_job.outcome == "verification_pending"
                or resume_job.delivery_hold_reason == "state_unknown"
            ),
        },
        "business_action_summary": None
        if action is None
        else {
            "status": action.status,
            "resource_version": action.resource_version,
            "created_at": action.created_at,
        },
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "decided_at": approval.decided_at,
        "consumed_at": approval.consumed_at,
    }
    _apply_approval_action_projection(payload, action_state)
    return project_approval_detail(payload).model_dump(mode="python")


async def _sqlite_approval_source(
    session: ScopedAsyncSession,
    approval: ApprovalRequest,
    *,
    before_sequence: int | None = None,
    before_message_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any] | None:
    if not 1 <= limit <= 100 or (before_sequence is None) != (before_message_id is None):
        raise RuntimeConflict("approval_source_cursor_invalid")
    if not approval.origin_turn_id or not approval.run_id:
        return None
    ticket = await session.scalar(
        select(SupportTicket).where(
            SupportTicket.tenant_id == approval.tenant_id,
            SupportTicket.id == approval.ticket_id,
            SupportTicket.customer_id == approval.customer_id,
        )
    )
    if ticket is None:
        return None
    turn = await session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.tenant_id == approval.tenant_id,
            ConversationTurn.id == approval.origin_turn_id,
            ConversationTurn.ticket_id == approval.ticket_id,
            ConversationTurn.run_id == approval.run_id,
        )
    )
    if turn is None:
        return None
    if before_sequence is not None and before_message_id is not None:
        cursor_exists = await session.scalar(
            select(TicketMessage.id)
            .join(
                ConversationTurn,
                and_(
                    ConversationTurn.tenant_id == TicketMessage.tenant_id,
                    ConversationTurn.id == TicketMessage.turn_id,
                    ConversationTurn.ticket_id == TicketMessage.ticket_id,
                ),
            )
            .where(
                TicketMessage.tenant_id == approval.tenant_id,
                TicketMessage.ticket_id == approval.ticket_id,
                TicketMessage.id == before_message_id,
                TicketMessage.conversation_sequence == before_sequence,
                TicketMessage.message_kind.in_(("customer", "assistant")),
                ConversationTurn.ordinal <= turn.ordinal,
            )
        )
        if cursor_exists is None:
            raise RuntimeConflict("approval_source_cursor_conflict")

    source_query = (
        select(TicketMessage)
        .join(
            ConversationTurn,
            and_(
                ConversationTurn.tenant_id == TicketMessage.tenant_id,
                ConversationTurn.id == TicketMessage.turn_id,
                ConversationTurn.ticket_id == TicketMessage.ticket_id,
            ),
        )
        .where(
            TicketMessage.tenant_id == approval.tenant_id,
            TicketMessage.ticket_id == approval.ticket_id,
            TicketMessage.conversation_sequence.is_not(None),
            TicketMessage.message_kind.in_(("customer", "assistant")),
            ConversationTurn.ordinal <= turn.ordinal,
        )
    )
    if before_sequence is not None and before_message_id is not None:
        source_query = source_query.where(
            (TicketMessage.conversation_sequence < before_sequence)
            | (
                (TicketMessage.conversation_sequence == before_sequence)
                & (TicketMessage.id < before_message_id)
            )
        )
    messages_desc = (
        await session.scalars(
            source_query.order_by(
                TicketMessage.conversation_sequence.desc(),
                TicketMessage.id.desc(),
            ).limit(limit + 1)
        )
    ).all()
    has_more = len(messages_desc) > limit
    selected_messages = list(reversed(messages_desc[:limit]))
    if before_sequence is None and not any(item.turn_id == turn.id for item in selected_messages):
        return None
    next_cursor = selected_messages[0] if has_more and selected_messages else None
    return {
        "approval_id": approval.id,
        "ticket_id": ticket.id,
        "title": ticket.title or "未命名对话",
        "origin_turn_id": turn.id,
        "messages": [
            {
                "id": item.id,
                "turn_id": item.turn_id,
                "kind": item.message_kind,
                "role": ("customer" if item.message_kind == "customer" else "assistant"),
                "content": item.content[:8000],
                "sequence": item.conversation_sequence,
                "is_origin_turn": item.turn_id == turn.id,
                "created_at": item.created_at,
            }
            for item in selected_messages
        ],
        "returned": len(selected_messages),
        "has_more": has_more,
        "next_before_sequence": (
            next_cursor.conversation_sequence if next_cursor is not None else None
        ),
        "next_before_message_id": next_cursor.id if next_cursor is not None else None,
    }


def _approval_allowed_actions(action_type: str, *, actionable: bool) -> list[str]:
    if not actionable:
        return []
    actions = ["approve", "reject"]
    if action_type in {"refund", "entitlement_change"}:
        actions.insert(1, "edit_and_approve")
    return actions


def _approval_resource_summary(action_payload: dict[str, Any]) -> str:
    for field in ("billing_record_id", "api_key_id", "subscription_id", "resource_id"):
        value = action_payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "受保护资源"


def _project_action_source(
    value: object,
    *,
    tenant_id: str,
    customer_id: str | None = None,
) -> ConversationActionStateV1:
    if not isinstance(value, dict):
        raise ConversationActionStateProjectionError("action source bundle is invalid")
    sources = conversation_action_sources_from_mapping(cast(dict[str, Any], value))
    if sources.approval.tenant_id != tenant_id or (
        customer_id is not None and sources.approval.customer_id != customer_id
    ):
        raise ConversationActionStateProjectionError("action source bundle crosses request scope")
    return project_conversation_action_state(sources)


def _customer_action_payload(
    state: ConversationActionStateV1,
    raw_value: object,
) -> CustomerActionPayloadResponse:
    """Rebuild one customer card from canonical identity and display-only facts."""

    raw = raw_value if isinstance(raw_value, dict) else {}
    if state.action_type == "refund":
        amount = raw.get("amount")
        currency = raw.get("currency")
        return CustomerActionPayloadResponse(
            billing_record_id=state.resource_id,
            amount=(
                amount
                if isinstance(amount, str)
                and 0 < len(amount) <= 15
                and amount.replace(".", "", 1).isdigit()
                else None
            ),
            currency=(
                currency
                if isinstance(currency, str)
                and len(currency) == 3
                and currency.isascii()
                and currency.isalpha()
                and currency == currency.upper()
                else None
            ),
        )
    if state.action_type == "api_key_revocation":
        return CustomerActionPayloadResponse(api_key_id=state.resource_id)
    raw_target = raw.get("target")
    target_value = raw_target if isinstance(raw_target, dict) else {}
    plan = target_value.get("plan")
    rpm_limit = target_value.get("rpm_limit")
    concurrency_limit = target_value.get("concurrency_limit")
    target = CustomerEntitlementTargetResponse(
        plan=(
            plan
            if isinstance(plan, str)
            and 0 < len(plan) <= 128
            and all(character.isalnum() or character in "_.:-" for character in plan)
            else None
        ),
        rpm_limit=(
            rpm_limit
            if isinstance(rpm_limit, int)
            and not isinstance(rpm_limit, bool)
            and 0 <= rpm_limit <= 1_000_000
            else None
        ),
        concurrency_limit=(
            concurrency_limit
            if isinstance(concurrency_limit, int)
            and not isinstance(concurrency_limit, bool)
            and 0 <= concurrency_limit <= 1_000_000
            else None
        ),
    )
    return CustomerActionPayloadResponse(
        subscription_id=state.resource_id,
        target=(
            target if any(value is not None for value in target.model_dump().values()) else None
        ),
    )


def _apply_approval_action_projection(
    payload: dict[str, Any],
    state: ConversationActionStateV1,
) -> dict[str, Any]:
    if str(payload.get("id")) != state.approval_id:
        raise ConversationActionStateProjectionError(
            "approval payload identity conflicts with action state"
        )
    # The Projector owns lifecycle truth.  The pre-existing checkpoint binding
    # remains an additional approver-authority prerequisite; trusted action
    # state never grants mutation authority by itself.
    approver_actionable = bool(payload.get("actionable")) and state.actionable
    payload["status"] = state.projection_status
    payload["status_version"] = state.status_version
    payload["resource_type"] = state.resource_type
    payload["resource_id"] = state.resource_id
    payload["origin_turn_id"] = state.origin_turn_id
    payload["business_version"] = state.resource_version
    payload["actionable"] = approver_actionable
    payload["allowed_actions"] = _approval_allowed_actions(
        state.action_type,
        actionable=approver_actionable,
    )
    return payload


def _apply_conversation_action_projection(
    payload: dict[str, Any],
    states: tuple[ConversationActionStateV1, ...],
) -> dict[str, Any]:
    existing = payload.get("pending_actions")
    existing_actions = (
        [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    )
    existing_by_id = {str(item.get("id")): item for item in existing_actions}
    turn_by_approval: dict[str, str] = {}
    turns = payload.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = turn.get("id")
            messages = turn.get("messages")
            if not isinstance(turn_id, str) or not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                approval_id = message.get("approval_id")
                if isinstance(approval_id, str) and approval_id:
                    turn_by_approval.setdefault(approval_id, turn_id)

    projected: list[dict[str, Any]] = []
    for state in states:
        previous = existing_by_id.get(state.approval_id, {})
        projected.append(
            ConversationActionResponse(
                id=state.approval_id,
                turn_id=(turn_by_approval.get(state.approval_id) or state.origin_turn_id),
                status=state.projection_status,
                action_type=state.action_type,
                action_payload=_customer_action_payload(
                    state,
                    previous.get("action_payload"),
                ),
                actionable=state.actionable,
                allowed_actions=list(state.allowed_customer_actions),
                status_version=state.status_version,
                customer_safe_reason_code=state.customer_safe_reason_code,
                created_at=state.created_at,
                updated_at=state.updated_at,
            ).model_dump(mode="python", exclude_none=True)
        )
    payload["pending_actions"] = projected

    statuses = {state.projection_status for state in states}
    if "verification_pending" in statuses:
        payload["activity_label"] = "等待执行结果核验"
    elif statuses.intersection({"approved", "executing"}):
        payload["activity_label"] = "正在执行已批准操作"
    elif "pending" in statuses:
        payload["activity_label"] = "等待审批"
    return payload


def _approval_list_item(payload: dict[str, Any]) -> dict[str, Any]:
    action_payload = payload.get("action_payload")
    review_context = payload.get("review_context")
    actionable = bool(payload.get("actionable"))
    return {
        "id": str(payload["id"]),
        "ticket_id": str(payload["ticket_id"]),
        "source_label": str(payload.get("source_label") or payload["ticket_id"]),
        "status": str(payload.get("status") or "unknown"),
        "action_type": str(payload.get("action_type") or "protected_action"),
        "resource_summary": str(
            payload.get("resource_summary")
            or _approval_resource_summary(
                action_payload if isinstance(action_payload, dict) else {}
            )
        ),
        "risk": str(
            payload.get("risk")
            or (review_context.get("risk") if isinstance(review_context, dict) else None)
            or "high"
        ),
        "actionable": actionable,
        "allowed_actions": _approval_allowed_actions(
            str(payload.get("action_type") or ""),
            actionable=actionable,
        ),
        "created_at": payload["created_at"],
    }


def _conversation_activity_label(
    turns: list[ConversationTurn],
    approvals: list[ApprovalRequest],
    *,
    lifecycle: str = "active",
    automation_mode: str = "agent",
) -> str:
    latest = max(turns, key=lambda item: (item.ordinal, item.id), default=None)
    return activity_label(
        lifecycle=lifecycle,
        automation_mode=automation_mode,
        latest_result=latest.result_state if latest else None,
        has_running=any(item.activity_state == "running" for item in turns),
        has_queued=any(item.activity_state == "queued" for item in turns),
        has_pending_action=any(item.status == "pending" for item in approvals),
        has_executing_action=any(item.status in {"approved", "executing"} for item in approvals),
        has_completed_action=any(
            latest is not None and item.status == "executed" and item.run_id == latest.run_id
            for item in approvals
        ),
    )


async def _sqlite_conversation_detail(
    session: ScopedAsyncSession, ticket: SupportTicket
) -> dict[str, Any]:
    turns = (
        await session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.ticket_id == ticket.id)
            .order_by(ConversationTurn.ordinal, ConversationTurn.id)
        )
    ).all()
    messages = (
        await session.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket.id)
            .order_by(
                TicketMessage.conversation_sequence, TicketMessage.created_at, TicketMessage.id
            )
        )
    ).all()
    approvals = (
        await session.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.ticket_id == ticket.id)
            .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
        )
    ).all()
    selected_revision_ids = [
        approval.selected_revision_id
        for approval in approvals
        if approval.selected_revision_id is not None
    ]
    selected_revisions = (
        (
            await session.scalars(
                select(ApprovalActionRevision).where(
                    ApprovalActionRevision.id.in_(selected_revision_ids)
                )
            )
        ).all()
        if selected_revision_ids
        else []
    )
    selected_revision_by_approval = {
        revision.approval_id: revision for revision in selected_revisions
    }

    def display_action_payload(approval: ApprovalRequest) -> dict[str, Any]:
        revision = selected_revision_by_approval.get(approval.id)
        if (
            revision is not None
            and revision.id == approval.selected_revision_id
            and revision.tenant_id == approval.tenant_id
            and revision.proposal_id == approval.proposal_id
            and revision.revision_number == approval.selected_revision_number
            and revision.resource_version == approval.business_version
        ):
            return revision.action_payload
        return approval.action_payload

    action_states = await ConversationActionStateProjector(session).list_for_ticket(
        tenant_id=ticket.tenant_id,
        customer_id=ticket.customer_id,
        ticket_id=ticket.id,
    )
    messages_by_turn: dict[str, list[TicketMessage]] = {}
    for message in messages:
        if message.turn_id:
            messages_by_turn.setdefault(message.turn_id, []).append(message)
    projected_turns: list[dict[str, Any]] = []
    for turn in turns:
        run = await session.get(AgentRun, turn.run_id) if turn.run_id else None
        citations = await _published_knowledge_sources(session, run.id) if run else []
        projected_turns.append(
            {
                "id": turn.id,
                "ordinal": turn.ordinal,
                "activity_state": turn.activity_state,
                "result_state": turn.result_state,
                "run_id": turn.run_id,
                "messages": [
                    {
                        "id": item.id,
                        "kind": item.message_kind,
                        "role": "customer" if item.role == "user" else item.role,
                        "content": item.content,
                        "sequence": item.conversation_sequence,
                        "approval_id": item.approval_id,
                        "created_at": item.created_at,
                    }
                    for item in messages_by_turn.get(turn.id, [])
                ],
                # Keep the bounded claim-level projection intact here.  The
                # product layer applies the three-*unique-source* display cap;
                # slicing claims would let one multi-claim source hide every
                # later business fact.
                "citations": citations,
                "run": await _run_projection(session, run) if run else None,
            }
        )
    result = {
        "id": ticket.id,
        "title": ticket.title or "未命名对话",
        "lifecycle": ticket.lifecycle,
        "automation_mode": ticket.automation_mode,
        "activity_label": _conversation_activity_label(
            list(turns),
            list(approvals),
            lifecycle=ticket.lifecycle,
            automation_mode=ticket.automation_mode,
        ),
        "allowed_actions": (
            ["append_message", "archive"] if ticket.lifecycle == "active" else ["restore"]
        ),
        "turns": projected_turns,
        "pending_actions": [
            {
                "id": item.id,
                "turn_id": next((turn.id for turn in turns if turn.run_id == item.run_id), None),
                "status": item.status,
                "action_type": item.action_type,
                "action_payload": display_action_payload(item),
                "allowed_actions": ["withdraw"] if item.status == "pending" else [],
                "created_at": item.created_at,
            }
            for item in approvals
        ],
        "created_at": ticket.created_at,
        "updated_at": ticket.last_message_at,
    }
    return _apply_conversation_action_projection(result, action_states)
