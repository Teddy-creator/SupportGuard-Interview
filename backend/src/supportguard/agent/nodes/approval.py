from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.actions.service import (
    ActionCandidate,
    ActionPipelineError,
    ActionService,
    build_action_candidate,
    build_approval_decision,
)
from supportguard.agent.nodes.finalization import SafeStopHost, safe_stop
from supportguard.agent.policy import PolicyRoute
from supportguard.agent.schemas import CandidateResponse, FinalResponse, ProposalEligibility
from supportguard.agent.state import AgentState
from supportguard.contracts.tools import ToolCallContext
from supportguard.db.models import ApprovalRequest, PolicyCapabilityResult
from supportguard.services.capability_ledger import ReservedCapability
from supportguard.services.runtime_jobs import JobLease
from supportguard.tools.gateway import ToolGateway


class ApprovalDecisionHandler(Protocol):
    async def handle(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        decision: dict[str, Any],
        trace_id: str,
        publication_state: dict[str, Any],
    ) -> dict[str, Any]: ...


class ApprovalNodeHost(SafeStopHost, Protocol):
    session: AsyncSession | None
    gateway: ToolGateway
    approval_handler: ApprovalDecisionHandler | None

    async def _transition(self, state: AgentState, **values: Any) -> None: ...

    async def _reserve_capability(
        self,
        state: AgentState,
        capability_name: str,
        *,
        model_arguments: dict[str, Any],
        observation_binding: list[dict[str, Any]],
    ) -> tuple[JobLease, ReservedCapability] | None: ...

    def _tool_context(
        self,
        state: AgentState,
        *,
        approval: bool,
        observation_binding: list[dict[str, Any]] | None = None,
        capability: ReservedCapability | None = None,
        lease: JobLease | None = None,
    ) -> ToolCallContext: ...

    async def _finish_capability(
        self,
        reservation: tuple[JobLease, ReservedCapability] | None,
        *,
        status: str,
        error_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> PolicyCapabilityResult | None: ...

    def _capability_payload(self, result: Any) -> dict[str, Any]: ...

    def _payload(self, result: Any) -> dict[str, Any]: ...

    async def _proposal_is_durable(
        self,
        state: AgentState,
        proposal: dict[str, Any],
        *,
        action_name: str,
        eligibility: ProposalEligibility,
    ) -> bool: ...

    async def _event(
        self,
        state: AgentState,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: str = "internal",
        status: str = "completed",
        tool_call_id: str | None = None,
        tool_round: int | None = None,
    ) -> None: ...

    def _render_validated_answer(
        self,
        candidate: CandidateResponse,
        *,
        route: PolicyRoute,
        finish_reason: str | None,
        integrity: bool,
        issue_type: str = "unknown",
        requested_action: str = "none",
        explicit_first_step: bool = False,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class _ActionPipelineHostAdapter:
    host: ApprovalNodeHost

    @property
    def gateway(self) -> ToolGateway:
        return self.host.gateway

    async def reserve_action_capability(
        self,
        state: Mapping[str, Any],
        capability_name: str,
        *,
        model_arguments: dict[str, Any],
        observation_binding: list[dict[str, Any]],
    ) -> tuple[JobLease, ReservedCapability] | None:
        return await self.host._reserve_capability(
            cast(AgentState, state),
            capability_name,
            model_arguments=model_arguments,
            observation_binding=observation_binding,
        )

    def action_tool_context(
        self,
        state: Mapping[str, Any],
        *,
        observation_binding: list[dict[str, Any]],
        capability: ReservedCapability | None,
        lease: JobLease | None,
    ) -> ToolCallContext:
        return self.host._tool_context(
            cast(AgentState, state),
            approval=True,
            observation_binding=observation_binding,
            capability=capability,
            lease=lease,
        )

    async def finish_action_capability(
        self,
        reservation: tuple[JobLease, ReservedCapability] | None,
        *,
        status: str,
        error_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> PolicyCapabilityResult | None:
        return await self.host._finish_capability(
            reservation,
            status=status,
            error_code=error_code,
            payload=payload,
        )

    def action_capability_payload(self, result: Any) -> dict[str, Any]:
        return self.host._capability_payload(result)

    def action_result_payload(self, result: Any) -> dict[str, Any]:
        return self.host._payload(result)


@dataclass(frozen=True, slots=True)
class ApprovalNodes:
    """Own proposal persistence, Graph interrupt and approval resume orchestration."""

    host: ApprovalNodeHost

    async def create_proposal(self, state: AgentState) -> AgentState:
        candidate = CandidateResponse.model_validate(state["candidate"])
        checkpoint_id = f"checkpoint:{state['run_id']}:awaiting_approval"
        if not state.get("job_id"):
            await self.host._transition(
                state,
                status="running",
                checkpoint_stage="proposal_ready",
                checkpoint_id=checkpoint_id,
                agent_finish_reason="proposed",
                tool_rounds=state["tool_rounds"],
                tool_attempts=state["tool_attempts"],
                llm_calls=state["llm_calls"],
            )
        eligibility = ProposalEligibility.model_validate(state.get("proposal_eligibility", {}))
        if not eligibility.eligible:
            return await safe_stop(
                self.host, state, eligibility.error_code or "proposal_eligibility_failed"
            )
        try:
            action_candidate = build_action_candidate(
                proposal_action=candidate.action,
                action_type=str(eligibility.action_type or ""),
                resource_type=eligibility.resource_type,
                resource_id=eligibility.resource_id,
                resource_version=eligibility.resource_version,
                trusted_arguments=eligibility.trusted_arguments,
                observation_binding=eligibility.observation_binding,
                citation_binding_ids=eligibility.citation_binding_ids,
            )
        except (TypeError, ValueError):
            return await safe_stop(self.host, state, "proposal_eligibility_failed")

        async def verify_durable(proposal: dict[str, Any]) -> bool:
            return await self.host._proposal_is_durable(
                state,
                proposal,
                action_name=action_candidate.policy_capability,
                eligibility=eligibility,
            )

        try:
            proposal_result = await ActionService().propose(
                host=_ActionPipelineHostAdapter(self.host),
                state=state,
                candidate=action_candidate,
                verify_durable=verify_durable,
            )
        except ActionPipelineError as exc:
            return await safe_stop(
                self.host,
                state,
                exc.code,
                error_code=exc.error_code,
            )
        proposal = dict(proposal_result.proposal)
        if (
            not state.get("job_id")
            and self.host.session is not None
            and proposal.get("approval_id")
        ):
            approval = await self.host.session.get(ApprovalRequest, str(proposal["approval_id"]))
            if approval is not None:
                approval.review_context = {
                    "original_ticket": state["user_message"],
                    "redacted_ticket": state["redacted_message"],
                    "evidence": state.get("evidence", []),
                    "tool_observations": state.get("tool_observations", []),
                    "risk": state.get("classification", {}).get("risk", "high"),
                    "policy_route": state.get("policy_route"),
                }
                await self.host.session.commit()
        if not state.get("job_id"):
            await self.host._transition(
                state,
                status="interrupted",
                checkpoint_stage="awaiting_approval",
                checkpoint_id=checkpoint_id,
                agent_finish_reason="proposed",
                tool_rounds=state["tool_rounds"],
                tool_attempts=state["tool_attempts"],
                llm_calls=state["llm_calls"],
            )
        await self.host._event(state, "proposal_drafted", proposal, visibility="customer")
        return {
            "action_candidate": action_candidate.model_dump(mode="json"),
            "action_result": proposal,
            "agent_finish_reason": "proposed",
        }

    @staticmethod
    async def await_human_approval(state: AgentState) -> AgentState:
        decision = interrupt(
            {
                "approval": state["action_result"],
                "original_ticket": state["user_message"],
                "redacted_ticket": state["redacted_message"],
                "evidence": state.get("evidence", []),
                "tool_observations": state.get("tool_observations", []),
                "risk": state.get("classification", {}).get("risk", "high"),
            }
        )
        return {"human_decision": cast(dict[str, Any], decision)}

    @staticmethod
    def route_after_proposal(state: AgentState) -> str:
        return "approval" if state.get("action_result", {}).get("proposal_id") else "finalize"

    @staticmethod
    def route_human_decision(state: AgentState) -> str:
        del state
        return "execute"

    async def execute_approved_action(self, state: AgentState) -> AgentState:
        if self.host.approval_handler is None:
            raise RuntimeError("approval handler is required to execute an approved action")
        await self.host._event(
            state,
            "human_decision",
            {"action": state["human_decision"].get("action")},
            visibility="customer",
        )
        candidate = CandidateResponse.model_validate(state["candidate"])
        action_candidate = ActionCandidate.model_validate(state["action_candidate"])
        approval_decision = build_approval_decision(
            command=state["human_decision"],
            proposal_result=state["action_result"],
        )
        route = PolicyRoute(state.get("policy_route", PolicyRoute.AWAIT_APPROVAL.value))
        publication_state = dict(state)
        publication_state["final"] = FinalResponse(
            answer=state.get("validated_answer")
            or self.host._render_validated_answer(
                candidate,
                route=route,
                finish_reason=state.get("agent_finish_reason"),
                integrity=state.get("citation_integrity", False),
            ),
            terminal_state="resolved",
            knowledge_chunk_ids=candidate.knowledge_chunk_ids,
            business_source_ids=candidate.business_source_ids,
            material_claims=candidate.material_claims,
            policy_route=route.value,
        ).model_dump(mode="json")
        execution = await ActionService().execute(
            handler=self.host.approval_handler,
            candidate=action_candidate,
            decision=approval_decision,
            trace_id=state["trace_id"],
            publication_state=publication_state,
        )
        return {
            "approval_decision": execution.decision.model_dump(mode="json"),
            "runtime_effect_result": execution.effect.model_dump(mode="json"),
            "execution_result": execution.payload,
        }
