from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

from supportguard.validation.public_mirror import load_public_mirror_provenance

CONTRACT_ROOT = Path("validation/contracts/interview_v2")
DISPOSITION_PATH = CONTRACT_ROOT / "test-disposition.v1.json"
ARCHIVE_SOURCE_COMMIT = "328bc8606fdfbe50c9f3530646e72c1c21269c12"
GIT = shutil.which("git") or "/usr/bin/git"


def _test_node_exists(node_id: str) -> bool:
    path_raw, separator, function_name = node_id.partition("::")
    if not separator:
        return False
    path = Path(path_raw)
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        for node in tree.body
    )


def _archive_source(path: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git command and frozen commit
        [GIT, "show", f"{ARCHIVE_SOURCE_COMMIT}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _archive_test_node_exists(node_id: str) -> bool:
    if load_public_mirror_provenance(Path.cwd()) is not None:
        disposition = json.loads(DISPOSITION_PATH.read_text())
        archived_nodes = {
            node for group in disposition["groups"] for node in group["old_test_nodes"]
        }
        return node_id in archived_nodes
    path, separator, function_name = node_id.partition("::")
    if not separator:
        return False
    tree = ast.parse(_archive_source(path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        for node in tree.body
    )


def test_phase6_test_disposition_is_explicit_and_references_live_contracts() -> None:
    disposition = json.loads(DISPOSITION_PATH.read_text())
    behavior = json.loads((CONTRACT_ROOT / "behavior-characterization.v1.json").read_text())
    safety = json.loads((CONTRACT_ROOT / "safety-invariant-manifest.v1.json").read_text())
    requirement_ids = (
        {item["id"] for item in behavior["baseline_observed"]}
        | {item["requirement_id"] for item in behavior["preserved_public_contract"]}
        | {item["requirement_id"] for item in safety["invariants"]}
    )
    allowed = set(safety["test_disposition_rule"]["allowed_dispositions"])

    assert disposition["schema_version"] == "supportguard.interview_v2.test_disposition.v1"
    assert disposition["status"] == "phase6_completed"
    assert disposition["authority"]["phase6_candidate_sha"] == (
        "30254587585fa2169cab071a926c501e06dac9a6"
    )
    assert disposition["authority"]["phase6_candidate_tree"] == (
        "199ca61783c5857cc95f83a468f1b80a5a313d81"
    )
    assert disposition["groups"]
    assert len({item["old_test_group"] for item in disposition["groups"]}) == len(
        disposition["groups"]
    )

    for group in disposition["groups"]:
        assert group["disposition"] in allowed
        assert group["disposition"] == "replaced_by_new_contract"
        assert set(group["requirement_ids"]) <= requirement_ids
        assert group["old_test_nodes"]
        assert group["replacement_test_nodes"]
        assert not any(_test_node_exists(node) for node in group["old_test_nodes"])
        assert all(_archive_test_node_exists(node) for node in group["old_test_nodes"])
        assert all(_test_node_exists(node) for node in group["replacement_test_nodes"])
        assert group["before_result"] == {
            "status": "pass",
            "candidate": "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb",
            "evidence_class": "archive_runtime_baseline",
        }
        assert group["after_result"] == {
            "old_test_current_workspace": "absent",
            "old_test_archive_source": "git_recoverable",
            "replacement_current_contract": "pass",
            "historical_result_rewritten": False,
        }

    safety_mappings = disposition["safety_keep_mappings"]
    assert {item["requirement_ids"][0] for item in safety_mappings} == {
        f"IE-S{index:02d}" for index in range(1, 15)
    }
    assert len({item["old_test_group"] for item in safety_mappings}) == 14
    for mapping in safety_mappings:
        assert mapping["disposition"] == "keep"
        assert mapping["current_test_nodes"]
        assert all(_test_node_exists(node) for node in mapping["current_test_nodes"])


def test_frozen_oracles_keep_their_historical_numbers() -> None:
    if load_public_mirror_provenance(Path.cwd()) is not None:
        behavior = json.loads((CONTRACT_ROOT / "behavior-characterization.v1.json").read_text())
        topology = behavior["baseline_observed"][0]["facts"]
        capabilities = behavior["baseline_observed"][2]["facts"]
        assert topology["langgraph_node_count"] == 19
        assert topology["contains_legacy_escalation_node"] is True
        assert capabilities["policy_only_proposal_capability_count"] == 4
        assert capabilities["legacy_escalation_capability_present"] is True
        return
    graph_source = _archive_source("backend/tests/test_v16_structural_characterization.py")
    capability_source = _archive_source("backend/tests/test_v12_capability_contracts.py")

    assert '"langgraph_node_count": 19' in graph_source
    assert '"contains_legacy_escalation_node": True' in graph_source
    assert '"policy_only_proposal_capability_count": 4' in graph_source
    assert '"create_support_escalation" in FROZEN_POLICY_TOOL_NAMES' in capability_source
    assert "len(FROZEN_POLICY_TOOL_NAMES) == 4" in capability_source
