import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.context import (
    PolicyCapabilityMcpCallContext,
    ReadMcpCallContext,
)
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.contracts.tools import (
    AccountResult,
    ApiKeyMetadataInput,
    ApiKeyMetadataResult,
    ApiKeyRevocationProposalInput,
    BillingRecordInput,
    BillingRecordResult,
    DraftProposalResult,
    EntitlementChangeProposalInput,
    EscalationInput,
    EscalationResult,
    IncidentImpactInput,
    IncidentImpactResult,
    RefundProposalInput,
    RefundProposalResult,
    RequestTraceInput,
    RequestTraceResult,
    ServiceStatusInput,
    ServiceStatusResult,
    SourceRef,
    SubscriptionResult,
    ToolCallContext,
    UsageInput,
    UsageResult,
)
from supportguard.db.models import (
    AgentRun,
    ApiKeyMetadata,
    ApiRequestTrace,
    ApiUsageBucket,
    ApiUsageSnapshot,
    ApprovalRequest,
    AuditEvent,
    BillingRecord,
    Customer,
    EscalationRecord,
    IncidentImpact,
    PlanCatalog,
    ProposalRecord,
    RuntimeJob,
    ServiceIncident,
    Subscription,
    SupportTicket,
    ToolInvocation,
    ToolObservation,
    TurnGroup,
    new_id,
)
from supportguard.services.approval_lifecycle import (
    ActionLifecycleService,
    canonical_approval_identity_values,
)
from supportguard.services.capability_ledger import capability_payload_hash
from supportguard.services.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

REFUND_LIMIT_USD = Decimal("500.00")

_MCP_BOUNDARY_MESSAGE_REASONS = {
    "invalid read MCP wrapper envelope": "invalid_wrapper_envelope",
    "security_definer_session_user_forbidden": "principal_rejected",
    "search_scope_unavailable": "search_scope_unavailable",
    "search_snapshot_unavailable": "search_snapshot_unavailable",
    "search_trace_origin_missing": "search_trace_origin_missing",
    "search_trace_transition_conflict": "search_trace_transition_conflict",
    "search_replay_conflict": "search_replay_conflict",
    "search_load_scope_conflict": "search_load_scope_conflict",
    "search_trace_start_binding_conflict": "search_trace_start_binding_conflict",
    "search_filter_scope_conflict": "search_filter_scope_conflict",
    "search_candidate_order_invalid": "search_candidate_order_invalid",
    "search_vector_order_invalid": "search_vector_order_invalid",
    "search_keyword_order_invalid": "search_keyword_order_invalid",
    "search_query_changed": "search_query_changed",
    "search_trace_missing": "search_trace_missing",
    "search_terminal_order_invalid": "search_terminal_order_invalid",
    "retrieval_substate_mutation_invalid": "retrieval_substate_mutation_invalid",
    "retrieval_substate_transition_invalid": "retrieval_substate_transition_invalid",
    "retrieval_terminal_receipt_invalid": "retrieval_terminal_receipt_invalid",
}


def _mcp_boundary_reason(sqlstate: str | None, original: object) -> str:
    """Return one bounded internal reason without retaining raw database text."""

    primary = str(original)
    for marker, reason in _MCP_BOUNDARY_MESSAGE_REASONS.items():
        if marker in primary:
            return reason
    return {
        "22023": "invalid_input_rejected",
        "42501": "principal_rejected",
        "55000": "state_rejected",
        "P0002": "scoped_resource_missing",
    }.get(sqlstate or "", "database_boundary_failed")


_READ_MCP_WRAPPERS = {
    name: f"supportguard_read_mcp_{name}"
    for name in (
        "query_account",
        "query_subscription",
        "query_api_usage",
        "check_service_status",
        "query_billing_record",
        "query_request_trace",
        "query_api_key_metadata",
        "query_incident_impact",
        "search_knowledge",
    )
}
_ACTION_MCP_WRAPPERS = {
    name: f"supportguard_action_mcp_{name}"
    for name in (
        "create_support_escalation",
        "propose_refund",
        "propose_api_key_revocation",
        "propose_entitlement_change",
    )
}

_MCP_NOT_FOUND_ERRORS: dict[str, tuple[ErrorCode, str]] = {
    "query_account": (
        ErrorCode.CUSTOMER_NOT_FOUND,
        "Customer is not available in the current scope",
    ),
    "query_subscription": (
        ErrorCode.SUBSCRIPTION_NOT_FOUND,
        "Subscription is not available in the current scope",
    ),
    "query_api_usage": (
        ErrorCode.USAGE_NOT_FOUND,
        "Usage is not available in the current scope",
    ),
    "query_billing_record": (
        ErrorCode.BILLING_SCOPE_VIOLATION,
        "Billing record is not available in the current scope",
    ),
    "query_request_trace": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "Request trace is not available in the current scope",
    ),
    "query_api_key_metadata": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "API Key metadata is not available in the current scope",
    ),
    "query_incident_impact": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "Incident impact is not available in the current scope",
    ),
    "create_support_escalation": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "Support ticket is not available in the current scope",
    ),
    "propose_refund": (
        ErrorCode.BILLING_SCOPE_VIOLATION,
        "Billing record is not available in the current scope",
    ),
    "propose_api_key_revocation": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "API Key is not available in the current scope",
    ),
    "propose_entitlement_change": (
        ErrorCode.TICKET_SCOPE_VIOLATION,
        "Subscription is not available in the current scope",
    ),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _usage_bucket_complete(
    bucket_bounds: list[tuple[datetime, datetime]],
    *,
    window_start: datetime,
    window_end: datetime,
    expected_count: int,
) -> bool:
    return (
        len(bucket_bounds) == expected_count
        and bool(bucket_bounds)
        and bucket_bounds[0][0] == window_start
        and bucket_bounds[-1][1] == window_end
        and len(set(bucket_bounds)) == len(bucket_bounds)
        and all(end - start == timedelta(minutes=1) for start, end in bucket_bounds)
        and all(
            left[1] == right[0]
            for left, right in zip(bucket_bounds, bucket_bounds[1:], strict=False)
        )
    )


