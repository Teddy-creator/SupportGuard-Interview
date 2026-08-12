from pathlib import Path

import pytest

from supportguard.contracts.capabilities import (
    POLICY_TOOL_INPUTS,
    READ_TOOL_INPUTS,
    RUNTIME_COMMAND_INPUTS,
)
from supportguard.contracts.testing import (
    TestRuntimeCapability,
    issue_test_runtime_capability,
)


def test_knowledge_search_model_schema_has_no_trusted_filters() -> None:
    schema = READ_TOOL_INPUTS["search_knowledge"].model_json_schema()
    assert set(schema["properties"]) == {"query"}


def test_runtime_commands_are_not_model_or_mcp_tools() -> None:
    all_mcp_tools = READ_TOOL_INPUTS.keys() | POLICY_TOOL_INPUTS.keys()
    assert not (all_mcp_tools & RUNTIME_COMMAND_INPUTS.keys())


def test_fixture_capability_cannot_be_forged_or_issued_for_non_test_runtime() -> None:
    with pytest.raises(RuntimeError, match="unavailable outside"):
        issue_test_runtime_capability(testing=False)
    with pytest.raises(RuntimeError, match="not issued"):
        TestRuntimeCapability(object())


def test_product_runtime_has_no_string_triggered_fixture_bypass() -> None:
    product_root = Path(__file__).parents[1] / "src/supportguard"
    forbidden = ("fixture_sync", "allow_unbound_context_fixture")
    matches = [
        str(path)
        for path in product_root.rglob("*.py")
        if path.name != "v5_retrieval.py"
        and any(marker in path.read_text(encoding="utf-8") for marker in forbidden)
    ]
    assert matches == []
