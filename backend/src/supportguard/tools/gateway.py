from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, create_model, model_validator

from supportguard.contracts.context import ReadMcpCallContext
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
    IncidentImpactInput,
    IncidentImpactResult,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    NoArguments,
    ObservationEnvelope,
    ObservationStatus,
    RefundProposalInput,
    RefundProposalResult,
    RequestTraceInput,
    RequestTraceResult,
    ServiceStatusInput,
    ServiceStatusResult,
    SubscriptionResult,
    ToolCallContext,
    UsageInput,
    UsageResult,
)
from supportguard.mcp.client import structured_result
from supportguard.mcp.runtime import MCPTransportFailure, ToolTransport

logger = logging.getLogger(__name__)


ReadToolName = Literal[
    "query_account",
    "query_subscription",
    "query_api_usage",
    "check_service_status",
    "query_billing_record",
    "query_request_trace",
    "query_api_key_metadata",
    "query_incident_impact",
    "search_knowledge",
]
ActionToolName = Literal[
    "propose_refund",
    "propose_api_key_revocation",
    "propose_entitlement_change",
]

READ_TOOL_ARGUMENTS: dict[ReadToolName, type[BaseModel]] = {
    "query_account": NoArguments,
    "query_subscription": NoArguments,
    "query_api_usage": UsageInput,
    "check_service_status": ServiceStatusInput,
    "query_billing_record": BillingRecordInput,
    "query_request_trace": RequestTraceInput,
    "query_api_key_metadata": ApiKeyMetadataInput,
    "query_incident_impact": IncidentImpactInput,
    "search_knowledge": KnowledgeSearchInput,
}

READ_TOOL_DESCRIPTIONS: dict[ReadToolName, str] = {
    "query_account": "Read the current scoped customer's account and subscription facts.",
    "query_subscription": "Read plan, entitlements, limits, catalog eligibility, and version.",
    "query_api_usage": "Read the current scoped customer's latest API usage snapshot.",
    "check_service_status": "Read current service status for one model and region.",
    "query_billing_record": "Read one opaque billing record in the current customer scope.",
    "query_request_trace": "Read one redacted request trace in the current tenant scope.",
    "query_api_key_metadata": "Read non-secret API Key metadata by ID or fingerprint.",
    "query_incident_impact": "Check whether one scoped request overlaps a known incident.",
    "search_knowledge": (
        "Search active versioned product knowledge for grounded evidence. Keep the query in the "
        "user's language and preserve the current question's business topic and requested answer "
        "dimensions; do not broaden it with unrelated workflow topics."
    ),
}


def canonical_schema_hash(schema: dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def model_argument_schema(name: ReadToolName) -> dict[str, Any]:
    return READ_TOOL_ARGUMENTS[name].model_json_schema()


def internal_mcp_transport_schema(name: ReadToolName) -> dict[str, Any]:
    transport_model = create_model(
        f"{name}Arguments",
        arguments=(READ_TOOL_ARGUMENTS[name], ...),
        trusted_context=(ToolCallContext, ...),
    )
    return transport_model.model_json_schema()


def read_tool_schema_hashes(name: ReadToolName) -> tuple[str, str]:
    return (
        canonical_schema_hash(model_argument_schema(name)),
        canonical_schema_hash(internal_mcp_transport_schema(name)),
    )


def native_read_tool_schemas(allowlist: set[ReadToolName]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": READ_TOOL_DESCRIPTIONS[name],
                "parameters": model_argument_schema(name),
            },
        }
        for name in READ_TOOL_ARGUMENTS
        if name in allowlist
    ]


class ReadToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ReadToolName
    arguments: (
        NoArguments
        | UsageInput
        | ServiceStatusInput
        | BillingRecordInput
        | KnowledgeSearchInput
        | RequestTraceInput
        | ApiKeyMetadataInput
        | IncidentImpactInput
    )

    @model_validator(mode="before")
    @classmethod
    def parse_arguments_for_tool_name(cls, value: Any) -> Any:
        """Parse provider JSON against the exact schema selected by tool name.

        Several tools intentionally have structurally identical public inputs.
        Letting Pydantic guess a plain Union therefore loses the semantic tool
        identity (for example request trace versus incident impact).  The name
        is the discriminator at this Runtime boundary.
        """
        if not isinstance(value, dict):
            return value
        name = value.get("name")
        arguments = value.get("arguments")
        if name not in READ_TOOL_ARGUMENTS:
            return value
        parsed = READ_TOOL_ARGUMENTS[cast(ReadToolName, name)].model_validate(arguments)
        return {**value, "arguments": parsed}

    @model_validator(mode="after")
    def validate_name_arguments(self) -> ReadToolCall:
        if self.name == "check_service_status" and not isinstance(
            self.arguments, ServiceStatusInput
        ):
            raise ValueError("check_service_status requires model and region")
        if self.name == "query_api_usage" and not isinstance(self.arguments, UsageInput):
            raise ValueError("query_api_usage requires a frozen window")
        if self.name == "query_billing_record" and not isinstance(
            self.arguments, BillingRecordInput
        ):
            raise ValueError("query_billing_record requires billing_record_id")
        if self.name == "query_request_trace" and not isinstance(self.arguments, RequestTraceInput):
            raise ValueError("query_request_trace requires request_id")
        if self.name == "query_api_key_metadata" and not isinstance(
            self.arguments, ApiKeyMetadataInput
        ):
            raise ValueError("query_api_key_metadata requires api_key_ref")
        if self.name == "query_incident_impact" and not isinstance(
            self.arguments, IncidentImpactInput
        ):
            raise ValueError("query_incident_impact requires request_id")
        if self.name == "search_knowledge" and not isinstance(self.arguments, KnowledgeSearchInput):
            raise ValueError("search_knowledge requires a knowledge query")
        if self.name in {"query_account", "query_subscription"} and not isinstance(
            self.arguments, NoArguments
        ):
            raise ValueError(f"{self.name} does not accept model or region")
        return self


class ActionToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ActionToolName
    arguments: dict[str, Any]