def action_hash(payload: dict[str, Any]) -> str:
    return canonical_json_hash(payload)


class BusinessService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.session = session
        self.test_capability = test_capability

    async def _invoke_mcp_wrapper(
        self,
        context: ToolCallContext,
        *,
        method: str,
        phase: Literal["reserve", "recheck", "execute", "record_result"],
        model_arguments: dict[str, Any],
        payload_hash: str | None = None,
        execution_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mcp_context = context.mcp_context
        trusted: dict[str, Any]
        if isinstance(mcp_context, ReadMcpCallContext):
            wrapper = _READ_MCP_WRAPPERS.get(method)
            if wrapper is None or phase == "record_result":
                raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Unknown read MCP wrapper")
            trusted = {
                "schema_version": "read-mcp-wrapper.v1",
                "phase": phase,
                "tool_name": method,
                "tenant_id": context.tenant_id,
                "run_id": context.run_id,
                "job_id": context.job_id,
                "segment_id": context.segment_id,
                "fencing_token": context.fencing_token,
                "delivery_generation": context.delivery_generation,
                "logical_invocation_id": mcp_context.logical_invocation_id,
                "tool_attempt_id": mcp_context.tool_attempt_id,
                "transport_attempt_id": mcp_context.transport_attempt_id,
                "transport_ordinal": mcp_context.transport_attempt,
                "provider_tool_call_id": context.tool_call_id,
                "call_deadline": mcp_context.call_deadline.isoformat(),
                "trace_origin": mcp_context.trace_origin,
            }
            if phase == "execute" and method == "search_knowledge":
                if execution_payload is None:
                    raise DomainError(
                        ErrorCode.TICKET_STATE_CONFLICT,
                        "Knowledge execution payload is required",
                    )
                trusted["execution_payload"] = execution_payload
        elif isinstance(mcp_context, PolicyCapabilityMcpCallContext):
            wrapper = _ACTION_MCP_WRAPPERS.get(method)
            if wrapper is None:
                raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Unknown action MCP wrapper")
            trusted = {
                "schema_version": "action-mcp-wrapper.v1",
                "phase": phase,
                "capability_name": method,
                "tenant_id": context.tenant_id,
                "run_id": context.run_id,
                "job_id": context.job_id,
                "segment_id": context.segment_id,
                "fencing_token": context.fencing_token,
                "delivery_generation": context.delivery_generation,
                "invocation_id": mcp_context.capability_invocation_id,
                "attempt_id": mcp_context.capability_attempt_id,
                "sequence": mcp_context.capability_sequence,
                "effect_identity": mcp_context.effect_identity,
                "decision_hash": mcp_context.causal_decision_hash,
                "binding_hash": mcp_context.observation_binding_hash,
                "call_deadline": mcp_context.call_deadline.isoformat(),
            }
            if phase == "execute":
                if execution_payload is None:
                    raise DomainError(
                        ErrorCode.TICKET_STATE_CONFLICT,
                        "Action execution payload is required",
                    )
                trusted["execution_payload"] = execution_payload
            if phase == "record_result":
                if payload_hash is None:
                    raise DomainError(
                        ErrorCode.TICKET_STATE_CONFLICT,
                        "Capability result hash is required",
                    )
                trusted["payload_hash"] = payload_hash
        else:
            raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Missing MCP reservation")
        try:
            result = await self.session.scalar(
                text(
                    f"SELECT public.{wrapper}("  # noqa: S608
                    "CAST(:model_arguments AS jsonb),CAST(:trusted_context AS jsonb))"
                ),
                {
                    "model_arguments": json.dumps(
                        model_arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    "trusted_context": json.dumps(
                        trusted, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ),
                },
            )
        except sqlalchemy_exc.DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            boundary_reason = _mcp_boundary_reason(sqlstate, exc.orig)
            logger.warning(
                "mcp_database_boundary_rejected",
                extra={
                    "mcp_method": method,
                    "mcp_phase": phase,
                    "mcp_boundary_reason": boundary_reason,
                    "mcp_sqlstate": sqlstate,
                },
            )
            if sqlstate == "P0002":
                code, message = _MCP_NOT_FOUND_ERRORS.get(
                    method,
                    (
                        ErrorCode.TICKET_STATE_CONFLICT,
                        "MCP business resource is not available in the current scope",
                    ),
                )
                raise DomainError(
                    code,
                    message,
                    details={"sqlstate": sqlstate, "boundary_reason": boundary_reason},
                ) from exc
            if sqlstate in {"22023", "42501", "55000"}:
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT,
                    "MCP capability rejected stale, forged, or inconsistent input",
                    details={"sqlstate": sqlstate, "boundary_reason": boundary_reason},
                ) from exc
            raise
        if not isinstance(result, dict) or result.get("authorized") is not True:
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "MCP wrapper rejected a stale, forged, or consumed invocation",
            )
        return result

    async def consume_mcp_reservation(
        self,
        context: ToolCallContext,
        *,
        method: str,
        model_arguments: dict[str, Any] | None = None,
    ) -> None:
        """Atomically bind one restricted MCP process call to its durable reservation."""

        if self.test_capability is not None:
            return
        await self._invoke_mcp_wrapper(
            context,
            method=method,
            phase="reserve",
            model_arguments=model_arguments or {},
        )

    async def execute_mcp_tool(
        self,
        context: ToolCallContext,
        *,
        method: str,
        model_arguments: dict[str, Any],
        execution_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        wrapper_result = await self._invoke_mcp_wrapper(
            context,
            method=method,
            phase="execute",
            model_arguments=model_arguments,
            execution_payload=execution_payload,
        )
        payload = wrapper_result.get("result")
        if not isinstance(payload, dict):
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "MCP wrapper returned an invalid business result",
            )
        return payload

    async def record_capability_effect(
        self,
        context: ToolCallContext,
        *,
        payload: dict[str, Any],
    ) -> None:
        """Bind an Action MCP business effect and its result in one transaction."""

        if self.test_capability is not None:
            return
        mcp_context = context.mcp_context
        if not isinstance(mcp_context, PolicyCapabilityMcpCallContext):
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "Capability effect requires a policy capability context",
            )
        await self._invoke_mcp_wrapper(
            context,
            method=mcp_context.capability_name,
            phase="record_result",
            model_arguments=payload,
            payload_hash=capability_payload_hash(payload),
        )

    async def query_account(self, context: ToolCallContext) -> AccountResult:
        customer = await self.session.scalar(
            select(Customer).where(
                Customer.id == context.customer_id,
                Customer.tenant_id == context.tenant_id,
            )
        )
        if customer is None:
            raise DomainError(ErrorCode.CUSTOMER_NOT_FOUND, "Customer was not found")

        return AccountResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            account_status=customer.status,
            security_status=customer.security_status,
            region=customer.region,
            observed_at=customer.updated_at,
            resource_version=str(customer.version),
            source_refs=[self._source_ref("customer", customer.id, customer.updated_at)],
        )

    async def query_api_usage(self, context: ToolCallContext, arguments: UsageInput) -> UsageResult:
        window_minutes: dict[str, int] = {"1m": 1, "5m": 5, "1h": 60, "24h": 1440}
        freshness_thresholds = {"1m": 120, "5m": 300, "1h": 900, "24h": 3600}
        logical_time = utc_now()
        if isinstance(context.mcp_context, ReadMcpCallContext):
            invocation = await self.session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.tenant_id == context.tenant_id,
                    ToolInvocation.run_id == context.run_id,
                    ToolInvocation.job_id == context.job_id,
                    ToolInvocation.logical_invocation_id
                    == context.mcp_context.logical_invocation_id,
                )
            )
            if invocation is None:
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT,
                    "Usage invocation logical time is unavailable",
                )
            logical_time = invocation.logical_time_utc
        logical_time = _as_utc(logical_time)
        window_end = logical_time.replace(second=0, microsecond=0)
        window_start = window_end - timedelta(minutes=window_minutes[arguments.window])
        buckets = (
            await self.session.scalars(
                select(ApiUsageBucket)
                .where(
                    ApiUsageBucket.tenant_id == context.tenant_id,
                    ApiUsageBucket.customer_id == context.customer_id,
                    ApiUsageBucket.bucket_end > window_start,
                    ApiUsageBucket.bucket_end <= window_end,
                )
                .order_by(
                    ApiUsageBucket.bucket_start,
                    ApiUsageBucket.bucket_end,
                    ApiUsageBucket.id,
                )
            )
        ).all()
        snapshot = await self.session.scalar(
            select(ApiUsageSnapshot)
            .where(
                ApiUsageSnapshot.tenant_id == context.tenant_id,
                ApiUsageSnapshot.customer_id == context.customer_id,
                ApiUsageSnapshot.observed_at <= logical_time,
            )
            .order_by(ApiUsageSnapshot.observed_at.desc())
            .limit(1)
        )
        if snapshot is None:
            raise DomainError(ErrorCode.USAGE_NOT_FOUND, "API usage snapshot was not found")
        subscription = await self.session.scalar(
            select(Subscription).where(
                Subscription.tenant_id == context.tenant_id,
                Subscription.customer_id == context.customer_id,
            )
        )
        if subscription is None:
            raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND, "Subscription was not found")
        bucket_bounds = [(_as_utc(item.bucket_start), _as_utc(item.bucket_end)) for item in buckets]
        expected_count = window_minutes[arguments.window]
        complete = _usage_bucket_complete(
            bucket_bounds,
            window_start=window_start,
            window_end=window_end,
            expected_count=expected_count,
        )
        coverage_through = bucket_bounds[-1][1] if buckets else window_start
        observed_at = min(
            coverage_through,
            _as_utc(snapshot.observed_at),
        )
        freshness_seconds = int((logical_time - observed_at).total_seconds())
        freshness_status: Literal["fresh", "stale", "unknown"]
        if not complete or freshness_seconds < 0:
            freshness_status = "unknown"
        elif freshness_seconds <= freshness_thresholds[arguments.window]:
            freshness_status = "fresh"
        else:
            freshness_status = "stale"
        resource_payload = {
            "bucket_sources": [[item.id, item.source_version] for item in buckets],
            "balance_source": snapshot.id,
            "subscription_version": subscription.version,
            "window": arguments.window,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        }
        resource_version = canonical_json_hash(resource_payload)
        source_refs = [
            self._source_ref("api_usage_snapshot", snapshot.id, snapshot.observed_at),
            # ``updated_at`` is the last mutation time, not the observation
            # time of this authoritative read.  The immutable version remains
            # part of ``resource_version`` while the source is observed now.
            self._source_ref("subscription", subscription.id, logical_time),
        ]
        if buckets:
            source_refs.extend(
                [
                    self._source_ref("api_usage_bucket", buckets[0].id, buckets[0].bucket_end),
                    self._source_ref("api_usage_bucket", buckets[-1].id, buckets[-1].bucket_end),
                ]
            )
        return UsageResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            window=arguments.window,
            window_start=window_start,
            window_end=window_end,
            request_count=sum(item.request_count for item in buckets),
            input_token_count=sum(item.input_token_count for item in buckets),
            output_token_count=sum(item.output_token_count for item in buckets),
            concurrency_current=(buckets[-1].concurrency_end if buckets else 0),
            concurrency_peak=max((item.concurrency_peak for item in buckets), default=0),
            remaining_balance=snapshot.remaining_balance,
            balance_currency=subscription.currency,
            freshness_seconds=freshness_seconds,
            freshness_status=freshness_status,
            observed_at=observed_at,
            resource_version=resource_version,
            source_refs=source_refs,
        )

    async def query_subscription(self, context: ToolCallContext) -> SubscriptionResult:
        subscription = await self.session.scalar(
            select(Subscription).where(
                Subscription.tenant_id == context.tenant_id,
                Subscription.customer_id == context.customer_id,
            )
        )
        if subscription is None:
            raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND, "Subscription was not found")
        return SubscriptionResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            subscription_id=subscription.id,
            plan=subscription.plan,
            status=subscription.status,
            rpm_limit=subscription.rpm_limit,
            concurrency_limit=subscription.concurrency_limit,
            catalog_eligibility=["quota_change", "plan_change"],
            version=subscription.version,
            source_refs=[self._source_ref("subscription", subscription.id)],
        )

    async def query_request_trace(
        self, context: ToolCallContext, arguments: RequestTraceInput
    ) -> RequestTraceResult:
        trace = await self.session.scalar(
            select(ApiRequestTrace).where(
                ApiRequestTrace.tenant_id == context.tenant_id,
                ApiRequestTrace.customer_id == context.customer_id,
                ApiRequestTrace.request_id == arguments.request_id,
            )
        )
        if trace is None:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "Request trace is unavailable")
        return RequestTraceResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            request_id=trace.request_id,
            model=trace.model,
            region=trace.region,
            status_code=trace.status_code,
            error_class=trace.error_class,
            stage_latency_ms={str(k): int(v) for k, v in trace.stage_latency_ms.items()},
            observed_at=trace.observed_at,
            version=trace.version,
            source_refs=[self._source_ref("api_request_trace", trace.id, trace.observed_at)],
        )

    async def query_api_key_metadata(
        self, context: ToolCallContext, arguments: ApiKeyMetadataInput
    ) -> ApiKeyMetadataResult:
        metadata = await self.session.scalar(
            select(ApiKeyMetadata).where(
                ApiKeyMetadata.tenant_id == context.tenant_id,
                ApiKeyMetadata.customer_id == context.customer_id,
                (ApiKeyMetadata.key_id == arguments.api_key_ref)
                | (ApiKeyMetadata.fingerprint == arguments.api_key_ref),
            )
        )
        if metadata is None:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "API Key metadata is unavailable")
        return ApiKeyMetadataResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            api_key_id=metadata.key_id,
            fingerprint=metadata.fingerprint,
            status=metadata.status,
            version=metadata.version,
            last_used_summary=metadata.last_used_summary,
            source_refs=[self._source_ref("api_key_metadata", metadata.id)],
        )

    async def query_incident_impact(
        self, context: ToolCallContext, arguments: IncidentImpactInput
    ) -> IncidentImpactResult:
        trace = await self.session.scalar(
            select(ApiRequestTrace).where(
                ApiRequestTrace.tenant_id == context.tenant_id,
                ApiRequestTrace.customer_id == context.customer_id,
                ApiRequestTrace.request_id == arguments.request_id,
            )
        )
        if trace is None:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "Incident impact is unavailable")
        impact = await self.session.scalar(
            select(IncidentImpact).where(
                IncidentImpact.tenant_id == context.tenant_id,
                IncidentImpact.request_trace_id == trace.id,
            )
        )
        observed_at = utc_now()
        return IncidentImpactResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            request_id=trace.request_id,
            impacted=None if impact is None else impact.impacted,
            incident_id=None if impact is None else impact.incident_id,
            public_incident_ref=None if impact is None else impact.public_incident_ref,
            observed_at=observed_at,
            source_refs=[
                self._source_ref(
                    "incident_impact",
                    trace.id if impact is None else impact.id,
                    observed_at,
                )
            ],
        )

    async def check_service_status(
        self, context: ToolCallContext, arguments: ServiceStatusInput
    ) -> ServiceStatusResult:
        incident = await self.session.scalar(
            select(ServiceIncident)
            .where(
                ServiceIncident.model == arguments.model,
                ServiceIncident.region == arguments.region,
                ServiceIncident.status != "resolved",
            )
            .order_by(ServiceIncident.started_at.desc())
            .limit(1)
        )
        observed_at = utc_now()
        if incident is None:
            return ServiceStatusResult(
                tool_call_id=context.tool_call_id,
                ticket_id=context.ticket_id,
                model=arguments.model,
                region=arguments.region,
                status="operational",
                summary="No active incident is recorded for this model and region.",
                observed_at=observed_at,
                source_refs=[
                    self._source_ref(
                        "service_status_query", f"{arguments.model}:{arguments.region}", observed_at
                    )
                ],
            )

        return ServiceStatusResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            model=incident.model,
            region=incident.region,
            status=incident.status,
            summary=incident.summary,
            observed_at=observed_at,
            source_refs=[self._source_ref("service_incident", incident.id, observed_at)],
        )

    async def query_billing_record(
        self, context: ToolCallContext, arguments: BillingRecordInput
    ) -> BillingRecordResult:
        billing = await self.session.scalar(
            select(BillingRecord).where(
                BillingRecord.id == arguments.billing_record_id,
                BillingRecord.tenant_id == context.tenant_id,
                BillingRecord.customer_id == context.customer_id,
            )
        )
        if billing is None:
            raise DomainError(
                ErrorCode.BILLING_SCOPE_VIOLATION,
                "Billing record is not available in the current scope",
            )
        return BillingRecordResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            billing_record_id=billing.id,
            amount=billing.amount,
            currency=billing.currency,
            status=billing.status,
            duplicate_of=billing.duplicate_of,
            version=billing.version,
            source_refs=[self._source_ref("billing_record", billing.id)],
        )

    async def create_support_escalation(
        self, context: ToolCallContext, arguments: EscalationInput
    ) -> EscalationResult:
        await self.assert_fenced_context(context)
        ticket = await self._get_scoped_ticket(context)
        existing = await self.session.scalar(
            select(EscalationRecord).where(
                EscalationRecord.tenant_id == context.tenant_id,
                EscalationRecord.idempotency_key == arguments.idempotency_key,
            )
        )
        if existing is not None:
            if existing.ticket_id != ticket.id or existing.customer_id != context.customer_id:
                raise DomainError(
                    ErrorCode.TICKET_SCOPE_VIOLATION,
                    "Idempotency key belongs to another scoped resource",
                )
            return self._escalation_result(context, existing)

        self._assert_actionable_ticket(ticket)

        escalation = EscalationRecord(
            id=new_id("esc"),
            tenant_id=context.tenant_id,
            ticket_id=ticket.id,
            customer_id=context.customer_id,
            reason=arguments.reason,
            status="open",
            idempotency_key=arguments.idempotency_key,
        )
        self.session.add(escalation)
        # The test-only synchronous v1.1 facade preserves its historical behavior.
        # In a real MCP process the record is an inert policy capability result;
        # only the fenced finalizer may advance canonical ticket state and events.
        if self.test_capability is not None:
            ticket.status = "manual_takeover"
            ticket.version += 1
            self._audit(
                context,
                "support_escalation_created",
                {"escalation_id": escalation.id, "reason": arguments.reason},
            )
        await self.session.flush()
        return self._escalation_result(context, escalation)

    async def propose_refund(
        self, context: ToolCallContext, arguments: RefundProposalInput
    ) -> RefundProposalResult:
        ticket = await self._get_scoped_ticket(context)
        existing = await self.session.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == context.tenant_id,
                ApprovalRequest.idempotency_key == arguments.idempotency_key,
            )
        )
        if existing is not None:
            if existing.ticket_id != ticket.id or existing.customer_id != context.customer_id:
                raise DomainError(
                    ErrorCode.TICKET_SCOPE_VIOLATION,
                    "Idempotency key belongs to another scoped resource",
                )
            return self._refund_result(context, existing)

        self._assert_actionable_ticket(ticket)

        billing = await self.session.scalar(
            select(BillingRecord).where(
                BillingRecord.id == arguments.billing_record_id,
                BillingRecord.tenant_id == context.tenant_id,
                BillingRecord.customer_id == context.customer_id,
            )
        )
        if billing is None:
            raise DomainError(
                ErrorCode.BILLING_SCOPE_VIOLATION,
                "Billing record is not available in the current scope",
            )
        if billing.customer_id != context.customer_id:
            raise DomainError(ErrorCode.BILLING_SCOPE_VIOLATION, "Billing record is out of scope")
        if billing.status != "charged":
            raise DomainError(ErrorCode.BILLING_NOT_CHARGED, "Billing record is not refundable")
        await self._require_observation_binding(
            context,
            tool_name="query_billing_record",
            resource_field="billing_record_id",
            resource_id=billing.id,
            resource_version=billing.version,
            require_policy_evidence=True,
        )

        linked_duplicate = await self.session.scalar(
            select(BillingRecord.id)
            .where(
                BillingRecord.tenant_id == context.tenant_id,
                BillingRecord.customer_id == context.customer_id,
                BillingRecord.duplicate_of == billing.id,
            )
            .limit(1)
        )
        if billing.duplicate_of is None and linked_duplicate is None:
            raise DomainError(
                ErrorCode.NOT_DUPLICATE_CHARGE,
                "Billing record has no explicit duplicate relationship",
            )
        if billing.currency != "USD" or billing.amount > REFUND_LIMIT_USD:
            raise DomainError(
                ErrorCode.REFUND_LIMIT_EXCEEDED,
                "Refund proposal exceeds the automatic review boundary",
                details={"limit": str(REFUND_LIMIT_USD), "currency": "USD"},
            )

        if not context.run_id or not context.checkpoint_id:
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Refund proposal requires a bound Agent Run and checkpoint",
            )
        run = await self.session.scalar(
            select(AgentRun).where(
                AgentRun.id == context.run_id,
                AgentRun.tenant_id == context.tenant_id,
                AgentRun.ticket_id == ticket.id,
                AgentRun.customer_id == context.customer_id,
            )
        )
        if run is None:
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Refund proposal has no canonical Agent Run",
            )
        identity = canonical_approval_identity_values(
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            action_type="refund",
            resource_id=billing.id,
            resource_version=billing.version,
            run=run,
        )
        active = await ActionLifecycleService(self.session).find_active(identity, lock=True)
        if active is not None:
            return self._refund_result(context, active)
        payload: dict[str, str | int] = {
            "billing_record_id": billing.id,
            "customer_id": context.customer_id,
            "amount": str(billing.amount),
            "currency": billing.currency,
            "refund_reason": arguments.refund_reason,
            "business_version": billing.version,
        }
        candidate_hash = action_hash(payload)
        proposal_identity = action_hash(
            {
                "tenant_id": context.tenant_id,
                "run_id": context.run_id,
                "action_type": "refund",
                "resource_id": billing.id,
                "resource_version": billing.version,
                "candidate_hash": candidate_hash,
            }
        )
        proposal = await self.session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.tenant_id == context.tenant_id,
                ProposalRecord.proposal_identity == proposal_identity,
            )
        )
        if proposal is None:
            proposal = ProposalRecord(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                proposal_identity=proposal_identity,
                action_type="refund",
                resource_id=billing.id,
                resource_version=billing.version,
                action_payload=payload,
                observation_binding=context.observation_binding,
                action_hash=candidate_hash,
                status="bound",
            )
            self.session.add(proposal)
            await self.session.flush()
        approval = ApprovalRequest(
            id=new_id("approval"),
            tenant_id=context.tenant_id,
            ticket_id=ticket.id,
            customer_id=context.customer_id,
            proposal_id=proposal.id,
            run_id=context.run_id,
            checkpoint_id=context.checkpoint_id,
            action_type="refund",
            resource_type=identity.resource_type,
            resource_id=identity.resource_id,
            origin_turn_id=identity.origin_turn_id,
            action_payload=payload,
            action_hash=proposal.action_hash,
            business_version=billing.version,
            status="pending",
            idempotency_key=arguments.idempotency_key,
        )
        self.session.add(approval)
        ticket.status = "awaiting_approval"
        ticket.risk = "high"
        ticket.version += 1
        self._audit(
            context,
            "refund_proposed",
            {"approval_id": approval.id, "billing_record_id": billing.id},
        )
        await self.session.flush()
        return self._refund_result(context, approval)

    async def propose_refund_draft(
        self, context: ToolCallContext, arguments: RefundProposalInput
    ) -> DraftProposalResult:
        """v1.2 policy-only proposal: durable but not actionable before segment finalize."""

        await self.assert_fenced_context(context)
        await self._get_scoped_ticket(context)
        billing = await self.session.scalar(
            select(BillingRecord).where(
                BillingRecord.id == arguments.billing_record_id,
                BillingRecord.tenant_id == context.tenant_id,
                BillingRecord.customer_id == context.customer_id,
            )
        )
        if billing is None:
            raise DomainError(
                ErrorCode.BILLING_SCOPE_VIOLATION,
                "Billing record is not available in the current scope",
            )
        if billing.customer_id != context.customer_id:
            raise DomainError(ErrorCode.BILLING_SCOPE_VIOLATION, "Billing record is out of scope")
        if billing.status != "charged":
            raise DomainError(ErrorCode.BILLING_NOT_CHARGED, "Billing record is not refundable")
        await self._require_observation_binding(
            context,
            tool_name="query_billing_record",
            resource_field="billing_record_id",
            resource_id=billing.id,
            resource_version=billing.version,
            require_policy_evidence=True,
        )
        duplicate = await self.session.scalar(
            select(BillingRecord.id).where(
                BillingRecord.tenant_id == context.tenant_id,
                BillingRecord.customer_id == context.customer_id,
                (BillingRecord.id == billing.duplicate_of)
                | (BillingRecord.duplicate_of == billing.id),
            )
        )
        if duplicate is None:
            raise DomainError(ErrorCode.NOT_DUPLICATE_CHARGE, "No duplicate relation exists")
        if billing.currency != "USD" or billing.amount > REFUND_LIMIT_USD:
            raise DomainError(
                ErrorCode.REFUND_LIMIT_EXCEEDED,
                "Refund proposal exceeds the automatic review boundary",
            )
        payload: dict[str, str | int] = {
            "billing_record_id": billing.id,
            "customer_id": context.customer_id,
            "amount": str(billing.amount),
            "currency": billing.currency,
            "refund_reason": arguments.refund_reason,
            "business_version": billing.version,
        }
        candidate_hash = action_hash(payload)
        identity = action_hash(
            {
                "tenant_id": context.tenant_id,
                "run_id": context.run_id,
                "action_type": "refund",
                "resource_id": billing.id,
                "resource_version": billing.version,
                "candidate_hash": candidate_hash,
            }
        )
        existing = await self.session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.tenant_id == context.tenant_id,
                ProposalRecord.proposal_identity == identity,
            )
        )
        if existing is None:
            existing = ProposalRecord(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                proposal_identity=identity,
                action_type="refund",
                resource_id=billing.id,
                resource_version=billing.version,
                action_payload=payload,
                observation_binding=context.observation_binding,
                action_hash=candidate_hash,
                status="draft",
            )
            self.session.add(existing)
            await self.session.flush()
        return DraftProposalResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            proposal_id=existing.id,
            status="draft",
            action_type="refund",
            action_hash=existing.action_hash,
            resource_id=existing.resource_id,
            resource_version=existing.resource_version,
            idempotency_key=arguments.idempotency_key,
            source_refs=[self._source_ref("proposal_record", existing.id)],
        )

    async def propose_api_key_revocation_draft(
        self, context: ToolCallContext, arguments: ApiKeyRevocationProposalInput
    ) -> DraftProposalResult:
        await self.assert_fenced_context(context)
        await self._get_scoped_ticket(context)
        metadata = await self.session.scalar(
            select(ApiKeyMetadata).where(
                ApiKeyMetadata.tenant_id == context.tenant_id,
                ApiKeyMetadata.customer_id == context.customer_id,
                ApiKeyMetadata.key_id == arguments.api_key_id,
            )
        )
        if metadata is None or metadata.status != "active":
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "API Key is not revocable")
        await self._require_observation_binding(
            context,
            tool_name="query_api_key_metadata",
            resource_field="api_key_id",
            resource_id=metadata.key_id,
            resource_version=metadata.version,
            require_policy_evidence=True,
        )
        payload: dict[str, Any] = {
            "api_key_id": metadata.key_id,
            "fingerprint": metadata.fingerprint,
            "customer_id": context.customer_id,
            "reason": arguments.reason,
            "business_version": metadata.version,
        }
        return await self._create_draft(
            context=context,
            action_type="api_key_revocation",
            resource_id=metadata.key_id,
            resource_version=metadata.version,
            payload=payload,
            idempotency_key=arguments.idempotency_key,
        )

    async def propose_entitlement_change_draft(
        self, context: ToolCallContext, arguments: EntitlementChangeProposalInput
    ) -> DraftProposalResult:
        await self.assert_fenced_context(context)
        await self._get_scoped_ticket(context)
        subscription = await self.session.scalar(
            select(Subscription).where(
                Subscription.id == arguments.subscription_id,
                Subscription.tenant_id == context.tenant_id,
                Subscription.customer_id == context.customer_id,
            )
        )
        if subscription is None:
            raise DomainError(ErrorCode.SUBSCRIPTION_NOT_FOUND, "Subscription was not found")
        await self._require_observation_binding(
            context,
            tool_name="query_subscription",
            resource_field="subscription_id",
            resource_id=subscription.id,
            resource_version=subscription.version,
            require_policy_evidence=True,
        )
        target = dict(arguments.target)
        if arguments.change_type == "quota_change":
            if set(target) not in ({"rpm_limit"}, {"concurrency_limit"}):
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT, "Exactly one quota target is required"
                )
            catalog = await self.session.scalar(
                select(PlanCatalog)
                .where(PlanCatalog.plan == subscription.plan)
                .order_by(PlanCatalog.version.desc())
                .limit(1)
            )
            if catalog is None:
                raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Catalog is unavailable")
            if (
                "rpm_limit" in target
                and not catalog.min_rpm <= int(target["rpm_limit"]) <= catalog.max_rpm
            ):
                raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "RPM target is outside Catalog")
            if "concurrency_limit" in target and not (
                catalog.min_concurrency
                <= int(target["concurrency_limit"])
                <= catalog.max_concurrency
            ):
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT, "Concurrency target is outside Catalog"
                )
        elif set(target) != {"plan"} or not str(target["plan"]):
            raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "A target plan is required")
        payload = {
            "subscription_id": subscription.id,
            "customer_id": context.customer_id,
            "change_type": arguments.change_type,
            "current": {
                "plan": subscription.plan,
                "rpm_limit": subscription.rpm_limit,
                "concurrency_limit": subscription.concurrency_limit,
            },
            "target": target,
            "reason": arguments.reason,
            "business_version": subscription.version,
        }
        return await self._create_draft(
            context=context,
            action_type="entitlement_change",
            resource_id=subscription.id,
            resource_version=subscription.version,
            payload=payload,
            idempotency_key=arguments.idempotency_key,
        )

    async def _create_draft(
        self,
        *,
        context: ToolCallContext,
        action_type: str,
        resource_id: str,
        resource_version: int,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> DraftProposalResult:
        candidate_hash = action_hash(payload)
        identity = action_hash(
            {
                "tenant_id": context.tenant_id,
                "run_id": context.run_id,
                "action_type": action_type,
                "resource_id": resource_id,
                "resource_version": resource_version,
                "candidate_hash": candidate_hash,
            }
        )
        proposal = await self.session.scalar(
            select(ProposalRecord).where(
                ProposalRecord.tenant_id == context.tenant_id,
                ProposalRecord.proposal_identity == identity,
            )
        )
        if proposal is None:
            proposal = ProposalRecord(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                proposal_identity=identity,
                action_type=action_type,
                resource_id=resource_id,
                resource_version=resource_version,
                action_payload=payload,
                observation_binding=context.observation_binding,
                action_hash=candidate_hash,
                status="draft",
            )
            self.session.add(proposal)
            await self.session.flush()
        return DraftProposalResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            proposal_id=proposal.id,
            status="draft",
            action_type=action_type,
            action_hash=proposal.action_hash,
            resource_id=proposal.resource_id,
            resource_version=proposal.resource_version,
            idempotency_key=idempotency_key,
            source_refs=[self._source_ref("proposal_record", proposal.id)],
        )

    async def _get_scoped_ticket(self, context: ToolCallContext) -> SupportTicket:
        ticket = await self.session.scalar(
            select(SupportTicket).where(
                SupportTicket.id == context.ticket_id,
                SupportTicket.tenant_id == context.tenant_id,
                SupportTicket.customer_id == context.customer_id,
            )
        )
        if ticket is None:
            raise DomainError(
                ErrorCode.TICKET_SCOPE_VIOLATION,
                "Support ticket is not available in the current scope",
            )
        if ticket.customer_id != context.customer_id:
            raise DomainError(ErrorCode.TICKET_SCOPE_VIOLATION, "Support ticket is out of scope")
        return ticket

    async def assert_fenced_context(self, context: ToolCallContext) -> None:
        if self.test_capability is not None:
            return
        if context.job_id is None:
            return
        if context.fencing_token is None:
            raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Missing MCP fence")
        bind = self.session.get_bind()
        username = bind.engine.url.username if bind.dialect.name == "postgresql" else None
        if username in {"supportguard_read_mcp", "supportguard_action_mcp"}:
            mcp_context = context.mcp_context
            method = (
                mcp_context.tool_name
                if isinstance(mcp_context, ReadMcpCallContext)
                else mcp_context.capability_name
                if isinstance(mcp_context, PolicyCapabilityMcpCallContext)
                else ""
            )
            await self._invoke_mcp_wrapper(
                context,
                method=method,
                phase="recheck",
                model_arguments={},
            )
            return
        job = await self.session.scalar(
            select(RuntimeJob)
            .where(
                RuntimeJob.id == context.job_id,
                RuntimeJob.run_id == context.run_id,
                RuntimeJob.tenant_id == context.tenant_id,
                RuntimeJob.status == "leased",
                RuntimeJob.fencing_token == context.fencing_token,
                RuntimeJob.lease_owner.is_not(None),
                RuntimeJob.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
        run = await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == context.run_id,
                AgentRun.tenant_id == context.tenant_id,
                AgentRun.active_job_id == context.job_id,
                AgentRun.active_fencing_token == context.fencing_token,
            )
            .with_for_update()
        )
        if job is None or run is None:
            raise DomainError(ErrorCode.TICKET_STATE_CONFLICT, "Stale MCP fence")

    async def _require_observation_binding(
        self,
        context: ToolCallContext,
        *,
        tool_name: str,
        resource_field: str,
        resource_id: str,
        resource_version: int,
        require_policy_evidence: bool,
    ) -> None:
        if self.test_capability is not None:
            return
        if context.job_id is None:
            return
        matches = [
            item
            for item in context.observation_binding
            if item.get("tool_name") == tool_name
            and item.get("status") == "ok"
            and item.get("resource_id") == resource_id
            and int(item.get("resource_version", -1)) == resource_version
            and item.get("resource_field") == resource_field
        ]
        policy = [
            item
            for item in context.observation_binding
            if item.get("tool_name") == "search_knowledge"
            and item.get("status") == "ok"
            and bool(item.get("source_refs"))
        ]
        if len(matches) != 1 or (require_policy_evidence and not policy):
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "Proposal is not bound to the required current-run observations",
            )
        required = [matches[0], *(policy[-1:] if require_policy_evidence else [])]
        for item in required:
            invocation_id = item.get("invocation_id")
            observation_id = item.get("observation_id")
            content_hash = item.get("observation_content_hash")
            turn_group_id = item.get("turn_group_id")
            if not all(
                isinstance(value, str) and value
                for value in (invocation_id, observation_id, content_hash, turn_group_id)
            ):
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT,
                    "Proposal observation binding has no durable ledger identity",
                )
            row = await self.session.scalar(
                select(ToolObservation)
                .join(ToolInvocation, ToolInvocation.id == ToolObservation.invocation_id)
                .join(TurnGroup, TurnGroup.id == ToolInvocation.turn_group_id)
                .where(
                    ToolObservation.id == observation_id,
                    ToolObservation.invocation_id == invocation_id,
                    ToolObservation.content_hash == content_hash,
                    ToolObservation.tenant_id == context.tenant_id,
                    ToolObservation.run_id == context.run_id,
                    ToolObservation.job_id == context.job_id,
                    ToolObservation.segment_id == context.segment_id,
                    ToolObservation.fencing_token == context.fencing_token,
                    ToolObservation.status == "ok",
                    ToolInvocation.tool_name == item.get("tool_name"),
                    ToolInvocation.lifecycle == "terminal",
                    ToolInvocation.outcome == "succeeded",
                    TurnGroup.id == turn_group_id,
                    TurnGroup.status == "closed",
                )
            )
            if row is None:
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT,
                    "Proposal observation binding does not resolve to a closed current-run ledger",
                )
            payload = row.payload
            if item.get("tool_name") == tool_name:
                data = payload.get("data", {})
                if (
                    str(data.get(resource_field, "")) != resource_id
                    or int(data.get("version", -1)) != resource_version
                ):
                    raise DomainError(
                        ErrorCode.TICKET_STATE_CONFLICT,
                        "Proposal observation payload no longer matches the target resource",
                    )
            elif item.get("tool_name") == "search_knowledge" and not payload.get("source_refs"):
                raise DomainError(
                    ErrorCode.TICKET_STATE_CONFLICT,
                    "Proposal policy observation has no persisted source reference",
                )

    @staticmethod
    def _assert_actionable_ticket(ticket: SupportTicket) -> None:
        if ticket.status not in {"open", "running", "needs_clarification"}:
            raise DomainError(
                ErrorCode.TICKET_STATE_CONFLICT,
                "Support ticket cannot accept this action in its current state",
                details={"status": ticket.status},
            )

    def _audit(self, context: ToolCallContext, event_type: str, payload: dict[str, str]) -> None:
        self.session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                ticket_id=context.ticket_id,
                customer_id=context.customer_id,
                event_type=event_type,
                actor_type="agent_runtime",
                actor_id=None,
                payload=payload,
                trace_id=context.trace_id,
                run_id=context.run_id,
                created_at=utc_now(),
            )
        )

    @staticmethod
    def _source_ref(
        resource_type: str, resource_id: str, observed_at: datetime | None = None
    ) -> SourceRef:
        return SourceRef(
            source_type="business_record",
            source_id=f"{resource_type}:{resource_id}",
            observed_at=observed_at or utc_now(),
        )

    @staticmethod
    def _escalation_result(
        context: ToolCallContext, escalation: EscalationRecord
    ) -> EscalationResult:
        return EscalationResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            escalation_id=escalation.id,
            status=escalation.status,
            idempotency_key=escalation.idempotency_key,
            source_refs=[BusinessService._source_ref("escalation_record", escalation.id)],
        )

    @staticmethod
    def _refund_result(context: ToolCallContext, approval: ApprovalRequest) -> RefundProposalResult:
        payload = approval.action_payload
        if (
            approval.proposal_id is None
            or approval.run_id is None
            or approval.checkpoint_id is None
        ):
            raise DomainError(
                ErrorCode.APPROVAL_BINDING_INVALID,
                "Approval is missing its Proposal or Agent Run binding",
            )
        return RefundProposalResult(
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            proposal_id=approval.proposal_id,
            approval_id=approval.id,
            status="pending",
            billing_record_id=str(payload["billing_record_id"]),
            amount=Decimal(str(payload["amount"])),
            currency=str(payload["currency"]),
            action_hash=approval.action_hash,
            business_version=approval.business_version,
            idempotency_key=approval.idempotency_key,
            run_id=approval.run_id,
            checkpoint_id=approval.checkpoint_id,
            source_refs=[BusinessService._source_ref("approval_request", approval.id)],
        )
