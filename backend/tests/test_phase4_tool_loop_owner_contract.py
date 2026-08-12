from __future__ import annotations

import ast
from pathlib import Path

from supportguard.agent import tool_loop as tool_loop_owner
from supportguard.agent.nodes import read_loop as read_loop_facade

SOURCE_ROOT = Path("backend/src/supportguard")
OWNER_PATH = SOURCE_ROOT / "agent" / "tool_loop.py"
FACADE_PATH = SOURCE_ROOT / "agent" / "nodes" / "read_loop.py"
CONTRACTS_PATH = SOURCE_ROOT / "agent" / "tool_loop_contracts.py"
TRANSPORT_PATH = SOURCE_ROOT / "agent" / "tool_transport.py"
GRAPH_PATH = SOURCE_ROOT / "agent" / "graph.py"


def test_tool_loop_owner_and_compatibility_facade_preserve_identity() -> None:
    assert read_loop_facade.ReadLoopHost is tool_loop_owner.ReadLoopHost
    assert read_loop_facade.ReadLoopNodes is tool_loop_owner.ReadLoopNodes

    definition_sites = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.ClassDef) and node.name == "ReadLoopNodes"
            for node in ast.parse(path.read_text()).body
        )
    ]
    assert definition_sites == [OWNER_PATH]


def test_current_runtime_uses_new_tool_loop_owner_directly() -> None:
    facade_imports = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if path != FACADE_PATH and "supportguard.agent.nodes.read_loop import" in path.read_text()
    ]

    assert facade_imports == []
    assert "supportguard.agent.tool_loop import" in GRAPH_PATH.read_text()


def test_tool_loop_has_named_bounded_stages() -> None:
    source = OWNER_PATH.read_text()
    tree = ast.parse(source)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReadLoopNodes"
    )
    methods = {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    stages = (
        "_plan_and_validate_batch",
        "_reserve_or_replay",
        "_execute_one",
        "_execute_transport",
        "_settle_one",
        "_close_batch",
    )

    assert set(stages) <= methods.keys()
    assert methods["execute_reads"].end_lineno is not None
    assert methods["execute_reads"].end_lineno - methods["execute_reads"].lineno + 1 < 150
    for name in stages:
        stage = methods[name]
        assert stage.end_lineno is not None
        assert stage.end_lineno - stage.lineno + 1 < 200

    execute_one = ast.get_source_segment(source, methods["_execute_one"])
    assert execute_one is not None
    assert execute_one.index("_reserve_or_replay") < execute_one.index("_execute_transport")


def test_tool_loop_dependencies_do_not_own_budget_or_durable_settlement() -> None:
    contracts = CONTRACTS_PATH.read_text()
    transport = TRANSPORT_PATH.read_text()
    owner = OWNER_PATH.read_text()

    assert "semantic_batch_rejections" not in contracts
    assert "terminal_observation" not in contracts
    assert "MAX_TOOL_" not in transport
    assert "_reserve_external" not in transport
    assert "_finish_tool_terminal" not in transport
    assert "_terminalize_tool_without_attempt" not in transport
    assert "semantic_batch_rejections" in owner
    assert "_finish_tool_terminal" in owner
    assert "semantic_no_progress" in owner
