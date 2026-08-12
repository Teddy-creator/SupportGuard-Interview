"""Physical Read MCP send boundary used by the bounded Tool Loop owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from supportguard.agent.schemas import NativeReadToolCall
from supportguard.agent.state import AgentState
from supportguard.contracts.tools import ObservationEnvelope
from supportguard.tools.gateway import ToolGateway


class ReadTransportHost(Protocol):
    gateway: ToolGateway

    def _normalize_gateway_result(self, *args: Any, **kwargs: Any) -> ObservationEnvelope: ...
    def _read_tool_context(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReadTransportRequest:
    state: AgentState
    item: NativeReadToolCall
    reservation: Any
    logical_invocation_id: str | None
    transport_ordinal: int
    round_index: int


@dataclass(frozen=True, slots=True)
class ReadTransportExecutor:
    """Perform one physical send and normalize it without granting or persisting authority."""

    host: ReadTransportHost

    async def send(self, request: ReadTransportRequest) -> ObservationEnvelope:
        result = await self.host.gateway.call_read(
            request.item.call,
            self.host._read_tool_context(
                request.state,
                request.item.tool_call_id,
                tool_name=request.item.call.name,
                reservation=request.reservation,
                logical_invocation_id=request.logical_invocation_id,
                transport_attempt=request.transport_ordinal,
                tool_round=request.round_index,
            ),
            allow_retry=False,
        )
        observation = self.host._normalize_gateway_result(
            request.item.call,
            request.item.tool_call_id,
            request.state,
            result,
        ).model_copy(update={"attempt_index": request.transport_ordinal})
        if isinstance(result, ObservationEnvelope) and (
            (result.tenant_id and result.tenant_id != request.state["tenant_id"])
            or (result.customer_id and result.customer_id != request.state["customer_id"])
        ):
            return observation.model_copy(
                update={
                    "status": "forbidden_tool",
                    "retryable": False,
                    "error_code": "observation_scope_mismatch",
                    "safe_error_summary": "The read returned data outside the authorized scope.",
                }
            )
        return observation


__all__ = [
    "ReadTransportExecutor",
    "ReadTransportHost",
    "ReadTransportRequest",
]
