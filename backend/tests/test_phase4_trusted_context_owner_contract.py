from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import cast

from supportguard.agent.context import build_trusted_task_state
from supportguard.agent.state import AgentState

SOURCE_ROOT = Path("backend/src/supportguard")
OWNER_PATH = SOURCE_ROOT / "agent" / "context.py"
CONSUMER_PATHS = (
    SOURCE_ROOT / "agent" / "decision.py",
    SOURCE_ROOT / "agent" / "decision_repair.py",
    SOURCE_ROOT / "agent" / "nodes" / "action_flow.py",
)


def test_trusted_task_state_has_one_public_owner_and_no_graph_callback() -> None:
    owner_source = OWNER_PATH.read_text()

    assert "def build_trusted_task_state" in owner_source
    for path in SOURCE_ROOT.rglob("*.py"):
        source = path.read_text()
        assert "_trusted_agent_task_state" not in source

    for path in CONSUMER_PATHS:
        source = path.read_text()
        assert "build_trusted_task_state(" in source
        assert "supportguard.agent.context import" in source


def test_trusted_context_selectors_are_not_duplicated_in_runtime_mixins() -> None:
    private_names = (
        "_usable_current_knowledge_observation",
        "_authoritative_read_only_fact_observation",
        "_authoritative_current_account_observation",
        "_authoritative_fact_completes_current_request",
        "_latest_assistant_history_message",
    )

    for path in SOURCE_ROOT.rglob("*.py"):
        source = path.read_text()
        for name in private_names:
            assert name not in source


def test_trusted_task_state_has_named_bounded_stages() -> None:
    tree = ast.parse(OWNER_PATH.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    stages = (
        "_base_trusted_task_state",
        "_add_completed_evidence_guidance",
        "_previous_rejection_guidance",
        "build_trusted_task_state",
    )

    assert set(stages) <= functions.keys()
    for name in stages:
        function = functions[name]
        assert function.end_lineno is not None
        assert function.end_lineno - function.lineno + 1 < 200


def test_trusted_task_state_is_deterministic_and_never_grants_action_authority() -> None:
    state = cast(
        AgentState,
        {
            "ticket_id": "ticket_context",
            "customer_id": "customer_context",
            "classification": {
                "issue_type": "billing_refund",
                "risk": "high",
                "policy_boundary": "allowed",
                "requested_action": "none",
            },
            "current_actions": [
                {
                    "action_type": "refund",
                    "projection_status": "pending",
                    "resource_id": "bill_context",
                }
            ],
            "action_admission": {"status": "none"},
            "action_obligation_ledger": {},
            "evidence_assessment": {"missing_groups": []},
        },
    )
    before = deepcopy(state)

    first = build_trusted_task_state(state)
    second = build_trusted_task_state(state)

    assert first == second
    assert state == before
    assert first["current_actions"] == state["current_actions"]
    assert first["current_actions_grant_action_authority"] is False
