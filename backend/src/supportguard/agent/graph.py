from __future__ import annotations

from typing import Any, Protocol, cast

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.context import ContextAssembler, ContextBudget
from supportguard.agent.contracts import canonical_runtime_manifest
from supportguard.agent.decision import DecisionNodeHost, DecisionNodes
from supportguard.agent.nodes.action_flow import ActionFlowHost, ActionFlowNodes
from supportguard.agent.nodes.approval import ApprovalNodeHost, ApprovalNodes
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.nodes.finalization import FinalizationHost, FinalizationNodes
from supportguard.agent.nodes.intake import IntakeNodeHost, IntakeNodes
from supportguard.agent.state import AgentState as AgentState
from supportguard.agent.tool_loop import ReadLoopHost, ReadLoopNodes
from supportguard.config import Settings
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.providers.base import (
    StructuredProvider,
)
from supportguard.rag.service import RetrievalService
from supportguard.tools.gateway import (
    ToolGateway,
)


class ApprovalHandler(Protocol):
    async def handle(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        decision: dict[str, Any],
        trace_id: str,
        publication_state: dict[str, Any],
    ) -> dict[str, Any]: ...


class MemoryWriterProtocol(Protocol):
    async def persist(self, state: AgentState) -> None: ...


class HistoryLoaderProtocol(Protocol):
    async def load(self, *, customer_id: str, issue_type: str) -> list[dict[str, Any]]: ...


