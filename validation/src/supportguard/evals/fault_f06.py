from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import xml.etree.ElementTree as ET  # local pytest-generated JUnit  # nosec B405
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from .gate import CONTRACT_ROOT, CONTRACTS
from .phase7_common import (
    Phase7ContractError,
    atomic_write_json,
    canonical_sha256,
    require_candidate,
    require_ignored_output,
    sha256_file,
    utc_now,
)

_MCP_NODE = (
    "backend/tests/test_read_mcp.py::"
    "test_retryable_stdio_process_loss_reaps_old_pid_and_rehandshakes"
)
_POSTGRES_FILES = frozenset(
    {
        "backend/tests/test_v124_agent_recovery.py",
        "backend/tests/test_v1512_post_effect_finalizer_atomicity_postgres.py",
    }
)
_MCP_RESULT = re.compile(
    r"\[mcp-partition-result\] name=hermetic collected=(?P<collected>\d+) "
    r"executed=(?P<executed>\d+) failures=(?P<failures>\d+) "
    r"errors=(?P<errors>\d+) skipped=(?P<skipped>\d+).*orphan_count=(?P<orphans>\d+)"
)


def _load_contract(root: Path) -> dict[str, Any]:
    name, expected_hash = CONTRACTS["ie_f06"]
    path = root / CONTRACT_ROOT / name
    if sha256_file(path) != expected_hash:
        raise Phase7ContractError("ie_f06_contract_hash_mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value.get("cases", [])) != 6:
        raise Phase7ContractError("ie_f06_denominator_mismatch")
    cases = value["cases"]
    if [str(item.get("id")) for item in cases] != [f"IE-F{ordinal:02d}" for ordinal in range(1, 7)]:
        raise Phase7ContractError("ie_f06_case_identity_mismatch")
    nodes = [str(node) for case in cases for node in case.get("deterministic_test_nodes", [])]
    if not nodes or len(nodes) != len(set(nodes)):
        raise Phase7ContractError("ie_f06_test_node_inventory_invalid")
    return value


def preflight(root: Path) -> dict[str, Any]:
    contract = _load_contract(root.resolve())
    nodes = [node for case in contract["cases"] for node in case["deterministic_test_nodes"]]
    return {
        "schema": "supportguard.interview_v2.ie_f06_preflight.v1",
        "contract_sha256": CONTRACTS["ie_f06"][1],
        "cases": 6,
        "test_nodes": len(nodes),
        "fault_injected": True,
        "real_provider_calls": 0,
        "protected_holdout_accessed": False,
        "cross_encoder_executed": False,
    }


def _environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "APP_ENV",
        "TEST_DATABASE_URL",
        "TEST_FINALIZER_DATABASE_URL",
        "TEST_WORKER_DATABASE_URL",
        "MCP_READ_DATABASE_URL",
        "MCP_ACTION_DATABASE_URL",
        "TEST_REDIS_URL",
        "TEST_WORKER_REDIS_URL",
        "TEST_RECONCILER_REDIS_URL",
        "TEST_API_REDIS_URL",
        "TRANSFORMERS_OFFLINE",
    }
    output = {key: value for key, value in os.environ.items() if key in allowed}
    output.setdefault("APP_ENV", "test")
    output.setdefault("TRANSFORMERS_OFFLINE", "1")
    return output


def _completed(
    arguments: Sequence[str], *, environment: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        list(arguments),
        cwd=Path.cwd(),
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    try:
        root = ET.parse(path).getroot()  # noqa: S314  # nosec B314
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        return tuple(  # type: ignore[return-value]
            sum(int(suite.get(name, "0")) for suite in suites)
            for name in ("tests", "failures", "errors", "skipped")
        )
    except (OSError, ValueError, ET.ParseError) as exc:
        raise Phase7ContractError("ie_f06_junit_malformed") from exc


def _run_direct_node(node: str, environment: Mapping[str, str]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="supportguard-phase7-f06-") as directory:
        junit = Path(directory) / "result.xml"
        completed = _completed(
            [sys.executable, "-m", "pytest", "-q", node, f"--junitxml={junit}"],
            environment=environment,
            timeout=600,
        )
        counts = _junit_counts(junit) if junit.is_file() else (0, 0, 1, 0)
    return {
        "mode": "direct_pytest",
        "node": node,
        "counts": {
            "tests": counts[0],
            "failures": counts[1],
            "errors": counts[2],
            "skipped": counts[3],
        },
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0
        and counts[0] > 0
        and counts[1] == counts[2] == counts[3] == 0,
        "diagnostic_sha256": canonical_sha256(
            {"stdout": completed.stdout, "stderr": completed.stderr}
        ),
    }


def _run_mcp(environment: Mapping[str, str]) -> dict[str, Any]:
    completed = _completed(
        [sys.executable, "scripts/run_mcp_test_partitions.py", "hermetic"],
        environment=environment,
        timeout=1200,
    )
    match = _MCP_RESULT.search(completed.stdout)
    facts = {key: int(value) for key, value in match.groupdict().items()} if match else {}
    passed = (
        completed.returncode == 0
        and facts.get("collected") == 6
        and facts.get("executed") == 6
        and facts.get("failures") == 0
        and facts.get("errors") == 0
        and facts.get("skipped") == 0
        and facts.get("orphans") == 0
    )
    return {
        "mode": "mcp_hermetic_partition",
        "required_node": _MCP_NODE,
        "facts": facts,
        "exit_code": completed.returncode,
        "passed": passed,
        "diagnostic_sha256": canonical_sha256(
            {"stdout": completed.stdout, "stderr": completed.stderr}
        ),
    }


def _run_postgres(nodes: list[str], environment: Mapping[str, str]) -> dict[str, Any]:
    if not environment.get("TEST_DATABASE_URL"):
        raise Phase7ContractError("ie_f06_test_database_url_required")
    isolated_environment = {
        **environment,
        "SUPPORTGUARD_INTEGRATION_NODES_JSON": json.dumps(nodes, separators=(",", ":")),
    }
    completed = _completed(
        [sys.executable, "scripts/run_isolated_integration.py", "integration"],
        environment=isolated_environment,
        timeout=3600,
    )
    receipt: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == "supportguard-isolated-integration.v1"
        ):
            receipt = value
            break
    passed = (
        completed.returncode == 0
        and receipt is not None
        and receipt.get("executed_node_ids") == nodes
        and receipt.get("executed_partitions") == len(nodes)
        and receipt.get("database_disposition") == "dropped"
    )
    return {
        "mode": "isolated_postgres",
        "nodes": nodes,
        "executed_partitions": None if receipt is None else receipt.get("executed_partitions"),
        "database_disposition": None if receipt is None else receipt.get("database_disposition"),
        "exit_code": completed.returncode,
        "passed": passed,
        "diagnostic_sha256": canonical_sha256(
            {"stdout": completed.stdout, "stderr": completed.stderr}
        ),
    }


