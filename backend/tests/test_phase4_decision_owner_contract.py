from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest

from supportguard.agent import decision as decision_owner
from supportguard.agent.nodes import decision as decision_facade
from supportguard.agent.state import AgentState

SOURCE_ROOT = Path("backend/src/supportguard")
OWNER_PATH = SOURCE_ROOT / "agent" / "decision.py"
FACADE_PATH = SOURCE_ROOT / "agent" / "nodes" / "decision.py"
GRAPH_PATH = SOURCE_ROOT / "agent" / "graph.py"


def test_decision_owner_and_compatibility_facade_preserve_identity() -> None:
    assert decision_facade.DecisionNodeHost is decision_owner.DecisionNodeHost
    assert decision_facade.DecisionNodes is decision_owner.DecisionNodes

    definition_sites: list[Path] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.ClassDef) and node.name == "DecisionNodes"
            for node in tree.body
        ):
            definition_sites.append(path)

    assert definition_sites == [OWNER_PATH]


def test_current_runtime_uses_new_decision_owner_directly() -> None:
    facade_imports = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if path != FACADE_PATH
        and "supportguard.agent.nodes.decision import" in path.read_text()
    ]

    assert facade_imports == []
    assert "supportguard.agent.decision import" in GRAPH_PATH.read_text()


def test_decision_pipeline_has_named_bounded_stages() -> None:
    tree = ast.parse(OWNER_PATH.read_text())
    decision_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DecisionNodes"
    )
    methods = {
        node.name: node
        for node in decision_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected_stages = (
        "_terminal_predecision",
        "_admission_and_evidence_predecision",
        "_prepare_provider_decision",
        "_call_provider",
        "_validate_provider_decision",
        "_normalize_provider_decision",
        "_publish_provider_decision",
    )

    assert set(expected_stages) <= methods.keys()
    assert methods["agent_decide"].end_lineno is not None
    assert methods["agent_decide"].end_lineno - methods["agent_decide"].lineno + 1 < 150
    for stage_name in expected_stages:
        stage = methods[stage_name]
        assert stage.end_lineno is not None
        assert stage.end_lineno - stage.lineno + 1 < 200

    orchestrator_source = ast.get_source_segment(
        OWNER_PATH.read_text(), methods["agent_decide"]
    )
    assert orchestrator_source is not None
    assert "_impl" not in orchestrator_source
    positions = [orchestrator_source.index(stage_name) for stage_name in expected_stages]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_decision_pipeline_preserves_completed_run_short_circuit() -> None:
    node = decision_owner.DecisionNodes(cast(Any, object()))
    state = cast(
        AgentState,
        {
            "candidate": {"answer": "done"},
            "agent_finish_reason": "answered",
            "evidence_replan_required": False,
        },
    )

    assert await node.agent_decide(state) == {}


@pytest.mark.asyncio
async def test_decision_pipeline_preserves_deterministic_policy_rejection() -> None:
    class RejectionHost:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        async def _event(
            self,
            _state: AgentState,
            event_type: str,
            payload: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            self.events.append((event_type, payload))

    host = RejectionHost()
    node = decision_owner.DecisionNodes(cast(Any, host))
    state = cast(
        AgentState,
        {
            "classification": {"policy_boundary": "prohibited"},
            "llm_calls": 0,
            "tool_rounds": 0,
            "tool_attempts": 0,
            "step_index": 2,
        },
    )

    result = await node.agent_decide(state)

    assert result["agent_finish_reason"] == "rejected"
    assert result["policy_route"] == "reject"
    assert result["step_index"] == 3
    assert result["candidate"]["action"] == "reject"
    assert host.events[0][0] == "agent_decision"
    assert host.events[0][1]["deterministic_policy_boundary"] == "prohibited"
