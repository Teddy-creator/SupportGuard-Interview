"""Typed planning and progress contracts for the bounded Read Tool Loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.actions.service import ActionSpec
from supportguard.agent.nodes.finalization import SafeStopHost
from supportguard.agent.schemas import NativeReadToolCall
from supportguard.agent.state import AgentState
from supportguard.agent.tool_transport import ReadTransportHost
from supportguard.contracts.action_preconditions import ActionAdmissionV2
from supportguard.contracts.tools import ObservationEnvelope
from supportguard.db.models import ToolObservation
from supportguard.services.runtime_jobs import JobLease


class ToolTurnHost(Protocol):
    session: AsyncSession | None

    async def _current_lease(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_tool_round(self, *args: Any, **kwargs: Any) -> Any: ...


class ReadLoopHost(ReadTransportHost, SafeStopHost, Protocol):
    session: AsyncSession | None

    async def _close_tool_batch(self, *args: Any, **kwargs: Any) -> AgentState: ...
    def _allowlist(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _current_lease(self, *args: Any, **kwargs: Any) -> Any: ...
    def _durable_read_invocation_logical_id(self, *args: Any, **kwargs: Any) -> str: ...
    def _effective_knowledge_observations(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _event(self, *args: Any, **kwargs: Any) -> None: ...
    def _fingerprint(self, *args: Any, **kwargs: Any) -> str: ...
    async def _finish_external(self, *args: Any, **kwargs: Any) -> None: ...
    async def _finish_tool_terminal(self, *args: Any, **kwargs: Any) -> Any: ...
    def _knowledge_comparison_state(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _reserve_external(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _terminalize_tool_without_attempt(self, *args: Any, **kwargs: Any) -> Any: ...
    async def _transition(self, *args: Any, **kwargs: Any) -> None: ...
    def _trusted_retrieval_intent(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReadBatchPlan:
    calls: tuple[NativeReadToolCall, ...]
    allowlist: frozenset[str]
    fingerprints: tuple[str, ...]
    semantic_rejections: dict[int, str]
    semantic_keys: dict[int, str]
    action_spec: ActionSpec | None
    admission: ActionAdmissionV2 | None
    admission_payload: dict[str, Any]
    round_index: int
    durable_attempt_base: int
    lease: JobLease | None
    durable_batch_budget_exhausted: bool
    invocation_ids: tuple[str, ...]
    logical_invocation_ids: tuple[str, ...]
    qualified_obligation_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReadCallContext:
    index: int
    item: NativeReadToolCall
    invocation_id: str | None
    logical_invocation_id: str | None
    obligation_id: str | None
    trusted_retrieval_intent: dict[str, Any] | None


@dataclass(slots=True)
class ReadBatchProgress:
    observations: list[dict[str, Any]] = field(default_factory=list)
    attempts_used: int = 0
    hard_terminal_reason: str | None = None
    transport_calls: int = 0
    durable_success_replays: int = 0
    qualified_obligation_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ReadCallResult:
    observation: ObservationEnvelope
    persisted_observation: ToolObservation | None
    attempt_cost: int
    hard_terminal_reason: str | None = None
    transport_calls: int = 0
    durable_success_replays: int = 0


__all__ = [
    "ReadBatchPlan",
    "ReadBatchProgress",
    "ReadCallContext",
    "ReadCallResult",
    "ReadLoopHost",
    "ToolTurnHost",
]
