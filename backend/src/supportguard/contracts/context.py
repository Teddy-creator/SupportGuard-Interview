from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import CausalDecisionV2
from supportguard.rag.intent import RetrievalIntentEnvelope


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"trusted context field {name} is required")


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: str
    authenticated_actor_id: str
    authenticated_actor_role: str
    request_id: str
    trace_id: str
    deadline: datetime
    subject_customer_id: str | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, str):
                _require_text(field.name, value)
        if self.deadline.tzinfo is None:
            raise ValueError("trusted context deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ControlPlaneContext:
    executor_service_id: str
    operation: str
    request_id: str
    trace_id: str
    deadline: datetime

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, str):
                _require_text(field.name, value)
        if self.deadline.tzinfo is None:
            raise ValueError("trusted context deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WorkerExecutionContext:
    tenant_id: str
    actor_principal_id: str
    executor_service_principal: str
    customer_id: str
    ticket_id: str
    run_id: str
    job_id: str
    segment_id: str
    delivery_generation: int
    fencing_token: int
    trace_id: str
    deadline: datetime

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, str):
                _require_text(field.name, value)
        if self.delivery_generation < 1:
            raise ValueError("delivery_generation must be positive")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if self.deadline.tzinfo is None:
            raise ValueError("trusted context deadline must be timezone-aware")


class ReadMcpCallContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_kind: Literal["read"] = "read"
    logical_invocation_id: str
    tool_attempt_id: str
    transport_attempt_id: str
    tool_name: str
    transport_attempt: int = Field(ge=1)
    agent_tool_round: int = Field(ge=1)
    call_deadline: datetime
    worker_deadline: datetime
    retrieval_intent: RetrievalIntentEnvelope | None = None
    trace_origin: Literal["agent_read_tool", "offline", "maintenance", "future_eval"] = (
        "agent_read_tool"
    )

    @model_validator(mode="after")
    def validate_deadline(self) -> ReadMcpCallContext:
        _require_text("logical_invocation_id", self.logical_invocation_id)
        _require_text("tool_attempt_id", self.tool_attempt_id)
        _require_text("transport_attempt_id", self.transport_attempt_id)
        _require_text("tool_name", self.tool_name)
        if self.call_deadline.tzinfo is None:
            raise ValueError("call_deadline must be timezone-aware")
        if self.worker_deadline.tzinfo is None:
            raise ValueError("worker_deadline must be timezone-aware")
        if self.call_deadline > self.worker_deadline:
            raise ValueError("MCP deadline cannot exceed the worker deadline")
        return self


class PolicyCapabilityMcpCallContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_kind: Literal["policy_capability"] = "policy_capability"
    capability_invocation_id: str
    capability_attempt_id: str
    capability_name: str
    effect_identity: str = Field(min_length=64, max_length=64)
    capability_attempt: int = Field(ge=1)
    capability_sequence: int = Field(ge=1)
    agent_tool_round: None = None
    causal_decision_hash: str = Field(min_length=64, max_length=64)
    causal_decision_schema_version: Literal["causal-decision.v2"] = "causal-decision.v2"
    causal_decision: CausalDecisionV2
    observation_binding_hash: str = Field(min_length=64, max_length=64)
    call_deadline: datetime
    worker_deadline: datetime

    @model_validator(mode="after")
    def validate_deadline(self) -> PolicyCapabilityMcpCallContext:
        _require_text("capability_invocation_id", self.capability_invocation_id)
        _require_text("capability_attempt_id", self.capability_attempt_id)
        _require_text("capability_name", self.capability_name)
        if self.call_deadline.tzinfo is None or self.worker_deadline.tzinfo is None:
            raise ValueError("MCP deadlines must be timezone-aware")
        if self.call_deadline > self.worker_deadline:
            raise ValueError("MCP deadline cannot exceed the worker deadline")
        if canonical_json_hash(self.causal_decision.model_dump(mode="python")) != (
            self.causal_decision_hash
        ):
            raise ValueError("causal decision hash does not match its typed payload")
        if self.causal_decision.observation_binding_hash != self.observation_binding_hash:
            raise ValueError("causal decision observation binding hash mismatch")
        return self


McpCallContext = Annotated[
    ReadMcpCallContext | PolicyCapabilityMcpCallContext,
    Field(discriminator="surface_kind"),
]


T = TypeVar("T")


class TrustedContextSlot(Generic[T]):
    """Lexically binds one context type and rejects accidental inheritance/mismatch."""

    def __init__(self, name: str) -> None:
        self._var: ContextVar[T | None] = ContextVar(name, default=None)

    def get(self) -> T:
        value = self._var.get()
        if value is None:
            raise RuntimeError("required trusted context is not bound")
        return value

    @contextmanager
    def bind(self, value: T) -> Iterator[T]:
        token: Token[T | None] = self._var.set(value)
        try:
            yield value
        finally:
            self._var.reset(token)


request_context = TrustedContextSlot[RequestContext]("supportguard_trusted_request")
control_plane_context = TrustedContextSlot[ControlPlaneContext](
    "supportguard_trusted_control_plane"
)
worker_execution_context = TrustedContextSlot[WorkerExecutionContext]("supportguard_trusted_worker")
mcp_call_context = TrustedContextSlot[McpCallContext]("supportguard_trusted_mcp_call")