class ToolGateway:
    def __init__(
        self,
        manager: ToolTransport,
        *,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.manager = manager
        self.test_capability = test_capability

    async def rehandshake_read(self, *, failed_generation: int | None = None) -> int:
        rehandshake = getattr(self.manager, "rehandshake", None)
        if rehandshake is None:
            # Test-only transports without a process supervisor remain usable;
            # production MCPManager always implements the explicit contract.
            return 1
        return int(
            await rehandshake(
                "read",
                failed_generation=failed_generation,
            )
        )

    async def call_read(
        self,
        call: ReadToolCall,
        context: ToolCallContext,
        *,
        allow_retry: bool = True,
    ) -> ObservationEnvelope:
        # Agent Graph is the sole transport-retry owner. MCPManager may reconnect
        # when used directly for supervisor diagnostics, but runtime tool calls
        # always expose one physical send per durable reservation.
        del allow_retry
        started = perf_counter()
        if call.name in {"query_account", "query_subscription"}:
            NoArguments.model_validate(call.arguments)
        elif call.name == "query_api_usage":
            UsageInput.model_validate(call.arguments)
        elif call.name == "check_service_status":
            ServiceStatusInput.model_validate(call.arguments)
        elif call.name == "query_billing_record":
            BillingRecordInput.model_validate(call.arguments)
        elif call.name == "query_request_trace":
            RequestTraceInput.model_validate(call.arguments)
        elif call.name == "query_api_key_metadata":
            ApiKeyMetadataInput.model_validate(call.arguments)
        elif call.name == "query_incident_impact":
            IncidentImpactInput.model_validate(call.arguments)
        else:
            KnowledgeSearchInput.model_validate(call.arguments)
        trusted = context.model_dump(exclude_none=True, mode="json")
        arguments = call.arguments.model_dump()
        transport = {"arguments": arguments, "trusted_context": trusted}
        attempts = 1
        transport_lifecycle: dict[str, Any] | None = None
        try:
            managed_result = await self.manager.call(
                "read",
                call.name,
                transport,
                reconnect_once=False,
            )
            result = managed_result.value
            attempts = managed_result.attempts
            transport_lifecycle = self._transport_lifecycle(
                getattr(managed_result, "lifecycle", None),
                context,
            )
            payload = structured_result(result)
        except TimeoutError as exc:
            return self._failure(
                call.name,
                context,
                started,
                "timeout",
                "tool_timeout",
                attempt_index=attempts,
                data=self._transport_failure_data("read", exc),
                transport_lifecycle=self._transport_lifecycle_from_error(exc, context),
            )
        except Exception as exc:
            logger.exception("Read MCP call failed", extra={"tool_name": call.name})
            return self._failure(
                call.name,
                context,
                started,
                "unavailable",
                "tool_unavailable",
                attempt_index=attempts,
                data=self._transport_failure_data("read", exc),
                transport_lifecycle=self._transport_lifecycle_from_error(exc, context),
            )
        if payload.get("domain_error"):
            return self._domain_failure(call.name, context, started, payload)
        try:
            validated: BaseModel
            if call.name == "query_account":
                validated = AccountResult.model_validate(payload)
            elif call.name == "query_subscription":
                validated = SubscriptionResult.model_validate(payload)
            elif call.name == "query_api_usage":
                validated = UsageResult.model_validate(payload)
            elif call.name == "check_service_status":
                validated = ServiceStatusResult.model_validate(payload)
            elif call.name == "query_billing_record":
                validated = BillingRecordResult.model_validate(payload)
            elif call.name == "query_request_trace":
                validated = RequestTraceResult.model_validate(payload)
            elif call.name == "query_api_key_metadata":
                validated = ApiKeyMetadataResult.model_validate(payload)
            elif call.name == "query_incident_impact":
                validated = IncidentImpactResult.model_validate(payload)
            else:
                validated = KnowledgeSearchResult.model_validate(payload)
        except Exception as exc:
            logger.warning(
                "Read MCP output rejected",
                extra={"tool_name": call.name, "error_type": type(exc).__name__},
            )
            return self._output_failure(call.name, context, started)
        return self._success(
            call.name,
            context,
            started,
            validated,
            attempt_index=attempts,
            transport_lifecycle=transport_lifecycle,
        )

    async def call_action(
        self, call: ActionToolCall, context: ToolCallContext
    ) -> ObservationEnvelope:
        started = perf_counter()
        arguments: (
            RefundProposalInput | ApiKeyRevocationProposalInput | EntitlementChangeProposalInput
        )
        validated: BaseModel
        if call.name == "propose_refund":
            visible = dict(call.arguments)
            visible.pop("idempotency_key", None)
            billing_record_id = str(visible.get("billing_record_id", ""))
            arguments = RefundProposalInput.model_validate(
                {
                    **visible,
                    "idempotency_key": (f"refund:{context.ticket_id}:{billing_record_id}"),
                }
            )
        elif call.name == "propose_api_key_revocation":
            visible = dict(call.arguments)
            visible.pop("idempotency_key", None)
            api_key_id = str(visible.get("api_key_id", ""))
            arguments = ApiKeyRevocationProposalInput.model_validate(
                {
                    **visible,
                    "idempotency_key": f"key-revoke:{context.ticket_id}:{api_key_id}",
                }
            )
        else:
            visible = dict(call.arguments)
            visible.pop("idempotency_key", None)
            subscription_id = str(visible.get("subscription_id", ""))
            arguments = EntitlementChangeProposalInput.model_validate(
                {
                    **visible,
                    "idempotency_key": (f"entitlement:{context.ticket_id}:{subscription_id}"),
                }
            )
        try:
            call_arguments = {
                **arguments.model_dump(),
                **context.model_dump(exclude_none=True, mode="json"),
            }
            managed_result = await self.manager.call(
                "action", call.name, call_arguments, reconnect_once=False
            )
            result = managed_result.value
            payload = structured_result(result)
        except TimeoutError as exc:
            return self._failure(
                call.name,
                context,
                started,
                "timeout",
                "action_timeout",
                data=self._transport_failure_data("action", exc),
            )
        except Exception as exc:
            logger.exception("Action MCP call failed", extra={"tool_name": call.name})
            return self._failure(
                call.name,
                context,
                started,
                "unavailable",
                "action_unavailable",
                data=self._transport_failure_data("action", exc),
            )
        if payload.get("domain_error"):
            return self._domain_failure(call.name, context, started, payload)
        if call.name == "propose_refund" and self.test_capability is not None:
            validated = RefundProposalResult.model_validate(payload)
        else:
            validated = DraftProposalResult.model_validate(payload)
        return self._success(call.name, context, started, validated)

    @staticmethod
    def _success(
        tool_name: str,
        context: ToolCallContext,
        started: float,
        result: BaseModel,
        attempt_index: int = 1,
        transport_lifecycle: dict[str, Any] | None = None,
    ) -> ObservationEnvelope:
        payload = result.model_dump(mode="json")
        source_refs = ToolGateway._deduplicate_exact_source_refs(payload.pop("source_refs", []))
        payload.pop("tool_call_id", None)
        payload.pop("ticket_id", None)
        version = payload.get("version") or payload.get("business_version")
        return ObservationEnvelope(
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            attempt_index=attempt_index,
            status="ok",
            retryable=False,
            observed_at=datetime.now(UTC),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
            source_refs=source_refs,
            resource_version=str(version) if version is not None else None,
            data=payload,
            transport_lifecycle=transport_lifecycle,
        )

    @staticmethod
    def _deduplicate_exact_source_refs(
        source_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Collapse transport duplicates without hiding conflicting source identities."""

        seen: set[tuple[str, str, str]] = set()
        normalized: list[dict[str, Any]] = []
        for source_ref in source_refs:
            identity = (
                str(source_ref.get("source_type", "")),
                str(source_ref.get("source_id", "")),
                str(source_ref.get("observed_at", "")),
            )
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(source_ref)
        return normalized

    @staticmethod
    def _transport_lifecycle(
        raw: object,
        context: ToolCallContext,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or raw.get("schema_version") != "mcp-transport-lifecycle.v1":
            return None
        lifecycle = dict(raw)
        lifecycle.update(
            {
                "tenant_id": context.tenant_id,
                "customer_id": context.customer_id,
                "ticket_id": context.ticket_id,
                "run_id": context.run_id,
                "job_id": context.job_id,
                "segment_id": context.segment_id,
                "delivery_generation": context.delivery_generation,
                "fencing_token": context.fencing_token,
            }
        )
        if isinstance(context.mcp_context, ReadMcpCallContext):
            lifecycle.update(
                {
                    "logical_invocation_id": context.mcp_context.logical_invocation_id,
                    "tool_attempt_id": context.mcp_context.tool_attempt_id,
                    "transport_attempt_id": context.mcp_context.transport_attempt_id,
                    "transport_ordinal": context.mcp_context.transport_attempt,
                    "tool_round": context.mcp_context.agent_tool_round,
                }
            )
        return lifecycle

    def _transport_lifecycle_from_error(
        self,
        exc: BaseException,
        context: ToolCallContext,
    ) -> dict[str, Any] | None:
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, MCPTransportFailure):
                return self._transport_lifecycle(current.lifecycle, context)
            current = current.__cause__ or current.__context__
        return None

    def _transport_failure_data(
        self,
        server_name: Literal["read", "action"],
        exc: BaseException,
    ) -> dict[str, Any]:
        """Expose bounded failure class/supervisor state without exception text."""

        safe_type = (
            str(exc.lifecycle.get("error_family", "unknown"))
            if isinstance(exc, MCPTransportFailure)
            else type(exc).__name__
        )
        data: dict[str, Any] = {"transport_error_type": safe_type}
        health = getattr(self.manager, "health", None)
        if not callable(health):
            return data
        try:
            server = health().get(server_name, {})
        except Exception:
            return data
        if not isinstance(server, dict):
            return data
        data["supervisor"] = {
            key: server[key]
            for key in (
                "state",
                "session",
                "schema",
                "reconnects",
                "pending_calls",
                "generation",
            )
            if key in server and isinstance(server[key], (str, int, bool))
        }
        return data

    @staticmethod
    def _failure(
        tool_name: str,
        context: ToolCallContext,
        started: float,
        status: Literal["timeout", "unavailable"],
        error_code: str,
        attempt_index: int = 1,
        data: dict[str, Any] | None = None,
        transport_lifecycle: dict[str, Any] | None = None,
    ) -> ObservationEnvelope:
        return ObservationEnvelope(
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            attempt_index=attempt_index,
            status=status,
            retryable=False,
            error_code=error_code,
            safe_error_summary="Required capability is temporarily unavailable.",
            observed_at=datetime.now(UTC),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
            data=data or {},
            transport_lifecycle=transport_lifecycle,
        )

    @staticmethod
    def _output_failure(
        tool_name: str,
        context: ToolCallContext,
        started: float,
    ) -> ObservationEnvelope:
        return ObservationEnvelope(
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            attempt_index=(
                context.mcp_context.transport_attempt
                if isinstance(context.mcp_context, ReadMcpCallContext)
                else 1
            ),
            status="invalid_input",
            retryable=False,
            error_code="tool_output_schema_invalid",
            safe_error_summary="The capability returned an invalid response.",
            observed_at=datetime.now(UTC),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    @staticmethod
    def _domain_failure(
        tool_name: str,
        context: ToolCallContext,
        started: float,
        payload: dict[str, object],
    ) -> ObservationEnvelope:
        internal_reason = payload.get("internal_boundary_reason")
        if isinstance(internal_reason, str) and internal_reason:
            logger.warning(
                "MCP domain boundary rejected: %s",
                internal_reason,
                extra={
                    "tool_name": tool_name,
                    "mcp_boundary_reason": internal_reason,
                },
            )
        status = str(payload.get("status", "invalid_input"))
        allowed = {
            "not_found",
            "denied",
            "forbidden_tool",
            "invalid_input",
            "conflict",
        }
        normalized = status if status in allowed else "invalid_input"
        return ObservationEnvelope(
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            ticket_id=context.ticket_id,
            run_id=context.run_id,
            attempt_index=1,
            status=cast(ObservationStatus, normalized),
            retryable=False,
            error_code=str(payload.get("error_code", "tool_domain_error")),
            safe_error_summary=str(payload.get("safe_error_summary", "Tool request was rejected.")),
            observed_at=datetime.now(UTC),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )
