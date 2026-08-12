from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from supportguard.agent.nodes.action_flow import ActionFlowNodes

GRAPH_PATH = Path("backend/src/supportguard/agent/graph.py")
APPROVAL_NODE_PATH = Path("backend/src/supportguard/agent/nodes/approval.py")


def test_support_graph_contains_only_composition_and_run_entrypoint() -> None:
    source = GRAPH_PATH.read_text()
    tree = ast.parse(source)
    graph_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SupportGraph"
    )
    methods = {
        node.name
        for node in graph_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert methods == {"__init__", "run", "_build"}
    assert graph_class.bases == []
    assert source.count("StateGraph(AgentState)") == 1
    assert "DecisionSupportMixin" not in source
    assert "return await self." not in source
    assert "_impl" not in source
    assert source.count("self.runtime))") == 6
    assert "AgentRuntimeServices(" in source


def test_current_graph_has_one_generic_action_proposal_branch() -> None:
    source = GRAPH_PATH.read_text()
    approval_source = APPROVAL_NODE_PATH.read_text()

    assert 'graph.add_node("call_action_proposal_mcp"' in source
    assert '"proposal": "call_action_proposal_mcp"' in source
    assert "call_refund_proposal_mcp" not in source
    assert approval_source.count("build_action_candidate(") == 1
    assert approval_source.count("ActionService(") == 2
    assert "call_refund_proposal_mcp" not in approval_source
    assert "call_escalation_mcp" not in source
    assert "create_support_escalation" not in source


def test_current_policy_routes_all_approved_action_types_through_generic_proposal() -> None:
    nodes = ActionFlowNodes(cast(Any, object()))

    assert nodes.route_policy({"policy_route": "await_human_approval"}) == "proposal"
    assert nodes.route_policy({"policy_route": "manual_takeover"}) == "finalize"