class SupportGraph:
    def __init__(
        self,
        *,
        provider: StructuredProvider,
        retrieval: RetrievalService | None,
        gateway: ToolGateway,
        budget: ContextBudget | None = None,
        approval_handler: ApprovalHandler | None = None,
        checkpointer: Any = None,
        memory_writer: MemoryWriterProtocol | None = None,
        history_loader: HistoryLoaderProtocol | None = None,
        session: AsyncSession | None = None,
        test_capability: TestRuntimeCapability | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.retrieval = retrieval  # retained only as an explicit non-Agent compatibility boundary
        self.memory_writer = memory_writer
        runtime_budget = budget or ContextBudget()
        runtime_settings = settings or Settings(
            _env_file=None,
            app_env="test",
            code_version="development",
        )
        runtime_manifest = canonical_runtime_manifest(
            settings=runtime_settings,
            model=provider.model,
            provider_mode=provider.mode,
            tool_call_mode=provider.tool_call_mode,
            context_version=runtime_budget.version,
        )
        self.runtime = AgentRuntimeServices(
            provider=provider,
            gateway=gateway,
            budget=runtime_budget,
            context_assembler=ContextAssembler(runtime_budget),
            approval_handler=approval_handler,
            history_loader=history_loader,
            session=session,
            test_capability=test_capability,
            settings=runtime_settings,
            runtime_manifest=runtime_manifest,
        )
        self.runtime_manifest = self.runtime.runtime_manifest
        self.segment_events = self.runtime.segment_events
        self.action_flow_nodes = ActionFlowNodes(cast(ActionFlowHost, self.runtime))
        self.approval_nodes = ApprovalNodes(cast(ApprovalNodeHost, self.runtime))
        self.decision_nodes = DecisionNodes(cast(DecisionNodeHost, self.runtime))
        self.finalization_nodes = FinalizationNodes(cast(FinalizationHost, self.runtime))
        self.intake_nodes = IntakeNodes(cast(IntakeNodeHost, self.runtime))
        self.read_loop_nodes = ReadLoopNodes(cast(ReadLoopHost, self.runtime))
        self.compiled = self._build().compile(checkpointer=checkpointer)

    async def run(self, state: AgentState, config: Any = None) -> AgentState:
        return cast(AgentState, await self.compiled.ainvoke(state, config))

    def _build(self) -> StateGraph[AgentState]:
        graph = StateGraph(AgentState)
        graph.add_node("ingest_ticket", self.intake_nodes.ingest_ticket)
        graph.add_node("redact_pii", self.intake_nodes.redact)
        graph.add_node(
            "load_conversation_action_state",
            self.intake_nodes.load_conversation_action_state,
        )
        graph.add_node("classify_ticket", self.intake_nodes.classify)
        graph.add_node("resolve_action_admission", self.intake_nodes.resolve_action_admission)
        graph.add_node("load_relevant_history", self.intake_nodes.load_history)
        graph.add_node(
            "evaluate_action_obligations",
            self.action_flow_nodes.evaluate_obligations,
        )
        graph.add_node("agent_decide", self.decision_nodes.agent_decide)
        graph.add_node("execute_read_tools_bounded", self.read_loop_nodes.execute_reads)
        graph.add_node(
            "bind_evidence_and_synthesize",
            self.action_flow_nodes.bind_evidence_and_synthesize,
        )
        graph.add_node(
            "explain_terminal_business_outcome",
            self.action_flow_nodes.explain_terminal_business_outcome,
        )
        graph.add_node("assemble_action_candidate", self.action_flow_nodes.assemble_action)
        graph.add_node("policy_gate", self.action_flow_nodes.policy)
        graph.add_node("call_action_proposal_mcp", self.approval_nodes.create_proposal)
        graph.add_node("await_human_approval", self.approval_nodes.await_human_approval)
        graph.add_node(
            "revalidate_and_execute_approved_action",
            self.approval_nodes.execute_approved_action,
        )
        graph.add_node("prepare_final_outcome", self.finalization_nodes.finalize)
        graph.add_node(
            "persist_final_state_memory_and_audit",
            self.finalization_nodes.persist_memory,
        )
        graph.add_edge(START, "ingest_ticket")
        graph.add_edge("ingest_ticket", "redact_pii")
        graph.add_edge("redact_pii", "load_conversation_action_state")
        graph.add_edge("load_conversation_action_state", "classify_ticket")
        graph.add_edge("classify_ticket", "resolve_action_admission")
        graph.add_edge("resolve_action_admission", "load_relevant_history")
        graph.add_edge("load_relevant_history", "evaluate_action_obligations")
        graph.add_conditional_edges(
            "evaluate_action_obligations",
            self.action_flow_nodes.route_obligations,
            {
                "decide": "agent_decide",
                "synthesize": "bind_evidence_and_synthesize",
                "assemble": "assemble_action_candidate",
                "terminal": "explain_terminal_business_outcome",
                "policy": "policy_gate",
            },
        )
        graph.add_conditional_edges(
            "agent_decide",
            self.decision_nodes.route_decision,
            {
                "tools": "execute_read_tools_bounded",
                "obligations": "evaluate_action_obligations",
                "policy": "policy_gate",
            },
        )
        graph.add_edge("execute_read_tools_bounded", "evaluate_action_obligations")
        graph.add_edge("bind_evidence_and_synthesize", "assemble_action_candidate")
        graph.add_edge("explain_terminal_business_outcome", "policy_gate")
        graph.add_edge("assemble_action_candidate", "policy_gate")
        graph.add_conditional_edges(
            "policy_gate",
            self.action_flow_nodes.route_policy,
            {
                "replan": "agent_decide",
                "finalize": "prepare_final_outcome",
                "proposal": "call_action_proposal_mcp",
            },
        )
        graph.add_conditional_edges(
            "call_action_proposal_mcp",
            self.approval_nodes.route_after_proposal,
            {"approval": "await_human_approval", "finalize": "prepare_final_outcome"},
        )
        graph.add_conditional_edges(
            "await_human_approval",
            self.approval_nodes.route_human_decision,
            {
                "execute": "revalidate_and_execute_approved_action",
                "finalize": "prepare_final_outcome",
            },
        )
        graph.add_edge("revalidate_and_execute_approved_action", "prepare_final_outcome")
        graph.add_edge("prepare_final_outcome", "persist_final_state_memory_and_audit")
        graph.add_edge("persist_final_state_memory_and_audit", END)
        return graph
