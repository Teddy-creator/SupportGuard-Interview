import hashlib
import json
import runpy
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _junit_assertion() -> Callable[[Path, str], None]:
    namespace = runpy.run_path(str(ROOT / "scripts/run_isolated_integration.py"))
    return cast(Callable[[Path, str], None], namespace["_assert_executed_junit"])


def _runner_namespace() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts/run_isolated_integration.py"))


def test_ci_mutable_suites_use_disposable_database_carriers() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    runner = (ROOT / "scripts/run_isolated_integration.py").read_text()
    permissions = (ROOT / "backend/src/supportguard/db/permissions.py").read_text()

    assert "run_isolated_integration.py integration" in workflow
    assert "run_isolated_integration.py v129-regression" not in workflow
    assert 'pytest -m "(postgres or redis) and not mcp"' not in workflow
    assert "make corrective-v129-regression" not in workflow
    assert 'sys.argv[1:] != ["integration"]' in runner
    assert "DROP DATABASE IF EXISTS" in runner
    assert "restore_interview_clone_database_access(" in runner
    assert "SET database_uuid=gen_random_uuid(),database_name=current_database()" not in runner
    assert "SET database_uuid=gen_random_uuid(),database_name=current_database()" in permissions
    assert "database_disposition" in runner
    assert "_assert_executed_junit(junit, node)" in runner
    assert '"--ignore-glob=backend/tests/test_eval*.py"' in runner
    assert '"/test_eval_" not in str(node)' in runner
    assert "legacy_final" not in runner
    assert '"bootstrap-roles"' not in runner
    assert "isolated_integration_source_drift" in runner
    assert "isolated_integration_runner_already_active" in runner
    assert '"source_state_sha256"' in runner
    assert "DEEPSEEK_API_KEY" not in runner


def test_isolated_runner_has_one_current_schema_identity() -> None:
    namespace = _runner_namespace()
    current = cast(Any, namespace["INTEGRATION_CONTRACT"])

    assert current.migration_command == "baseline-upgrade"
    assert current.schema_identity == "interview_baseline"
    assert current.evidence_class == "current_product"
    assert current.current_product_evidence is True

    runner = (ROOT / "scripts/run_isolated_integration.py").read_text()
    assert '[executable, "db", contract.migration_command]' in runner
    assert '"bootstrap-interview-roles"' in runner
    for receipt_field in (
        '"schema_identity": contract.schema_identity',
        '"migration_command": f"supportguard db {contract.migration_command}"',
        '"evidence_class": contract.evidence_class',
        '"current_product_evidence": contract.current_product_evidence',
    ):
        assert receipt_field in runner
    assert '"integration_inventory_sha256"' in runner


def test_current_integration_inventory_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPPORTGUARD_INTEGRATION_START_AFTER", raising=False)
    monkeypatch.delenv("SUPPORTGUARD_INTEGRATION_NODE", raising=False)
    monkeypatch.delenv("SUPPORTGUARD_INTEGRATION_NODES_JSON", raising=False)
    namespace = _runner_namespace()
    collect = cast(Callable[[], tuple[list[str], Any]], namespace["_collect_integration_nodes"])

    current_nodes, inventory = collect()
    manifest_path = ROOT / "backend/tests/integration_schema_disposition.v1.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)

    assert inventory.manifest_sha256 == hashlib.sha256(manifest_raw).hexdigest()
    assert {node.partition("::")[0] for node in current_nodes} == set(manifest["current_files"])
    assert (
        "backend/tests/test_v1213_postgres_contract.py::"
        "test_v1213_forward_repairs_are_current_head_without_new_schema_objects" in current_nodes
    )
    assert (
        "backend/tests/test_phase4_escalation_retirement_postgres.py::"
        "test_escalation_direct_and_generic_paths_fail_closed_without_writes"
    ) in current_nodes
    assert all("test_v129_" not in node for node in current_nodes)


def test_current_integration_accepts_only_exact_known_phase7_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = (
        "backend/tests/test_v124_agent_recovery.py::"
        "test_turn_takeover_reuses_decision_and_terminalizes_every_ordinal"
    )
    monkeypatch.delenv("SUPPORTGUARD_INTEGRATION_START_AFTER", raising=False)
    monkeypatch.delenv("SUPPORTGUARD_INTEGRATION_NODE", raising=False)
    monkeypatch.setenv("SUPPORTGUARD_INTEGRATION_NODES_JSON", json.dumps([node]))
    namespace = _runner_namespace()
    collect = cast(Callable[[], tuple[list[str], Any]], namespace["_collect_integration_nodes"])

    selected, _inventory = collect()

    assert selected == [node]

    monkeypatch.setenv(
        "SUPPORTGUARD_INTEGRATION_NODES_JSON",
        json.dumps(["backend/tests/test_unknown.py::test_unknown"]),
    )
    with pytest.raises(RuntimeError, match="isolated_integration_exact_nodes_unknown"):
        collect()


def test_integration_inventory_fails_closed_for_an_unclassified_file() -> None:
    namespace = _runner_namespace()
    load = cast(Callable[[list[str]], Any], namespace["_load_integration_inventory"])

    with pytest.raises(
        RuntimeError,
        match="integration_disposition_file_inventory_mismatch",
    ):
        load(["backend/tests/test_unclassified_schema.py::test_unknown"])


def test_isolated_runner_serializes_cluster_global_role_setup() -> None:
    namespace = _runner_namespace()
    lock = cast(Callable[[str], AbstractContextManager[None]], namespace["_invocation_lock"])
    cluster_endpoint = cast(Callable[[str], str], namespace["_cluster_endpoint"])
    asyncpg_url = "postgresql+asyncpg://user@LOCALHOST:55432/db_one"
    psycopg_url = "postgresql+psycopg://other@localhost:55432/db_two"

    assert cluster_endpoint(asyncpg_url) == cluster_endpoint(psycopg_url)

    with (
        lock(asyncpg_url),
        pytest.raises(RuntimeError, match="isolated_integration_runner_already_active"),
        lock(psycopg_url),
    ):
        raise AssertionError("second runner unexpectedly acquired the lock")


def test_isolated_runner_rejects_source_state_drift() -> None:
    namespace = _runner_namespace()
    source_state = cast(Callable[[], Any], namespace["_source_state"])
    assert_stable = cast(Callable[[Any], None], namespace["_assert_source_stable"])
    actual = source_state()
    invalid = type(actual)(
        head_sha=actual.head_sha,
        dirty=actual.dirty,
        sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="isolated_integration_source_drift"):
        assert_stable(invalid)


def test_isolated_runner_rejects_a_skipped_partition(tmp_path: Path) -> None:
    report = tmp_path / "skipped.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="1"/>',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="isolated_partition_not_executed"):
        _junit_assertion()(report, "tests/test_example.py::test_skipped")


def test_isolated_runner_accepts_exactly_one_executed_partition(
    tmp_path: Path,
) -> None:
    report = tmp_path / "passed.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )

    _junit_assertion()(report, "tests/test_example.py::test_passed")
