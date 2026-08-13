from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import xml.etree.ElementTree as ET  # local pytest-generated JUnit  # nosec B405
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast

from .phase7_common import (
    Phase7ContractError,
    atomic_write_json,
    canonical_sha256,
    require_candidate,
    require_ignored_output,
    utc_now,
)

ProofKind = Literal[
    "backend_full",
    "integration_current",
    "mcp_current",
    "frontend_unit",
    "browser_current_19",
    "clean_compose",
]
PROOF_KINDS: Final = (
    "backend_full",
    "integration_current",
    "mcp_current",
    "frontend_unit",
    "browser_current_19",
    "clean_compose",
)
_MCP_RESULT = re.compile(
    r"\[mcp-partition-result\] name=(?P<name>hermetic|postgres) "
    r"collected=(?P<collected>\d+) executed=(?P<executed>\d+) "
    r"failures=(?P<failures>\d+) errors=(?P<errors>\d+) "
    r"skipped=(?P<skipped>\d+).*orphan_count=(?P<orphans>\d+)"
)


def _executable(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise Phase7ContractError(f"phase7_proof_executable_unavailable:{name}")
    return value


def _environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "CI",
        "APP_ENV",
        "AUTH_MODE",
        "DEMO_FAKE_PROVIDER",
        "EMBEDDING_MODE",
        "TRANSFORMERS_OFFLINE",
        "TEST_DATABASE_URL",
        "TEST_FINALIZER_DATABASE_URL",
        "TEST_WORKER_DATABASE_URL",
        "MCP_READ_DATABASE_URL",
        "MCP_ACTION_DATABASE_URL",
        "TEST_REDIS_URL",
        "TEST_WORKER_REDIS_URL",
        "TEST_RECONCILER_REDIS_URL",
        "TEST_API_REDIS_URL",
        "PLAYWRIGHT_BASE_URL",
        "PLAYWRIGHT_EXECUTABLE_PATH",
        "PLAYWRIGHT_OUTPUT_DIR",
        "COMPOSE_PROJECT_NAME",
        "CODE_VERSION",
        "BACKEND_IMAGE",
        "FRONTEND_IMAGE",
        "API_HOST_PORT",
        "POSTGRES_HOST_PORT",
        "REDIS_HOST_PORT",
        "FRONTEND_HOST_PORT",
        "V125_API_PORT",
        "API_HOST",
        "E2E_FIXTURE_RUNNER",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run(
    root: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        list(arguments),
        cwd=root,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _safe_command_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "exit_code": completed.returncode,
        "diagnostic_sha256": canonical_sha256(
            {"stdout": completed.stdout, "stderr": completed.stderr}
        ),
    }


def _junit_counts(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()  # noqa: S314  # nosec B314
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        return {
            name: sum(int(suite.get(name, "0")) for suite in suites)
            for name in ("tests", "failures", "errors", "skipped")
        }
    except (OSError, ValueError, ET.ParseError) as exc:
        raise Phase7ContractError("phase7_proof_junit_malformed") from exc


def _backend(root: Path, environment: Mapping[str, str], directory: Path) -> dict[str, Any]:
    junit = directory / "backend.xml"
    completed = _run(
        root,
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "not mcp",
            "--ignore-glob=backend/tests/test_eval*.py",
            f"--junitxml={junit}",
        ],
        environment,
        timeout=3600,
    )
    counts = (
        _junit_counts(junit)
        if junit.is_file()
        else {
            "tests": 0,
            "failures": 0,
            "errors": 1,
            "skipped": 0,
        }
    )
    return {
        **_safe_command_result(completed),
        "counts": counts,
        "denominator": counts["tests"],
        "passed": completed.returncode == 0
        and counts["tests"] > 0
        and counts["failures"] == counts["errors"] == 0,
    }


def _integration(root: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    completed = _run(
        root,
        [sys.executable, "scripts/run_isolated_integration.py", "integration"],
        environment,
        timeout=7200,
    )
    raw_receipt: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == "supportguard-isolated-integration.v1"
        ):
            raw_receipt = value
            break
    return {
        **_safe_command_result(completed),
        "denominator": None if raw_receipt is None else raw_receipt.get("executed_partitions"),
        "database_disposition": None
        if raw_receipt is None
        else raw_receipt.get("database_disposition"),
        "inventory_sha256": None
        if raw_receipt is None
        else raw_receipt.get("integration_inventory_sha256"),
        "executed_node_ids_sha256": None
        if raw_receipt is None
        else raw_receipt.get("executed_node_ids_sha256"),
        "passed": completed.returncode == 0
        and raw_receipt is not None
        and int(raw_receipt.get("executed_partitions", 0)) > 0
        and raw_receipt.get("database_disposition") == "dropped",
    }


def _mcp(root: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    completed = _run(
        root,
        [sys.executable, "scripts/run_mcp_test_partitions.py", "all"],
        environment,
        timeout=3600,
    )
    partitions = {
        match.group("name"): {
            key: int(value) for key, value in match.groupdict().items() if key != "name"
        }
        for match in _MCP_RESULT.finditer(completed.stdout)
    }
    passed = completed.returncode == 0 and set(partitions) == {"hermetic", "postgres"}
    passed = passed and all(
        facts["collected"] == facts["executed"]
        and facts["failures"] == facts["errors"] == facts["skipped"] == facts["orphans"] == 0
        for facts in partitions.values()
    )
    return {
        **_safe_command_result(completed),
        "partitions": partitions,
        "denominator": sum(facts["executed"] for facts in partitions.values()),
        "passed": passed,
    }


def _frontend(root: Path, environment: Mapping[str, str], directory: Path) -> dict[str, Any]:
    report = directory / "vitest.json"
    commands = [
        [_executable("pnpm"), "--dir", "frontend", "lint"],
        [
            _executable("pnpm"),
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "--reporter=json",
            f"--outputFile={report}",
        ],
        [_executable("pnpm"), "--dir", "frontend", "build"],
    ]
    completed = [_run(root, command, environment, timeout=1200) for command in commands]
    value = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    counts = {
        "tests": int(value.get("numTotalTests", 0)),
        "passed": int(value.get("numPassedTests", 0)),
        "failed": int(value.get("numFailedTests", 0)),
        "pending": int(value.get("numPendingTests", 0)),
    }
    passed = (
        all(item.returncode == 0 for item in completed)
        and value.get("success") is True
        and counts["tests"] > 0
        and counts["failed"] == 0
    )
    return {
        "commands": [_safe_command_result(item) for item in completed],
        "counts": counts,
        "denominator": counts["tests"],
        "passed": passed,
    }


def _browser(root: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    project = environment.get("COMPOSE_PROJECT_NAME", "").strip()
    if re.fullmatch(r"supportguard[a-z0-9_-]{2,52}", project) is None or project in {
        "supportguard",
        "supportguard_default",
    }:
        raise Phase7ContractError("phase7_browser_requires_explicit_compose_project")
    if not environment.get("PLAYWRIGHT_BASE_URL", "").strip():
        raise Phase7ContractError("phase7_browser_requires_explicit_base_url")

    docker = _executable("docker")
    preflight_commands = (
        (
            docker,
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "bootstrap-demo",
            "supportguard",
            "demo",
            "temporal-refresh",
            "--tenant",
            "tenant_demo",
        ),
        (
            docker,
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "bootstrap-demo",
            "supportguard",
            "demo",
            "temporal-preflight",
            "--tenant",
            "tenant_demo",
        ),
        (
            docker,
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "worker",
            "supportguard",
            "demo",
            "resource-preflight",
            "--tenant",
            "tenant_demo",
        ),
    )
    preflight_completed = [
        _run(root, command, environment, timeout=300) for command in preflight_commands
    ]
    preflight_payloads: list[dict[str, Any]] = []
    for completed_item in preflight_completed:
        try:
            payload = json.loads(completed_item.stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            payload = {}
        preflight_payloads.append(payload if isinstance(payload, dict) else {})
    temporal_refresh, temporal_preflight, resource_preflight = preflight_payloads
    preflight_passed = (
        all(item.returncode == 0 for item in preflight_completed)
        and temporal_refresh.get("schema") == "demo-temporal-report.v1"
        and temporal_refresh.get("mode") == "refresh"
        and temporal_refresh.get("tenant_id") == "tenant_demo"
        and temporal_refresh.get("latest_snapshot_age_seconds") == 0
        and temporal_preflight.get("schema") == "demo-temporal-report.v1"
        and temporal_preflight.get("mode") == "preflight"
        and temporal_preflight.get("tenant_id") == "tenant_demo"
        and isinstance(temporal_preflight.get("latest_snapshot_age_seconds"), int)
        and 0 <= int(temporal_preflight["latest_snapshot_age_seconds"]) <= 120
        and resource_preflight.get("schema") == "demo-resource-report.v1"
        and resource_preflight.get("tenant_id") == "tenant_demo"
        and resource_preflight.get("ready") is True
    )
    preflight = {
        "commands": [_safe_command_result(item) for item in preflight_completed],
        "latest_snapshot_age_seconds": temporal_preflight.get("latest_snapshot_age_seconds"),
        "resource_ready": resource_preflight.get("ready"),
        "passed": preflight_passed,
    }
    if not preflight_passed:
        return {
            "demo_preflight": preflight,
            "counts": {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0},
            "denominator": 0,
            "passed": False,
        }

    completed = _run(
        root,
        [
            _executable("pnpm"),
            "--dir",
            "frontend",
            "exec",
            "playwright",
            "test",
            "--reporter=json",
        ],
        environment,
        timeout=3600,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = {}
    stats = value.get("stats", {}) if isinstance(value, dict) else {}
    expected = int(stats.get("expected", 0))
    unexpected = int(stats.get("unexpected", 0))
    flaky = int(stats.get("flaky", 0))
    skipped = int(stats.get("skipped", 0))
    return {
        **_safe_command_result(completed),
        "demo_preflight": preflight,
        "counts": {
            "expected": expected,
            "unexpected": unexpected,
            "flaky": flaky,
            "skipped": skipped,
        },
        "denominator": expected + unexpected + flaky + skipped,
        "passed": completed.returncode == 0
        and expected == 19
        and unexpected == flaky == skipped == 0,
    }


def _compose(root: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    completed = _run(
        root,
        [_executable("uv"), "run", "--frozen", "python", "scripts/compose_verify.py"],
        environment,
        timeout=1200,
    )
    try:
        value = json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        value = {}
    services = value.get("services", {}) if isinstance(value, dict) else {}
    denominator = sum(int(count) for count in services.values()) if services else 0
    return {
        **_safe_command_result(completed),
        "denominator": denominator,
        "services": services,
        "mcp_children": value.get("mcp_children") if isinstance(value, dict) else None,
        "embedding_contract": value.get("embedding_contract") if isinstance(value, dict) else None,
        "passed": completed.returncode == 0
        and value.get("status") == "passed"
        and denominator == 8
        and value.get("mcp_children") == 4,
    }


def execute(root: Path, *, candidate_sha: str, output: Path, kind: ProofKind) -> dict[str, Any]:
    if kind not in PROOF_KINDS:
        raise Phase7ContractError("phase7_proof_kind_unknown")
    root = root.resolve()
    identity_before = require_candidate(root, candidate_sha)
    output = require_ignored_output(root, output)
    environment = _environment()
    with tempfile.TemporaryDirectory(prefix=f"supportguard-phase7-{kind}-") as directory:
        temporary = Path(directory)
        if kind == "backend_full":
            result = _backend(root, environment, temporary)
        elif kind == "integration_current":
            result = _integration(root, environment)
        elif kind == "mcp_current":
            result = _mcp(root, environment)
        elif kind == "frontend_unit":
            result = _frontend(root, environment, temporary)
        elif kind == "browser_current_19":
            result = _browser(root, environment)
        else:
            result = _compose(root, environment)
    identity_after = require_candidate(root, candidate_sha)
    if identity_after != identity_before:
        raise Phase7ContractError(f"candidate_source_changed_during_{kind}")
    receipt = {
        "schema": "supportguard.interview_v2.deterministic_proof.v1",
        "classification": kind,
        "recorded_at": utc_now(),
        "candidate": identity_before.as_dict(),
        "candidate_sha": candidate_sha,
        "denominator": result.get("denominator"),
        "passed": result.get("passed") is True,
        "result": result,
        "claims": {
            "provider_quality_measured": False,
            "real_provider_called": False,
            "evaluation_v6_holdout_accessed": False,
            "cross_encoder_executed": False,
        },
        "cleanup": {"temporary_artifacts_removed": True},
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    atomic_write_json(output, cast(dict[str, Any], receipt))
    if receipt["passed"] is not True:
        raise Phase7ContractError(f"phase7_deterministic_proof_failed:{kind}")
    return receipt
