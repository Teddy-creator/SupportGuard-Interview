from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from supportguard.agent.schemas import AgentDecision, CandidateResponse

OutputT = TypeVar("OutputT")
SchemaOutputT = TypeVar("SchemaOutputT", bound=BaseModel)
TERMINAL_CANDIDATE_FUNCTION = "final_candidate"


def native_terminal_candidate_schema() -> dict[str, Any]:
    """Return the reserved Provider-output function, never an application tool."""

    return {
        "type": "function",
        "function": {
            "name": TERMINAL_CANDIDATE_FUNCTION,
            "description": (
                "Submit one terminal, evidence-bound CandidateResponse. This is a response "
                "transport only; it performs no read, write, approval, or external effect."
            ),
            "parameters": CandidateResponse.model_json_schema(),
        },
    }


@dataclass(frozen=True, slots=True)
class RawProviderToolCall:
    provider_tool_call_id: str
    name: str
    arguments_json: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class RawProviderDecision:
    """Provider response captured before tool name/argument/schema validation."""

    finish_reason: str | None
    content: str | None
    tool_calls: tuple[RawProviderToolCall, ...]


def raw_decision_from_typed(decision: AgentDecision) -> RawProviderDecision:
    if decision.decision_type == "tool_calls":
        calls = tuple(
            RawProviderToolCall(
                provider_tool_call_id=item.tool_call_id,
                name=item.call.name,
                arguments_json=json.dumps(
                    item.call.arguments.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                ordinal=ordinal,
            )
            for ordinal, item in enumerate(decision.tool_calls)
        )
        return RawProviderDecision("tool_calls", None, calls)
    return RawProviderDecision(
        "stop",
        decision.model_dump_json(),
        (),
    )


@dataclass(frozen=True)
class ProviderUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class ProviderTransportRecord:
    serializer_version: str
    request_bytes: bytes
    request_hash: str

    @classmethod
    def from_bytes(
        cls, request_bytes: bytes, *, serializer_version: str
    ) -> ProviderTransportRecord:
        return cls(
            serializer_version=serializer_version,
            request_bytes=request_bytes,
            request_hash=hashlib.sha256(request_bytes).hexdigest(),
        )


def canonical_transport_record(payload: dict[str, Any]) -> ProviderTransportRecord:
    request_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return ProviderTransportRecord.from_bytes(request_bytes, serializer_version="canonical-json.v1")


@dataclass(frozen=True)
class ProviderCallResult(Generic[OutputT]):
    output: OutputT
    attempts: int
    usage: ProviderUsage
    trace_metadata: dict[str, str]
    transport: ProviderTransportRecord | None = None
    transport_attempts: int = 1


class StructuredProvider(Protocol):
    mode: str
    model: str
    tool_call_mode: str

    async def generate(
        self,
        *,
        system: str,
        user: str,
        output_schema: type[SchemaOutputT],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[SchemaOutputT]: ...

    async def decide(
        self,
        *,
        system: str,
        context: str,
        tools: list[dict[str, Any]],
        prior_turns: list[dict[str, Any]],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[RawProviderDecision] | AgentDecision: ...


def normalize_provider_result(
    value: ProviderCallResult[SchemaOutputT] | SchemaOutputT,
) -> ProviderCallResult[SchemaOutputT]:
    """Compatibility adapter for deterministic test doubles during v1.2 migration."""

    if isinstance(value, ProviderCallResult):
        return value
    return ProviderCallResult(output=value, attempts=1, usage=ProviderUsage(), trace_metadata={})


def normalize_decision_result(
    value: ProviderCallResult[RawProviderDecision]
    | ProviderCallResult[AgentDecision]
    | RawProviderDecision
    | AgentDecision,
) -> ProviderCallResult[RawProviderDecision]:
    if isinstance(value, ProviderCallResult):
        output = value.output
        raw = output if isinstance(output, RawProviderDecision) else raw_decision_from_typed(output)
        return ProviderCallResult(
            raw,
            value.attempts,
            value.usage,
            value.trace_metadata,
            value.transport,
            value.transport_attempts,
        )
    raw = value if isinstance(value, RawProviderDecision) else raw_decision_from_typed(value)
    return ProviderCallResult(raw, 1, ProviderUsage(), {})
