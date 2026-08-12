from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("backend/src/supportguard")
OWNER_PATH = SOURCE_ROOT / "agent" / "decision_repair.py"
DECISION_PATH = SOURCE_ROOT / "agent" / "decision.py"
LEGACY_MIXIN_PATH = SOURCE_ROOT / "agent" / "nodes" / "decision_support.py"


def _class_methods(path: Path, class_name: str) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text())
    owner = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_terminal_repair_has_one_physical_owner() -> None:
    definition_sites = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.ClassDef) and node.name == "DecisionRepair"
            for node in ast.parse(path.read_text()).body
        )
    ]

    assert definition_sites == [OWNER_PATH]
    assert "def _repair_terminal_decision" not in LEGACY_MIXIN_PATH.read_text()
    assert "_repair_terminal_decision" not in DECISION_PATH.read_text()


def test_decision_node_calls_the_repair_owner_directly() -> None:
    source = DECISION_PATH.read_text()

    assert "DecisionRepair(cast(DecisionRepairHost, self.host)).repair(" in source
    assert "supportguard.agent.decision_repair import" in source


def test_terminal_repair_has_named_bounded_stages() -> None:
    source = OWNER_PATH.read_text()
    methods = _class_methods(OWNER_PATH, "DecisionRepair")
    stages = (
        "_prepare",
        "_call_and_validate",
        "_call_grounded",
        "_call_generic",
        "_manifest",
        "_settle_failure",
        "_settle_success",
    )

    assert set(stages) <= methods.keys()
    assert methods["repair"].end_lineno is not None
    assert methods["repair"].end_lineno - methods["repair"].lineno + 1 < 80
    for name in stages:
        method = methods[name]
        assert method.end_lineno is not None
        assert method.end_lineno - method.lineno + 1 < 200

    repair_source = ast.get_source_segment(source, methods["repair"])
    assert repair_source is not None
    assert "_impl" not in repair_source
    assert repair_source.index("_prepare") < repair_source.index("_call_and_validate")
    assert repair_source.index("_call_and_validate") < repair_source.index("_settle_success")
