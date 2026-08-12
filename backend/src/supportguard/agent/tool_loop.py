from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from supportguard.actions.service import ActionSpec, get_action_spec
from supportguard.agent.constants import MAX_LLM_CALLS, MAX_TOOL_ATTEMPTS, MAX_TOOL_ROUNDS
from supportguard.agent.nodes.finalization import safe_stop
from supportguard.agent.obligations import ActionObligationLedger, evaluate_action_obligations
from supportguard.agent.schemas import AgentDecision, NativeReadToolCall
from supportguard.agent.state import AgentState
from supportguard.agent.tool_loop_contracts import (
    ReadBatchPlan as _BatchPlan,
)
from supportguard.agent.tool_loop_contracts import (
    ReadBatchProgress as _BatchProgress,
)
from supportguard.agent.tool_loop_contracts import (
    ReadCallContext as _CallContext,
)
from supportguard.agent.tool_loop_contracts import (
    ReadCallResult,
    ReadLoopHost,
    ToolTurnHost,
)
from supportguard.agent.tool_policy import (
    obligation_for_capability,
    semantic_batch_rejections,
    semantic_invocation_key,
)
from supportguard.agent.tool_transport import (
    ReadTransportExecutor,
    ReadTransportRequest,
)
from supportguard.contracts.action_preconditions import ActionAdmissionV2
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.tools import ObservationEnvelope
from supportguard.db.models import AgentRun, ToolInvocation
from supportguard.observability.metrics import TOOL_OBSERVATIONS
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger


async def open_tool_turn(
    host: ToolTurnHost,
    state: AgentState,
    decision: AgentDecision,
    context_manifest: dict[str, Any],
) -> AgentState:
    """Persist one bounded Provider tool turn before the Read Loop executes it."""

    lease = await host._current_lease(state)
    if host.session is None or lease is None:
        return {"tool_round_rejected": state["tool_rounds"] >= MAX_TOOL_ROUNDS}
    rejected = state["tool_rounds"] >= MAX_TOOL_ROUNDS
    if not rejected:
        await host._reserve_tool_round(state)
    calls = [
        InvocationSpec(
            provider_tool_call_id=item.tool_call_id,
            tool_name=item.call.name,
            arguments={},
            arguments_hash=hashlib.sha256(
                json.dumps(
                    item.call.arguments.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            ordinal=index,
        )
        for index, item in enumerate(decision.tool_calls)
    ]
    turn, invocations = await ToolLedger(host.session).open_turn(
        lease,
        segment_id=state["segment_id"],
        tool_round=(state["tool_rounds"] if rejected else state["tool_rounds"] + 1),
        decision=decision.model_dump(mode="json"),
        context_manifest=context_manifest,
        calls=calls,
    )
    await host.session.commit()
    return {
        "turn_group_id": turn.id,
        "tool_invocation_ids": [item.id for item in invocations],
        "tool_logical_invocation_ids": [item.logical_invocation_id for item in invocations],
        "tool_rounds": state["tool_rounds"] if rejected else state["tool_rounds"] + 1,
        "tool_round_rejected": rejected,
    }


def _hard_terminal_read_reason(observation: ObservationEnvelope) -> str | None:
    error_code = str(observation.error_code or "")
    if observation.status in {"forbidden_tool", "timeout", "unavailable"}:
        return error_code or "read_capability_failed"
    if (
        error_code
        in {
            "billing_scope_violation",
            "ticket_scope_violation",
            "cross_tenant_argument",
            "cross_tenant_observation",
            "observation_scope_mismatch",
            "forbidden_surface",
            "forbidden_tool_surface",
        }
        or "retry_exhausted" in error_code
    ):
        return error_code or "read_capability_failed"
    return None


def _semantic_contract(
    state: AgentState,
    calls: tuple[NativeReadToolCall, ...],
) -> tuple[
    ActionSpec | None,
    ActionAdmissionV2 | None,
    dict[str, Any],
    dict[int, str],
    dict[int, str],
]:
    """Derive no-progress keys from deterministic obligations, never authorization."""

    admission_payload = state.get("action_admission", {})
    ledger_payload = state.get("action_obligation_ledger", {})
    action_spec: ActionSpec | None = None
    admission: ActionAdmissionV2 | None = None
    semantic_rejections: dict[int, str] = {}
    semantic_keys: dict[int, str] = {}
    if not (
        admission_payload.get("schema_version") == "action-admission.v2"
        and admission_payload.get("status") == "admitted"
        and ledger_payload
    ):
        return action_spec, admission, ledger_payload, semantic_rejections, semantic_keys

    admission = ActionAdmissionV2.model_validate(admission_payload)
    if admission.action_type is None:
        return action_spec, admission, ledger_payload, semantic_rejections, semantic_keys
    action_spec = get_action_spec(admission.action_type)
    ledger = ActionObligationLedger.model_validate(ledger_payload)
    semantic_rejections.update(
        semantic_batch_rejections(
            action_spec=action_spec,
            ledger=ledger,
            calls=[item.call for item in calls],
        )
    )
    index_snapshot = next(
        (
            str(item.get("data", {}).get("index_version"))
            for item in reversed(state.get("tool_observations", []))
            if item.get("tool_name") == "search_knowledge"
            and item.get("data", {}).get("index_version")
        ),
        None,
    )
    semantic_keys = {
        index: semantic_invocation_key(
            action_spec=action_spec,
            admission=admission,
            ledger=ledger,
            call=item.call,
            index_snapshot=index_snapshot,
        )
        for index, item in enumerate(calls)
    }
    return action_spec, admission, ledger_payload, semantic_rejections, semantic_keys


def _terminal_observation(
    state: AgentState,
    context: _CallContext,
    *,
    status: str,
    error_code: str,
    summary: str,
    attempt_index: int = 1,
    data: dict[str, Any] | None = None,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        tool_name=context.item.call.name,
        tool_call_id=context.item.tool_call_id,
        ticket_id=state["ticket_id"],
        run_id=state["run_id"],
        attempt_index=attempt_index,
        status=cast(Any, status),
        retryable=False,
        error_code=error_code,
        safe_error_summary=summary,
        observed_at=datetime.now(UTC),
        duration_ms=0,
        data=data or {},
    )


@dataclass(frozen=True, slots=True)
class ReadLoopNodes:
    """Own one bounded Read Tool round and its durable Observation settlement."""

    host: ReadLoopHost

    async def execute_reads(self, state: AgentState) -> AgentState:
        """Execute one bounded batch through explicit plan, call, settle and close stages."""

        plan, terminal = await self._plan_and_validate_batch(state)
        if terminal is not None:
            return terminal
        if plan is None:  # pragma: no cover - the pair is an internal invariant
            raise RuntimeError("read_loop_plan_missing")

        progress = _BatchProgress(
            qualified_obligation_ids=set(plan.qualified_obligation_ids),
        )
        for call_index, item in enumerate(plan.calls):
            context = self._call_context(state, plan, call_index, item)
            result = await self._execute_one(state, plan, progress, context)
            await self._settle_one(state, plan, progress, context, result)
        return await self._close_batch(state, plan, progress)

    async def _plan_and_validate_batch(
        self,
        state: AgentState,
    ) -> tuple[_BatchPlan | None, AgentState | None]:
        decision = AgentDecision.model_validate(state["agent_decision"])
        calls = tuple(decision.tool_calls)
        if not calls or len(calls) > 3:
            return None, await self.host._close_tool_batch(
                state, list(calls), outcome="invalid_input", status="invalid_input"
            )
        if state.get("tool_round_rejected"):
            return None, await self.host._close_tool_batch(
                state,
                list(calls),
                outcome="budget_exhausted",
                status="denied",
                stop_reason="tool_round_budget_exhausted",
            )
        durable_turn = bool(
            self.host.session is not None and state.get("job_id") and state.get("turn_group_id")
        )
        if not durable_turn and len(calls) > MAX_TOOL_ATTEMPTS - state["tool_attempts"]:
            return None, await self.host._close_tool_batch(
                state,
                list(calls),
                outcome="budget_exhausted",
                status="denied",
                stop_reason="tool_attempt_budget_exhausted",
            )

        fingerprints = tuple(state.get("executed_fingerprints", []))
        proposed = tuple(self.host._fingerprint(item.call, state) for item in calls)
        action_spec, admission, ledger_payload, semantic_rejections, semantic_keys = (
            _semantic_contract(state, calls)
        )
        call_ids = [item.tool_call_id for item in calls]
        if len(set(call_ids)) != len(call_ids):
            return None, await self.host._close_tool_batch(
                state, list(calls), outcome="invalid_input", status="invalid_input"
            )
        exact_rejections = {
            index for index, fingerprint in enumerate(proposed) if fingerprint in fingerprints
        }
        if not ledger_payload and exact_rejections:
            return None, await self.host._close_tool_batch(
                state,
                list(calls),
                outcome="no_progress",
                status="denied",
                reserve_preflight=True,
            )
        if not ledger_payload:
            for index in exact_rejections:
                semantic_rejections.setdefault(index, "duplicate_logical_invocation")

        round_index = (
            state["tool_rounds"]
            if self.host.session is not None and state.get("job_id")
            else state["tool_rounds"] + 1
        )
        durable_attempt_base = state["tool_attempts"]
        if self.host.session is not None and state.get("job_id"):
            durable_run = await self.host.session.get(AgentRun, state["run_id"])
            if durable_run is None:
                raise RuntimeConflict("run_not_found")
            durable_attempt_base = max(durable_attempt_base, durable_run.tool_attempts)
        lease = cast(JobLease | None, await self.host._current_lease(state))
        budget_exhausted = await self._durable_batch_exceeds_budget(
            state,
            lease=lease,
            durable_turn=durable_turn,
            durable_attempt_base=durable_attempt_base,
        )
        qualified_ids = (
            frozenset(
                item.obligation_id
                for item in ActionObligationLedger.model_validate(ledger_payload).obligations
                if item.status in {"satisfied", "read_qualified"}
            )
            if ledger_payload
            else frozenset()
        )
        return (
            _BatchPlan(
                calls=calls,
                allowlist=frozenset(self.host._allowlist(state)),
                fingerprints=fingerprints,
                semantic_rejections=semantic_rejections,
                semantic_keys=semantic_keys,
                action_spec=action_spec,
                admission=admission,
                admission_payload=state.get("action_admission", {}),
                round_index=round_index,
                durable_attempt_base=durable_attempt_base,
                lease=lease,
                durable_batch_budget_exhausted=budget_exhausted,
                invocation_ids=tuple(state.get("tool_invocation_ids", [])),
                logical_invocation_ids=tuple(state.get("tool_logical_invocation_ids", [])),
                qualified_obligation_ids=qualified_ids,
            ),
            None,
        )

    async def _durable_batch_exceeds_budget(
        self,
        state: AgentState,
        *,
        lease: JobLease | None,
        durable_turn: bool,
        durable_attempt_base: int,
    ) -> bool:
        if not (
            durable_turn
            and self.host.session is not None
            and lease is not None
            and state.get("turn_group_id")
        ):
            return False
        pending = await ToolLedger(self.host.session).pending_invocation_count(
            lease,
            str(state["turn_group_id"]),
        )
        return pending > MAX_TOOL_ATTEMPTS - durable_attempt_base

    def _call_context(
        self,
        state: AgentState,
        plan: _BatchPlan,
        call_index: int,
        item: NativeReadToolCall,
    ) -> _CallContext:
        obligation_id = (
            obligation_for_capability(plan.action_spec, item.call.name)
            if plan.action_spec is not None
            else None
        )
        return _CallContext(
            index=call_index,
            item=item,
            invocation_id=(
                plan.invocation_ids[call_index] if call_index < len(plan.invocation_ids) else None
            ),
            logical_invocation_id=(
                plan.logical_invocation_ids[call_index]
                if call_index < len(plan.logical_invocation_ids)
                else None
            ),
            obligation_id=obligation_id,
            trusted_retrieval_intent=(
                self.host._trusted_retrieval_intent(state).model_dump(mode="json")
                if item.call.name == "search_knowledge"
                else None
            ),
        )

    async def _execute_one(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
    ) -> ReadCallResult:
        settled = await self._reserve_or_replay(state, plan, progress, context)
        if settled is not None:
            return settled
        return await self._execute_transport(state, plan, progress, context)

    async def _reserve_or_replay(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
    ) -> ReadCallResult | None:
        replayed = None
        if (
            self.host.session is not None
            and plan.lease is not None
            and context.invocation_id is not None
            and state.get("turn_group_id")
        ):
            replayed = await ToolLedger(self.host.session).replay_terminal_observation(
                plan.lease,
                context.invocation_id,
                turn_id=str(state["turn_group_id"]),
                provider_tool_call_id=context.item.tool_call_id,
                tool_name=context.item.call.name,
                arguments_hash=canonical_json_hash(
                    context.item.call.arguments.model_dump(mode="json")
                ),
            )
        if replayed is not None:
            persisted, observation = replayed
            if (
                context.trusted_retrieval_intent is not None
                and persisted.payload.get("trusted_retrieval_intent")
                != context.trusted_retrieval_intent
            ):
                raise RuntimeConflict("trusted_retrieval_intent_replay_mismatch")
            return ReadCallResult(
                observation=observation,
                persisted_observation=persisted,
                attempt_cost=0,
                hard_terminal_reason=(
                    None if observation.status == "ok" else _hard_terminal_read_reason(observation)
                ),
                durable_success_replays=int(observation.status == "ok"),
            )
        if plan.durable_batch_budget_exhausted:
            observation = _terminal_observation(
                state,
                context,
                status="denied",
                error_code="tool_attempt_budget_exhausted",
                summary="The remaining durable tool batch exceeds its attempt budget.",
            )
            persisted = await self.host._terminalize_tool_without_attempt(
                plan.lease,
                invocation_id=context.invocation_id,
                observation=observation,
                trusted_retrieval_intent=context.trusted_retrieval_intent,
            )
            return ReadCallResult(
                observation,
                persisted,
                0,
                hard_terminal_reason="tool_attempt_budget_exhausted",
            )
        if progress.hard_terminal_reason is not None:
            observation = _terminal_observation(
                state,
                context,
                status="denied",
                error_code="cancelled_due_to_terminal_failure",
                summary="A prior hard terminal failure cancelled the remaining tool call.",
                data={"terminal_reason": progress.hard_terminal_reason},
            )
            return await self._persist_preflight(
                state,
                context,
                observation,
                invocation_outcome="cancelled",
            )
        if context.index in plan.semantic_rejections or (
            context.obligation_id is not None
            and context.obligation_id in progress.qualified_obligation_ids
        ):
            observation = _terminal_observation(
                state,
                context,
                status="denied",
                error_code="semantic_no_progress",
                summary="The requested read cannot advance an unsatisfied obligation.",
                data={
                    "reason_code": plan.semantic_rejections.get(
                        context.index,
                        "obligation_qualified_earlier_in_batch",
                    ),
                    "semantic_invocation_key": plan.semantic_keys.get(context.index),
                },
            )
            return await self._persist_preflight(
                state,
                context,
                observation,
                invocation_outcome="no_progress",
            )
        if context.item.call.name not in plan.allowlist:
            observation = _terminal_observation(
                state,
                context,
                status="forbidden_tool",
                error_code="tool_not_allowlisted",
                summary="The requested capability is not allowed for this step.",
            )
            settled = await self._persist_preflight(state, context, observation)
            return ReadCallResult(
                settled.observation,
                settled.persisted_observation,
                settled.attempt_cost,
                hard_terminal_reason="tool_not_allowlisted",
            )
        return None

    async def _persist_preflight(
        self,
        state: AgentState,
        context: _CallContext,
        observation: ObservationEnvelope,
        *,
        invocation_outcome: str | None = None,
    ) -> ReadCallResult:
        reserved = await self.host._reserve_external(
            state,
            "tool_preflight",
            logical_invocation_id=context.invocation_id,
        )
        persisted = await self.host._finish_tool_terminal(
            reserved,
            invocation_id=context.invocation_id,
            observation=observation,
            attempt_status="failed",
            invocation_outcome=invocation_outcome,
            trusted_retrieval_intent=context.trusted_retrieval_intent,
        )
        return ReadCallResult(observation, persisted, 1)

    async def _execute_transport(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
    ) -> ReadCallResult:
        logical_invocation_id = context.logical_invocation_id
        if (
            self.host.session is not None
            and plan.lease is not None
            and context.invocation_id is not None
        ):
            invocation = await self.host.session.get(ToolInvocation, context.invocation_id)
            logical_invocation_id = self.host._durable_read_invocation_logical_id(
                state=state,
                lease=plan.lease,
                invocation=invocation,
                provider_tool_call_id=context.item.tool_call_id,
                tool_name=context.item.call.name,
                arguments_hash=canonical_json_hash(
                    context.item.call.arguments.model_dump(mode="json")
                ),
            )
        transport_ordinal = await self._next_transport_ordinal(plan, context)
        if transport_ordinal is None:
            observation = _terminal_observation(
                state,
                context,
                status="unavailable",
                error_code="tool_transport_budget_exhausted",
                summary="The read capability exhausted its bounded transport retries.",
                attempt_index=2,
            )
            persisted = await self.host._terminalize_tool_without_attempt(
                plan.lease,
                invocation_id=context.invocation_id,
                observation=observation,
                trusted_retrieval_intent=context.trusted_retrieval_intent,
            )
            return ReadCallResult(
                observation,
                persisted,
                0,
                hard_terminal_reason="tool_transport_budget_exhausted",
            )

        reserved = await self.host._reserve_external(
            state,
            "read_mcp",
            logical_invocation_id=context.invocation_id,
            transport_ordinal=transport_ordinal,
        )
        await self._mark_executing(plan, context)
        observation = await self._send_transport(
            state,
            plan,
            context,
            reservation=reserved,
            logical_invocation_id=logical_invocation_id,
            transport_ordinal=transport_ordinal,
        )
        attempt_cost = 1
        should_retry = (
            observation.status in {"timeout", "unavailable"}
            and transport_ordinal < 2
            and plan.durable_attempt_base
            + progress.attempts_used
            + (len(plan.calls) - context.index)
            < MAX_TOOL_ATTEMPTS
        )
        if should_retry:
            await self.host._finish_external(
                reserved,
                status="failed",
                error_code=observation.error_code,
                transport_lifecycle=observation.transport_lifecycle,
            )
            try:
                await self.host.gateway.rehandshake_read(
                    failed_generation=self._failed_generation(observation)
                )
            except Exception:
                observation = observation.model_copy(
                    update={
                        "retryable": False,
                        "error_code": "mcp_rehandshake_failed",
                        "safe_error_summary": "The read capability could not be safely restored.",
                    }
                )
                persisted = await self.host._terminalize_tool_without_attempt(
                    plan.lease,
                    invocation_id=context.invocation_id,
                    observation=observation,
                    trusted_retrieval_intent=context.trusted_retrieval_intent,
                )
            else:
                retry_ordinal = transport_ordinal + 1
                retry = await self.host._reserve_external(
                    state,
                    "read_mcp",
                    logical_invocation_id=context.invocation_id,
                    transport_ordinal=retry_ordinal,
                )
                observation = await self._send_transport(
                    state,
                    plan,
                    context,
                    reservation=retry,
                    logical_invocation_id=logical_invocation_id,
                    transport_ordinal=retry_ordinal,
                )
                attempt_cost = 2
                persisted = await self.host._finish_tool_terminal(
                    retry,
                    invocation_id=context.invocation_id,
                    observation=observation,
                    attempt_status="succeeded" if observation.status == "ok" else "failed",
                    trusted_retrieval_intent=context.trusted_retrieval_intent,
                )
        else:
            persisted = await self.host._finish_tool_terminal(
                reserved,
                invocation_id=context.invocation_id,
                observation=observation,
                attempt_status="succeeded" if observation.status == "ok" else "failed",
                trusted_retrieval_intent=context.trusted_retrieval_intent,
            )
        return ReadCallResult(
            observation,
            persisted,
            attempt_cost,
            hard_terminal_reason=_hard_terminal_read_reason(observation),
            transport_calls=1,
        )

    async def _next_transport_ordinal(
        self,
        plan: _BatchPlan,
        context: _CallContext,
    ) -> int | None:
        if (
            self.host.session is not None
            and plan.lease is not None
            and context.invocation_id is not None
        ):
            return await ToolLedger(self.host.session).next_transport_ordinal(
                plan.lease,
                context.invocation_id,
            )
        return 1

    async def _mark_executing(self, plan: _BatchPlan, context: _CallContext) -> None:
        if self.host.session is None or plan.lease is None or context.invocation_id is None:
            return
        await ToolLedger(self.host.session).mark_executing(plan.lease, context.invocation_id)
        await self.host.session.commit()

    async def _send_transport(
        self,
        state: AgentState,
        plan: _BatchPlan,
        context: _CallContext,
        *,
        reservation: Any,
        logical_invocation_id: str | None,
        transport_ordinal: int,
    ) -> ObservationEnvelope:
        return await ReadTransportExecutor(self.host).send(
            ReadTransportRequest(
                state=state,
                item=context.item,
                reservation=reservation,
                logical_invocation_id=logical_invocation_id,
                transport_ordinal=transport_ordinal,
                round_index=plan.round_index,
            )
        )

    @staticmethod
    def _failed_generation(observation: ObservationEnvelope) -> int | None:
        if not isinstance(observation.transport_lifecycle, dict):
            return None
        generation = observation.transport_lifecycle.get("session_generation")
        return generation if isinstance(generation, int) else None

    async def _settle_one(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
        result: ReadCallResult,
    ) -> None:
        if result.hard_terminal_reason is not None:
            progress.hard_terminal_reason = result.hard_terminal_reason
        payload = self._observation_payload(state, plan, progress, context, result)
        TOOL_OBSERVATIONS.labels(
            tool=result.observation.tool_name,
            status=result.observation.status,
        ).inc()
        progress.observations.append(payload)
        self._qualify_obligation(state, plan, progress, context)
        progress.attempts_used += result.attempt_cost
        progress.transport_calls += result.transport_calls
        progress.durable_success_replays += result.durable_success_replays
        await self.host._event(
            state,
            "tool_observation",
            payload,
            visibility="customer",
            tool_call_id=context.item.tool_call_id,
            tool_round=plan.round_index,
        )

    def _observation_payload(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
        result: ReadCallResult,
    ) -> dict[str, Any]:
        payload = result.observation.model_dump(mode="json")
        if context.trusted_retrieval_intent is not None:
            payload["trusted_retrieval_intent"] = context.trusted_retrieval_intent
        payload["trusted_scope"] = {
            "tenant_id": state["tenant_id"],
            "customer_id": state["customer_id"],
            "scope_hash": (
                plan.admission_payload.get("scope_hash")
                or hashlib.sha256(
                    json.dumps(
                        {
                            "customer_id": state["customer_id"],
                            "tenant_id": state["tenant_id"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            ),
        }
        if (
            plan.admission is not None
            and plan.action_spec is not None
            and context.obligation_id is not None
        ):
            obligation = next(
                (
                    entry
                    for entry in plan.action_spec.obligations
                    if entry.obligation_id == context.obligation_id
                ),
                None,
            )
            if obligation is not None and obligation.resource_ref_argument is not None:
                arguments = context.item.call.arguments.model_dump(mode="json")
                payload["request_binding"] = {
                    "arguments_hash": canonical_json_hash(arguments),
                    "resource_ref": arguments.get(obligation.resource_ref_argument),
                }
        payload["remaining_budget"] = {
            "llm_calls": max(0, MAX_LLM_CALLS - state["llm_calls"]),
            "tool_rounds": max(0, MAX_TOOL_ROUNDS - (plan.round_index + 1)),
            "tool_attempts": max(
                0,
                MAX_TOOL_ATTEMPTS
                - (plan.durable_attempt_base + progress.attempts_used + result.attempt_cost),
            ),
        }
        if result.persisted_observation is not None and context.invocation_id is not None:
            payload.update(
                {
                    "invocation_id": context.invocation_id,
                    "observation_id": result.persisted_observation.id,
                    "observation_content_hash": result.persisted_observation.content_hash,
                    "turn_group_id": state.get("turn_group_id"),
                }
            )
        return payload

    def _qualify_obligation(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        context: _CallContext,
    ) -> None:
        if (
            plan.action_spec is None
            or plan.admission is None
            or context.obligation_id is None
            or progress.hard_terminal_reason is not None
        ):
            return
        ledger = evaluate_action_obligations(
            action_spec=plan.action_spec,
            admission=plan.admission,
            observations=[*state.get("tool_observations", []), *progress.observations],
            run_id=state["run_id"],
            citation_bindings=[],
            provider_attempt_id=None,
        )
        entry = next(
            (item for item in ledger.obligations if item.obligation_id == context.obligation_id),
            None,
        )
        if entry is not None and entry.status in {"satisfied", "read_qualified"}:
            progress.qualified_obligation_ids.add(context.obligation_id)

    async def _close_batch(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
    ) -> AgentState:
        if self.host.session is not None and plan.lease is not None and state.get("turn_group_id"):
            await ToolLedger(self.host.session).close_turn(plan.lease, state["turn_group_id"])
            await self.host.session.commit()
        all_observations = [*state.get("tool_observations", []), *progress.observations]
        await self.host._transition(
            state,
            status="running",
            checkpoint_stage="evidence_collected",
            tool_rounds=plan.round_index,
            tool_attempts=plan.durable_attempt_base + progress.attempts_used,
            llm_calls=state["llm_calls"],
        )
        delta = self._state_delta(state, plan, progress, all_observations)
        stop_reason = self._stop_reason(progress)
        if stop_reason is not None:
            if stop_reason == "semantic_no_progress":
                await self.host._event(
                    state,
                    "semantic_no_progress",
                    {
                        "reason_code": "no_new_obligation_progress",
                        "rejected_tool_count": len(progress.observations),
                    },
                    visibility="customer",
                    status="failed",
                )
            delta.update(
                await safe_stop(
                    self.host,
                    cast(AgentState, {**state, **delta}),
                    stop_reason,
                )
            )
        return delta

    def _state_delta(
        self,
        state: AgentState,
        plan: _BatchPlan,
        progress: _BatchProgress,
        all_observations: list[dict[str, Any]],
    ) -> AgentState:
        turns = list(state.get("provider_turns", []))
        turns.extend(self._provider_tool_turn(item) for item in progress.observations)
        fingerprint_state = cast(
            AgentState,
            {**state, "tool_observations": all_observations},
        )
        completed = [self.host._fingerprint(item.call, fingerprint_state) for item in plan.calls]
        current_message = str(state.get("redacted_message", ""))
        comparison_requested, comparison_complete = self.host._knowledge_comparison_state(
            all_observations,
            current_message=current_message,
        )
        effective = self.host._effective_knowledge_observations(
            all_observations,
            current_message=current_message,
        )
        return AgentState(
            tool_observations=all_observations,
            latest_observations=progress.observations,
            provider_turns=turns,
            executed_fingerprints=[*plan.fingerprints, *completed],
            tool_rounds=plan.round_index,
            tool_attempts=plan.durable_attempt_base + progress.attempts_used,
            evidence=[
                {
                    **item,
                    "index_version": observation.get("data", {}).get("index_version"),
                    "invocation_id": observation.get("invocation_id"),
                    "observation_id": observation.get("observation_id"),
                }
                for observation in all_observations
                if observation.get("tool_name") == "search_knowledge"
                for item in observation.get("data", {}).get("evidence", [])
            ],
            evidence_conflict=any(
                bool(observation.get("data", {}).get("conflict"))
                or observation.get("data", {}).get("refusal_reason")
                in {
                    "unresolved_published_version_conflict",
                    "conflicting_current_evidence",
                    "historical_interval_ambiguous",
                }
                for observation in effective
            ),
            knowledge_comparison_requested=comparison_requested,
            knowledge_comparison_complete=comparison_complete,
        )

    @staticmethod
    def _provider_tool_turn(observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": observation["tool_call_id"],
            "content": json.dumps(
                {
                    "tool_name": observation.get("tool_name"),
                    "status": observation.get("status"),
                    "source_refs": observation.get("source_refs", []),
                    "result_hash": observation.get("result_hash"),
                    "evidence_ids": [
                        item.get("evidence_id")
                        for item in observation.get("data", {}).get("evidence", [])
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _stop_reason(progress: _BatchProgress) -> str | None:
        if any(
            item.get("error_code") == "mcp_rehandshake_failed" for item in progress.observations
        ):
            return "mcp_rehandshake_failed"
        if progress.hard_terminal_reason is not None:
            return progress.hard_terminal_reason
        if progress.transport_calls == 0 and progress.durable_success_replays == 0:
            return "semantic_no_progress"
        return None


__all__ = ["ReadLoopHost", "ReadLoopNodes"]
