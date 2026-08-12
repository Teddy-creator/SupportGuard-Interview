#!/usr/bin/env python3
"""Run current mutable PostgreSQL suites in disposable Interview databases."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import xml.etree.ElementTree as ET  # local pytest-generated JUnit  # nosec B405
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.config import Settings
from supportguard.db.permissions import restore_interview_clone_database_access

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DISPOSITION_MANIFEST = ROOT / "backend/tests/integration_schema_disposition.v1.json"


@dataclass(frozen=True, slots=True)
class ModeContract:
    migration_command: str
    schema_identity: str
    evidence_class: str
    current_product_evidence: bool


INTEGRATION_CONTRACT = ModeContract(
    migration_command="baseline-upgrade",
    schema_identity="interview_baseline",
    evidence_class="current_product",
    current_product_evidence=True,
)


@dataclass(frozen=True, slots=True)
class SourceState:
    head_sha: str
    dirty: bool
    sha256: str


@dataclass(frozen=True, slots=True)
class IntegrationInventory:
    manifest_sha256: str


class _Collector:
    def __init__(self, target: Path) -> None:
        self.target = target

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.target.write_text(json.dumps([item.nodeid for item in session.items]))


def _source_state() -> SourceState:
    """Hash HEAD and Git-visible worktree state for a long-running invocation."""

    git = shutil.which("git")
    if git is None:
        raise RuntimeError("isolated_integration_git_missing")
    head_sha = subprocess.run(  # noqa: S603  # nosec B603
        [git, "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = subprocess.run(  # noqa: S603  # nosec B603
        [git, "diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(  # noqa: S603  # nosec B603
        [git, "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    untracked_paths = sorted(path for path in untracked if path)
    digest = hashlib.sha256()
    digest.update(b"head\0")
    digest.update(head_sha.encode())
    digest.update(b"\0diff\0")
    digest.update(diff)
    for raw_path in untracked_paths:
        path = ROOT / os.fsdecode(raw_path)
        if not path.is_file():
            continue
        digest.update(b"\0path\0")
        digest.update(raw_path)
        digest.update(b"\0content\0")
        digest.update(path.read_bytes())
    return SourceState(
        head_sha=head_sha,
        dirty=bool(diff or untracked_paths),
        sha256=digest.hexdigest(),
    )


def _assert_source_stable(expected: SourceState) -> None:
    actual = _source_state()
    if actual != expected:
        raise RuntimeError(
            "isolated_integration_source_drift:"
            f"expected={expected.sha256}:actual={actual.sha256}:"
            f"expected_head={expected.head_sha}:actual_head={actual.head_sha}"
        )


def _cluster_endpoint(base: str) -> str:
    url = make_url(base)
    query_host = url.query.get("host")
    if isinstance(query_host, tuple):
        query_host = query_host[0] if query_host else None
    host = url.host or (str(query_host) if query_host is not None else "local-default")
    if not host.startswith("/"):
        host = host.casefold()
    query_port = url.query.get("port")
    if isinstance(query_port, tuple):
        query_port = query_port[0] if query_port else None
    port = url.port or (int(query_port) if query_port is not None else 5432)
    return json.dumps({"host": host, "port": port}, sort_keys=True, separators=(",", ":"))


@contextmanager
def _invocation_lock(base: str) -> Iterator[None]:
    """Serialize runners because PostgreSQL service roles are cluster-global."""

    lock_key = hashlib.sha256(_cluster_endpoint(base).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"supportguard-integration-{lock_key}.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("isolated_integration_runner_already_active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _role_url(base: str, role: str, database: str) -> str:
    return (
        make_url(base)
        .set(username=role, password=role, database=database)
        .render_as_string(hide_password=False)
    )


async def _database_ddl(
    base: str, statement: str, parameters: dict[str, str] | None = None
) -> None:
    engine = create_async_engine(make_url(base).set(database="postgres"))
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement), parameters or {})
    finally:
        await engine.dispose()


async def _drop_owned_database(base: str, database: str) -> None:
    """Attempt both termination and drop, preserving every cleanup failure."""

    failures: list[Exception] = []
    try:
        await _database_ddl(
            base,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=:database AND pid<>pg_backend_pid()",
            {"database": database},
        )
    except Exception as exc:  # cleanup must still attempt DROP
        failures.append(exc)
    try:
        await _database_ddl(base, f'DROP DATABASE IF EXISTS "{database}"')  # noqa: S608
    except Exception as exc:
        failures.append(exc)
    if failures:
        raise ExceptionGroup(f"owned_database_cleanup_failed:{database}", failures)


def _run(argv: list[str], environment: dict[str, str], timeout: int = 1800) -> None:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        argv, cwd=ROOT, env=environment, check=False, timeout=timeout
    )
    if completed.returncode:
        raise RuntimeError(f"isolated_partition_failed:{Path(argv[0]).name}:{argv[-1]}")


def _run_with_source_guard(
    argv: list[str],
    environment: dict[str, str],
    expected_source: SourceState,
    *,
    timeout: int = 1800,
) -> None:
    """Check source immediately before and after both successful and failed children."""

    _assert_source_stable(expected_source)
    command_failure: Exception | None = None
    try:
        _run(argv, environment, timeout=timeout)
    except Exception as exc:
        command_failure = exc
    drift_failure: Exception | None = None
    try:
        _assert_source_stable(expected_source)
    except Exception as exc:
        drift_failure = exc
    if command_failure is not None and drift_failure is not None:
        raise ExceptionGroup(
            "isolated_partition_failed_with_source_drift",
            [command_failure, drift_failure],
        )
    if command_failure is not None:
        raise command_failure
    if drift_failure is not None:
        raise drift_failure


def _raise_after_cleanup(
    label: str,
    primary: BaseException | None,
    cleanup: BaseException | None,
) -> None:
    if primary is not None and cleanup is not None:
        raise BaseExceptionGroup(label, [primary, cleanup])
    if primary is not None:
        raise primary
    if cleanup is not None:
        raise cleanup


def _assert_executed_junit(path: Path, node: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"isolated_partition_junit_missing:{node}")
    try:
        root = ET.parse(path).getroot()  # noqa: S314  # nosec B314
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        counts = tuple(
            sum(int(suite.get(name, "0")) for suite in suites)
            for name in ("tests", "failures", "errors", "skipped")
        )
    except (OSError, ValueError, ET.ParseError) as exc:
        raise RuntimeError(f"isolated_partition_junit_malformed:{node}") from exc
    if counts != (1, 0, 0, 0):
        raise RuntimeError(
            "isolated_partition_not_executed:"
            f"{node}:tests={counts[0]}:failures={counts[1]}:"
            f"errors={counts[2]}:skipped={counts[3]}"
        )


def _database_zero(value: str) -> str:
    before, separator, _database = value.rpartition("/")
    return f"{before}{separator}0" if separator else value


def _clone_environment(environment: dict[str, str], base: str, database: str) -> dict[str, str]:
    cloned = {
        **environment,
        "DATABASE_URL": _role_url(base, "supportguard", database),
        "TEST_DATABASE_URL": _role_url(base, "supportguard", database),
        "TEST_FINALIZER_DATABASE_URL": _role_url(base, "supportguard", database),
        "TEST_WORKER_DATABASE_URL": _role_url(base, "supportguard_worker", database),
        "MCP_READ_DATABASE_URL": _role_url(base, "supportguard_read_mcp", database),
        "MCP_ACTION_DATABASE_URL": _role_url(base, "supportguard_action_mcp", database),
    }
    for key in (
        "TEST_REDIS_URL",
        "TEST_WORKER_REDIS_URL",
        "TEST_RECONCILER_REDIS_URL",
        "TEST_API_REDIS_URL",
    ):
        if value := cloned.get(key):
            cloned[key] = _database_zero(value)
    return cloned


def _contract_payload(contract: ModeContract) -> dict[str, object]:
    return {
        "migration_command": contract.migration_command,
        "schema_identity": contract.schema_identity,
        "evidence_class": contract.evidence_class,
        "current_product_evidence": contract.current_product_evidence,
    }


def _load_integration_inventory(nodes: list[str]) -> IntegrationInventory:
    try:
        raw = INTEGRATION_DISPOSITION_MANIFEST.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("integration_disposition_manifest_unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "supportguard-current-integration-inventory.v2"
    ):
        raise RuntimeError("integration_disposition_manifest_schema_invalid")
    if payload.get("current_contract") != _contract_payload(INTEGRATION_CONTRACT):
        raise RuntimeError("integration_disposition_current_contract_mismatch")
    current_files = payload.get("current_files")
    if not (
        isinstance(current_files, list)
        and current_files
        and all(isinstance(item, str) and item for item in current_files)
    ):
        raise RuntimeError("integration_disposition_entries_invalid")
    current_file_set = set(current_files)
    if len(current_file_set) != len(current_files):
        raise RuntimeError("integration_disposition_entries_not_unique")

    node_set = set(nodes)
    collected_files = {node.partition("::")[0] for node in node_set}
    if collected_files != current_file_set:
        missing = sorted(collected_files - current_file_set)
        stale = sorted(current_file_set - collected_files)
        raise RuntimeError(
            f"integration_disposition_file_inventory_mismatch:missing={missing}:stale={stale}"
        )
    return IntegrationInventory(manifest_sha256=hashlib.sha256(raw).hexdigest())


def _collect_integration_nodes() -> tuple[list[str], IntegrationInventory]:
    with tempfile.TemporaryDirectory(prefix="supportguard-integration-collect-") as temporary:
        target = Path(temporary) / "nodes.json"
        result = pytest.main(
            [
                "--collect-only",
                "-q",
                "-m",
                "(postgres or redis) and not mcp",
                "--ignore-glob=backend/tests/test_eval*.py",
            ],
            plugins=[_Collector(target)],
        )
        if result != pytest.ExitCode.OK or not target.is_file():
            raise RuntimeError("isolated_integration_collection_failed")
        nodes = json.loads(target.read_text())
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("isolated_integration_collection_empty")
    selected = [str(node) for node in nodes if "/test_eval_" not in str(node)]
    inventory = _load_integration_inventory(selected)
    if start_after := os.environ.get("SUPPORTGUARD_INTEGRATION_START_AFTER"):
        try:
            offset = selected.index(start_after) + 1
        except ValueError as exc:
            raise RuntimeError("isolated_integration_start_after_missing") from exc
        selected = selected[offset:]
    if pattern := os.environ.get("SUPPORTGUARD_INTEGRATION_NODE"):
        selected = [node for node in selected if pattern in node]
    if not selected:
        raise RuntimeError("isolated_integration_selection_empty")
    return selected, inventory


def _run_integration(base: str) -> None:
    contract = INTEGRATION_CONTRACT
    source_state = _source_state()
    template = f"supportguard_integration_template_{uuid4().hex[:10]}"
    admin_url = _role_url(base, "supportguard", template)
    environment = _clone_environment(
        {
            **os.environ,
            "APP_ENV": "test",
            "TRANSFORMERS_OFFLINE": "1",
        },
        base,
        template,
    )
    executable = str(Path(sys.executable).with_name("supportguard"))
    template_created = False
    primary_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    receipt: dict[str, object] | None = None
    try:
        asyncio.run(_database_ddl(base, f'CREATE DATABASE "{template}"'))  # noqa: S608
        template_created = True
        role_bootstrap_command = "bootstrap-interview-roles"
        setup = (
            ([executable, "db", role_bootstrap_command], admin_url),
            (
                [executable, "db", contract.migration_command],
                _role_url(base, "supportguard_migrator", template),
            ),
            ([executable, "db", "configure-mcp-roles"], admin_url),
            (
                [executable, "db", "seed"],
                _role_url(base, "supportguard_bootstrap", template),
            ),
            (
                [executable, "knowledge", "ingest", "--fixture"],
                _role_url(base, "supportguard_bootstrap", template),
            ),
        )
        for argv, database_url in setup:
            step_environment = {**environment, "DATABASE_URL": database_url}
            _run_with_source_guard(
                argv,
                step_environment,
                source_state,
                timeout=600,
            )
        nodes, inventory = _collect_integration_nodes()
        for ordinal, node in enumerate(nodes, start=1):
            _assert_source_stable(source_state)
            database = f"supportguard_integration_{ordinal}_{uuid4().hex[:8]}"
            junit = (
                Path(tempfile.gettempdir())
                / f"supportguard-integration-{os.getpid()}-{ordinal}-{uuid4().hex}.xml"
            )
            database_created = False
            node_failure: BaseException | None = None
            node_cleanup_failure: BaseException | None = None
            try:
                asyncio.run(  # each node gets an immutable schema/data baseline
                    _database_ddl(  # noqa: S608
                        base,
                        f'CREATE DATABASE "{database}" TEMPLATE "{template}"',
                    )
                )
                database_created = True
                clone_environment = _clone_environment(environment, base, database)
                _assert_source_stable(source_state)
                asyncio.run(
                    restore_interview_clone_database_access(
                        Settings(
                            _env_file=None,
                            app_env="test",
                            database_url=_role_url(base, "supportguard", database),
                        ),
                        source_database=template,
                    )
                )
                _assert_source_stable(source_state)
                _run_with_source_guard(
                    [executable, "db", role_bootstrap_command],
                    {
                        **clone_environment,
                        "DATABASE_URL": _role_url(base, "supportguard", database),
                    },
                    source_state,
                    timeout=600,
                )
                _assert_source_stable(source_state)
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    node,
                    f"--junitxml={junit}",
                ]
                _run_with_source_guard(command, clone_environment, source_state)
                _assert_executed_junit(junit, node)
            except BaseException as exc:
                node_failure = exc
            finally:
                junit.unlink(missing_ok=True)
                if database_created:
                    try:
                        asyncio.run(_drop_owned_database(base, database))
                    except BaseException as exc:
                        node_cleanup_failure = exc
            _raise_after_cleanup(
                f"isolated_partition_and_cleanup_failed:{node}",
                node_failure,
                node_cleanup_failure,
            )
        _assert_source_stable(source_state)
        receipt = {
            "schema": "supportguard-isolated-integration.v1",
            "mode": "integration",
            "schema_identity": contract.schema_identity,
            "migration_command": f"supportguard db {contract.migration_command}",
            "evidence_class": contract.evidence_class,
            "current_product_evidence": contract.current_product_evidence,
            "executed_partitions": len(nodes),
            "database_disposition": "dropped",
            "head_sha": source_state.head_sha,
            "source_dirty": source_state.dirty,
            "source_state_sha256": source_state.sha256,
        }
        receipt["integration_inventory_sha256"] = inventory.manifest_sha256
    except BaseException as exc:
        primary_failure = exc
    finally:
        if template_created:
            try:
                asyncio.run(_drop_owned_database(base, template))
            except BaseException as exc:
                cleanup_failure = exc
    _raise_after_cleanup(
        "isolated_invocation_and_cleanup_failed:integration",
        primary_failure,
        cleanup_failure,
    )
    if receipt is None:
        raise RuntimeError("isolated_integration_receipt_missing")
    print(json.dumps(receipt, sort_keys=True))


def main() -> None:
    if sys.argv[1:] != ["integration"]:
        raise SystemExit("usage: run_isolated_integration.py integration")
    base = os.environ.get("TEST_DATABASE_URL")
    if not base or not make_url(base).drivername.startswith("postgresql"):
        raise RuntimeError("TEST_DATABASE_URL_postgresql_required")
    with _invocation_lock(base):
        _run_integration(base)


if __name__ == "__main__":
    main()
