from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

import pytest
from langgraph.graph import END, START
from pydantic import ValidationError

from supportguard.agent.decision import DecisionNodes
from supportguard.agent.graph import SupportGraph
from supportguard.agent.nodes import runtime_support
from supportguard.agent.nodes.action_flow import ActionFlowNodes
from supportguard.agent.nodes.approval import ApprovalNodes
from supportguard.agent.nodes.finalization import FinalizationNodes
from supportguard.agent.nodes.intake import IntakeNodes
from supportguard.agent.tool_loop import ReadLoopNodes
from supportguard.mcp import client as mcp_client
from supportguard.mcp import manager as compatibility_manager
from supportguard.mcp.action_server import mcp as action_mcp
from supportguard.mcp.runtime import (
    EXPECTED_TOOLS,
    FROZEN_SCHEMA_HASHES,
    ManagedServer,
    MCPManager,
    ToolTransport,
)
from supportguard.tools.capabilities import (
    ACTION_PROPOSAL_CAPABILITIES,
    CAPABILITIES,
    READ_CAPABILITIES,
    RUNTIME_EFFECT_CAPABILITIES,
)
from supportguard.tools.gateway import ActionToolCall, ToolGateway

EXPECTED_READ_CAPABILITIES = frozenset(
    {
        "check_service_status",
        "query_account",
        "query_api_key_metadata",
        "query_api_usage",
        "query_billing_record",
        "query_incident_impact",
        "query_request_trace",
        "query_subscription",
        "search_knowledge",
    }
)
EXPECTED_PROPOSAL_CAPABILITIES = frozenset(
    {
        "propose_api_key_revocation",
        "propose_entitlement_change",
        "propose_refund",
    }
)
EXPECTED_RUNTIME_CAPABILITIES = frozenset(
    {
        "execute_api_key_revocation",
        "execute_entitlement_change",
        "execute_refund",
    }
)


def _current_graph() -> object:
    # Foundation probe only: Phase 4D replaces this private builder check with
    # typed public-stage tests.  Deliberately do not freeze a node count here.
    graph_shell = SupportGraph.__new__(SupportGraph)
    graph_shell.action_flow_nodes = ActionFlowNodes(graph_shell)
    graph_shell.approval_nodes = ApprovalNodes(graph_shell)
    graph_shell.decision_nodes = DecisionNodes(graph_shell)
    graph_shell.finalization_nodes = FinalizationNodes(graph_shell)
    graph_shell.intake_nodes = IntakeNodes(graph_shell)
    graph_shell.read_loop_nodes = ReadLoopNodes(graph_shell)
    return graph_shell._build()


def _generated_action_schema_hash(tools: list[Any]) -> str:
    payload = [
        {
            "name": tool.name,
            "input": tool.inputSchema,
            "output": tool.outputSchema,
        }
        for tool in sorted(tools, key=lambda value: value.name)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_current_capability_owner_is_exactly_nine_three_three() -> None:
    assert READ_CAPABILITIES == EXPECTED_READ_CAPABILITIES
    assert ACTION_PROPOSAL_CAPABILITIES == EXPECTED_PROPOSAL_CAPABILITIES
    assert RUNTIME_EFFECT_CAPABILITIES == EXPECTED_RUNTIME_CAPABILITIES
    assert set(CAPABILITIES) == (
        EXPECTED_READ_CAPABILITIES | EXPECTED_PROPOSAL_CAPABILITIES | EXPECTED_RUNTIME_CAPABILITIES
    )
    assert all(CAPABILITIES[name].model_visible for name in EXPECTED_READ_CAPABILITIES)
    assert all(
        CAPABILITIES[name].requires_human_approval
        for name in EXPECTED_PROPOSAL_CAPABILITIES | EXPECTED_RUNTIME_CAPABILITIES
    )
    assert "create_support_escalation" not in CAPABILITIES


def test_manager_module_is_a_compatibility_reexport_not_a_second_lifecycle_owner() -> None:
    assert compatibility_manager.MCPManager is MCPManager
    assert compatibility_manager.ToolTransport is ToolTransport
    source = inspect.getsource(compatibility_manager)
    assert "class MCPManager" not in source
    assert "class ManagedServer" not in source


def test_runtime_not_client_owns_current_initialize_and_discovery_lifecycle() -> None:
    raw_source = inspect.getsource(mcp_client.raw_mcp_session)
    compatibility_source = inspect.getsource(mcp_client.mcp_session)
    runtime_source = inspect.getsource(ManagedServer._run_session)  # noqa: SLF001
    discovery_source = inspect.getsource(ManagedServer._verify_schema)  # noqa: SLF001

    assert "session.initialize" not in raw_source
    assert "session.list_tools" not in raw_source
    assert "raw_mcp_session" in runtime_source
    assert "await session.initialize()" in runtime_source
    assert "await self.session.list_tools()" in discovery_source
    assert "await session.initialize()" in compatibility_source


@pytest.mark.asyncio
async def test_current_action_mcp_discovery_and_frozen_hash_are_exactly_three_tools() -> None:
    tools = await action_mcp.list_tools()
    discovered = {tool.name for tool in tools}

    assert EXPECTED_TOOLS == {
        "read": EXPECTED_READ_CAPABILITIES,
        "action": EXPECTED_PROPOSAL_CAPABILITIES,
    }
    assert discovered == EXPECTED_PROPOSAL_CAPABILITIES
    assert _generated_action_schema_hash(tools) == FROZEN_SCHEMA_HASHES["action"]


def test_current_graph_has_no_live_escalation_node_or_edge() -> None:
    graph = _current_graph()
    nodes = set(graph.nodes)
    edges = set(graph.edges)
    branches = str(graph.branches)

    assert START in {source for source, _ in edges}
    assert END in {target for _, target in edges}
    assert "call_escalation_mcp" not in nodes
    assert all("escalation" not in str(edge) for edge in edges)
    assert "call_escalation_mcp" not in branches


def test_current_runtime_has_no_escalation_reservation_or_graph_adapter() -> None:
    approval_source = inspect.getsource(ApprovalNodes)
    reservation_source = inspect.getsource(runtime_support.GraphRuntimeSupport._reserve_capability)

    assert "execute_safe_action" not in approval_source
    assert "EscalationCausalDecisionV2" not in reservation_source
    assert "create_support_escalation" not in reservation_source


def test_gateway_requires_an_explicit_transport_and_has_no_per_call_session_owner() -> None:
    signature = inspect.signature(ToolGateway)
    assert signature.parameters["manager"].default is inspect.Parameter.empty
    source = inspect.getsource(ToolGateway)
    assert "read_mcp_session" not in source
    assert "action_mcp_session" not in source
    assert "manager is None" not in source


@pytest.mark.asyncio
async def test_forged_live_escalation_is_rejected_before_transport() -> None:
    with pytest.raises(ValidationError):
        ActionToolCall.model_validate(
            {
                "name": "create_support_escalation",
                "arguments": {"reason": "forged"},
            }
        )

    manager = MCPManager()
    with pytest.raises(RuntimeError, match="not allowed on action MCP"):
        await manager.call(
            "action",
            "create_support_escalation",
            {},
            reconnect_once=False,
        )