def _collect_postgres_nodes(selectors: list[str], environment: Mapping[str, str]) -> list[str]:
    class Collector:
        nodes: list[str]

        def __init__(self) -> None:
            self.nodes = []

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            self.nodes = [item.nodeid for item in session.items]

    collector = Collector()
    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(environment)
    try:
        result = pytest.main(["--collect-only", "-q", *selectors], plugins=[collector])
    finally:
        os.environ.clear()
        os.environ.update(previous)
    nodes = collector.nodes
    if result != pytest.ExitCode.OK or not nodes or len(nodes) != len(set(nodes)):
        raise Phase7ContractError("ie_f06_postgres_node_collection_failed")
    if any(not any(node.startswith(selector) for selector in selectors) for node in nodes):
        raise Phase7ContractError("ie_f06_postgres_node_collection_drift")
    return nodes


def execute(root: Path, *, candidate_sha: str, output: Path) -> dict[str, Any]:
    root = root.resolve()
    identity_before = require_candidate(root, candidate_sha)
    output = require_ignored_output(root, output)
    contract = _load_contract(root)
    environment = _environment()
    all_nodes = [
        str(node) for case in contract["cases"] for node in case["deterministic_test_nodes"]
    ]
    postgres_selectors = [node for node in all_nodes if node.partition("::")[0] in _POSTGRES_FILES]
    postgres_nodes = _collect_postgres_nodes(postgres_selectors, environment)
    direct_nodes = [
        node for node in all_nodes if node != _MCP_NODE and node not in postgres_selectors
    ]
    direct_results = {node: _run_direct_node(node, environment) for node in direct_nodes}
    mcp_result = _run_mcp(environment)
    postgres_result = _run_postgres(postgres_nodes, environment)
    node_passed = {node: bool(result["passed"]) for node, result in direct_results.items()}
    node_passed[_MCP_NODE] = bool(mcp_result["passed"])
    node_passed.update(
        {
            selector: bool(postgres_result["passed"])
            and any(node.startswith(selector) for node in postgres_nodes)
            for selector in postgres_selectors
        }
    )
    case_results = [
        {
            "id": case["id"],
            "title": case["title"],
            "classification": "fault-injected",
            "test_nodes": list(case["deterministic_test_nodes"]),
            "passed": all(
                node_passed.get(str(node), False) for node in case["deterministic_test_nodes"]
            ),
        }
        for case in contract["cases"]
    ]
    identity_after = require_candidate(root, candidate_sha)
    if identity_after != identity_before:
        raise Phase7ContractError("candidate_source_changed_during_ie_f06")
    passed = sum(bool(case["passed"]) for case in case_results)
    receipt = {
        "schema": "supportguard.interview_v2.ie_f06_receipt.v1",
        "classification": "deterministic_fault_injected_not_provider_quality",
        "recorded_at": utc_now(),
        "candidate": identity_before.as_dict(),
        "contract_sha256": CONTRACTS["ie_f06"][1],
        "denominator": 6,
        "passed": passed,
        "failed": 6 - passed,
        "cases": case_results,
        "executions": {
            "direct": list(direct_results.values()),
            "mcp": mcp_result,
            "postgres": postgres_result,
        },
        "claims": {
            "passed": passed == 6,
            "fault_injected": True,
            "provider_quality_measured": False,
            "real_provider_called": False,
            "evaluation_v6_holdout_accessed": False,
            "cross_encoder_executed": False,
        },
        "cleanup": {
            "temporary_junit_directories_removed": True,
            "isolated_postgres_database_disposition": postgres_result.get("database_disposition"),
            "mcp_orphan_count": mcp_result.get("facts", {}).get("orphans"),
        },
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    atomic_write_json(output, receipt)
    if passed != 6:
        raise Phase7ContractError("ie_f06_matrix_failed")
    return receipt
