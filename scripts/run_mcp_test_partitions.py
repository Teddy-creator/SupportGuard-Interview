#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess  # nosec B404
import sys
import tempfile
import time
import xml.etree.ElementTree as ET  # local pytest-generated JUnit  # nosec B405
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.evidence.mcp_test_registry import (
    PARTITION_ENV,
    PARTITION_LEADER_ENV,
    REGISTRY_ENV,
    birth_from_record,
    create_registry,
    load_records,
    validate_partition_confirmations,
    validate_process_owner_manifest,
)
from supportguard.evidence.process_contract import (
    INNER_KILL_GRACE_SECONDS,
    INNER_TERM_GRACE_SECONDS,
    ProcessBirthIdentity,
    identity_matches,
    process_birth_identity,
)

ROOT = Path(__file__).resolve().parents[1]
PROCESS_OWNER_MANIFEST = ROOT / "backend/tests/mcp_process_owner_manifest.v1.json"

HERMETIC_NODES = (
    "backend/tests/test_read_mcp.py::test_read_server_handshake_discovery_and_scoped_call",
    "backend/tests/test_read_mcp.py::test_action_server_isolated_discovery_and_proposal_only",
    "backend/tests/test_read_mcp.py::test_backend_manages_long_lived_mcp_sessions_and_clean_shutdown",
    "backend/tests/test_read_mcp.py::test_retryable_stdio_process_loss_reaps_old_pid_and_rehandshakes",
    "backend/tests/test_read_mcp.py::test_graph_owned_stdio_rehandshake_occurs_between_two_physical_sends",
    "backend/tests/test_read_mcp.py::test_real_pending_call_is_cancelled_before_exact_child_pids_exit",
)
POSTGRES_NODES = (
    "backend/tests/test_phase4_escalation_retirement_postgres.py::"
    "test_three_current_action_proposals_complete_real_stdio_and_postgres",
    "backend/tests/test_phase4_escalation_retirement_postgres.py::"
    "test_refund_proposal_uses_active_fence_when_ticket_projection_has_converged",
    "backend/tests/test_v1212_production_agent_vertical.py::test_public_http_to_restricted_mcp_agent_finalizer_vertical",
    "backend/tests/test_v1213_identity_bound_e2e.py::test_three_actions_bind_exact_http_runtime_and_resource_effects",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_current_restricted_postgres_roles_call_all_twelve_stdio_mcp_tools",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_action_capabilities_serialize_concurrent_double_execution",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_agent_search_trace_binds_exact_tool_invocation_and_full_decisions",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_inactive_subscription_keeps_scoped_policy_search_readable",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_restricted_missing_billing_record_is_a_domain_denial",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_v158_read_mcp_compare_without_anchor_publishes_two_traced_groups",
    "backend/tests/test_v124_postgres_mcp_vertical.py::test_contextual_historical_read_mcp_query_publishes_both_version_groups",
)


@dataclass(frozen=True)
class Partition:
    name: str
    selector: str
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class IsolatedPostgresPartition:
    database_name: str
    environment: dict[str, str]
    admin_url: str


PARTITIONS = {
    "hermetic": Partition("hermetic", "mcp and not postgres", HERMETIC_NODES),
    "postgres": Partition("postgres", "mcp and postgres", POSTGRES_NODES),
}


def validate_collected_manifest() -> str:
    expected = {
        "hermetic": sorted(HERMETIC_NODES),
        "postgres": sorted(POSTGRES_NODES),
    }
    with tempfile.TemporaryDirectory(prefix="supportguard-mcp-manifest-") as temp:
        manifest_path = Path(temp) / "manifest.json"
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [sys.executable, "scripts/collect_mcp_test_manifest.py", str(manifest_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode or not manifest_path.is_file():
            raise RuntimeError("mcp_partition_manifest_collection_failed")
        raw_observed = json.loads(manifest_path.read_text())
        observed = {name: sorted(nodes) for name, nodes in raw_observed.items()}
    if observed != expected:
        raise RuntimeError("mcp_partition_manifest_drift")
    canonical = json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _safe_identity(value: str) -> tuple[str, str, int | None, str]:
    parsed = make_url(value)
    return (parsed.drivername, parsed.host or "", parsed.port, parsed.database or "")


def validate_postgres_environment(environment: dict[str, str]) -> dict[str, str]:
    required = {
        "TEST_DATABASE_URL": "supportguard",
        "MCP_READ_DATABASE_URL": "supportguard_read_mcp",
        "MCP_ACTION_DATABASE_URL": "supportguard_action_mcp",
    }
    identities: dict[str, tuple[str, str, int | None, str]] = {}
    roles: dict[str, str] = {}
    for key, expected_role in required.items():
        raw = environment.get(key)
        if not raw:
            raise RuntimeError(f"mcp_postgres_environment_missing:{key}")
        parsed = make_url(raw)
        if not parsed.drivername.startswith("postgresql"):
            raise RuntimeError(f"mcp_postgres_environment_not_postgresql:{key}")
        if parsed.username != expected_role:
            raise RuntimeError(f"mcp_postgres_environment_wrong_role:{key}")
        identities[key] = _safe_identity(raw)
        roles[key] = expected_role
    target = identities["TEST_DATABASE_URL"][1:]
    if any(identity[1:] != target for identity in identities.values()):
        raise RuntimeError("mcp_postgres_environment_target_mismatch")
    return roles


def _role_database_url(base: str, username: str, database: str) -> str:
    return (
        make_url(base)
        .set(username=username, password=username, database=database)
        .render_as_string(hide_password=False)
    )


async def _database_ddl(admin_url: str, statement: str, parameters: dict[str, str]) -> None:
    engine = create_async_engine(make_url(admin_url).set(database="postgres"))
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


def _run_setup(command: list[str], *, environment: dict[str, str]) -> None:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        cwd=ROOT,
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"mcp_postgres_partition_setup_failed:{command[-1]}")


def cleanup_postgres_partition_database(partition: IsolatedPostgresPartition) -> None:
    asyncio.run(
        _database_ddl(
            partition.admin_url,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=:database_name AND pid<>pg_backend_pid()",
            {"database_name": partition.database_name},
        )
    )
    asyncio.run(
        _database_ddl(
            partition.admin_url,
            f'DROP DATABASE IF EXISTS "{partition.database_name}"',
            {},
        )
    )


def prepare_postgres_partition_environment(
    parent_environment: dict[str, str],
) -> IsolatedPostgresPartition:
    validate_postgres_environment(parent_environment)
    base = parent_environment["TEST_DATABASE_URL"]
    database_name = f"supportguard_mcp_{os.getpid()}_{uuid4().hex[:10]}"
    admin_url = _role_database_url(base, "supportguard", database_name)
    isolated = IsolatedPostgresPartition(database_name, {}, admin_url)
    asyncio.run(
        _database_ddl(
            admin_url,
            f'CREATE DATABASE "{database_name}"',
            {},
        )
    )
    environment = parent_environment.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": admin_url,
            "TEST_DATABASE_URL": admin_url,
            "TEST_FINALIZER_DATABASE_URL": admin_url,
            "TEST_WORKER_DATABASE_URL": _role_database_url(
                base, "supportguard_worker", database_name
            ),
            "MCP_READ_DATABASE_URL": _role_database_url(
                base, "supportguard_read_mcp", database_name
            ),
            "MCP_ACTION_DATABASE_URL": _role_database_url(
                base, "supportguard_action_mcp", database_name
            ),
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    isolated = IsolatedPostgresPartition(database_name, environment, admin_url)
    supportguard = str(Path(sys.executable).parent / "supportguard")
    setup_commands = (
        ([supportguard, "db", "bootstrap-interview-roles"], admin_url),
        (
            [supportguard, "db", "baseline-upgrade"],
            _role_database_url(base, "supportguard_migrator", database_name),
        ),
        (
            [supportguard, "db", "seed"],
            _role_database_url(base, "supportguard_bootstrap", database_name),
        ),
        ([supportguard, "db", "configure-mcp-roles"], admin_url),
        (
            [supportguard, "knowledge", "ingest", "--fixture"],
            _role_database_url(base, "supportguard_bootstrap", database_name),
        ),
    )
    try:
        for command, database_url in setup_commands:
            setup_environment = environment.copy()
            setup_environment["DATABASE_URL"] = database_url
            _run_setup(command, environment=setup_environment)
    except BaseException:
        cleanup_postgres_partition_database(isolated)
        raise
    return isolated


def _mcp_child_pids() -> set[int]:
    observed = subprocess.run(  # noqa: S603  # nosec B603
        ["/bin/ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pids: set[int] = set()
    for line in observed.splitlines():
        if (
            "supportguard.mcp.read_server" not in line
            and "supportguard.mcp.action_server" not in line
        ):
            continue
        fields = line.strip().split(maxsplit=1)
        if fields and fields[0].isdigit():
            pids.add(int(fields[0]))
    return pids


def _wait_new_mcp_children_absent(baseline: set[int], timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = _mcp_child_pids() - baseline
        if not observed:
            return set()
        time.sleep(0.02)
    return _mcp_child_pids() - baseline


def _junit_counts(path: Path) -> tuple[str, tuple[int, int, int, int] | None]:
    if not path.is_file():
        return "missing", None
    try:
        root = ET.parse(path).getroot()  # local pytest JUnit  # noqa: S314  # nosec B314
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        names = ("tests", "failures", "errors", "skipped")
        values = tuple(sum(int(suite.get(name, "0")) for suite in suites) for name in names)
        return "ok", values  # type: ignore[return-value]
    except (OSError, ValueError, ET.ParseError):
        return "malformed", None


def _wait_group_absent(pgid: int, timeout: float) -> bool:
    def live_members() -> list[int]:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            ["/bin/ps", "-axo", "pid=,pgid=,state="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        result: list[int] = []
        for line in completed.stdout.splitlines():
            fields = line.split()
            if (
                len(fields) == 3
                and fields[0].isdigit()
                and fields[1].isdigit()
                and int(fields[1]) == pgid
                and not fields[2].startswith("Z")
            ):
                result.append(int(fields[0]))
        return result

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not live_members():
            return True
        time.sleep(0.02)
    return not live_members()


def _terminate_group(pgid: int, *, birth: ProcessBirthIdentity) -> list[str]:
    escalations: list[str] = []
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("mcp_partition_signal_authorization_failed")
    for name, sig, grace in (
        ("SIGTERM", signal.SIGTERM, INNER_TERM_GRACE_SECONDS),
        ("SIGKILL", signal.SIGKILL, INNER_KILL_GRACE_SECONDS),
    ):
        if not identity_matches(birth):
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return escalations
            raise RuntimeError("mcp_partition_birth_identity_changed")
        try:
            os.killpg(pgid, sig)
            escalations.append(name)
        except ProcessLookupError:
            return escalations
        if _wait_group_absent(pgid, grace):
            return escalations
    return escalations


def _cleanup_registered_mcp(registry: Path) -> tuple[int, list[str]]:
    records = load_records(registry)
    registered = [item for item in records if item.get("state") == "registered"]
    errors: list[str] = []
    survivors = 0
    for record in registered:
        pid_raw = record.get("leader_pid")
        if not isinstance(pid_raw, int):
            raise RuntimeError("mcp_registry_record_malformed")
        pid = pid_raw
        try:
            _terminate_group(pid, birth=birth_from_record(record))
        except RuntimeError as exc:
            errors.append(str(exc))
        if _wait_group_absent(pid, 0):
            continue
        survivors += 1
    return survivors, errors


def run_partition(
    partition: Partition, *, parent_environment: dict[str, str] | None = None
) -> None:
    if os.name != "posix":
        raise RuntimeError("mcp_partition_posix_required")
    base_environment = (parent_environment or dict(os.environ)).copy()
    roles: dict[str, str] = {}
    if partition.name == "postgres":
        roles = validate_postgres_environment(base_environment)
    role_summary = ",".join(sorted(set(roles.values()))) or "fixture-owned"
    target_fingerprint = "fixture-owned"
    if partition.name == "postgres":
        safe_target = _safe_identity(base_environment["TEST_DATABASE_URL"])[1:]
        target_fingerprint = hashlib.sha256(repr(safe_target).encode()).hexdigest()
    print(
        f"[mcp-partition] name={partition.name} selector={partition.selector!r} "
        f"expected={len(partition.nodes)} environment={role_summary} "
        f"target_sha256={target_fingerprint}",
        flush=True,
    )
    before = _mcp_child_pids()
    registry_owner: tempfile.TemporaryDirectory[str] | None = None
    registry_raw = base_environment.get(REGISTRY_ENV)
    if registry_raw:
        registry = Path(registry_raw)
    else:
        registry_owner = tempfile.TemporaryDirectory(
            prefix=f"supportguard-mcp-registry-{partition.name}-"
        )
        registry = create_registry(Path(registry_owner.name) / "registry")
    with tempfile.TemporaryDirectory(prefix=f"supportguard-mcp-{partition.name}-") as temp:
        junit = Path(temp) / "junit.xml"
        environment = base_environment.copy()
        environment[REGISTRY_ENV] = str(registry)
        environment[PARTITION_ENV] = partition.name
        environment[PARTITION_LEADER_ENV] = "self"
        cancelled = False
        timeout = int(base_environment.get("SUPPORTGUARD_V129_MCP_PARTITION_TIMEOUT", "1800"))
        previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def cancel(_signum: int, _frame: object) -> None:
            nonlocal cancelled
            cancelled = True

        for sig in previous_handlers:
            signal.signal(sig, cancel)
        timed_out = False
        teardown_errors: list[str] = []
        escalation: list[str] = []
        nested_survivors = 0
        process: subprocess.Popen[bytes] | None = None
        birth: ProcessBirthIdentity | None = None
        try:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-m",
                    partition.selector,
                    *partition.nodes,
                    f"--junitxml={junit}",
                ],
                cwd=ROOT,
                env=environment,
                process_group=0,
            )
            birth = process_birth_identity(process.pid)
            if (
                process.pid <= 1
                or os.getpgid(process.pid) != process.pid
                or os.getsid(process.pid) != os.getsid(0)
            ):
                raise RuntimeError("mcp_partition_root_identity_invalid")
            deadline = time.monotonic() + timeout
            while process.poll() is None and not cancelled and time.monotonic() < deadline:
                time.sleep(0.05)
            timed_out = process.poll() is None and not cancelled
        except BaseException as exc:
            teardown_errors.append(f"partition_exception:{type(exc).__name__}:{exc}")
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            if process is not None and process.poll() is None and birth is not None:
                try:
                    escalation.extend(_terminate_group(process.pid, birth=birth))
                except RuntimeError as exc:
                    teardown_errors.append(str(exc))
            elif process is not None and process.poll() is None:
                teardown_errors.append("mcp_partition_birth_identity_unavailable")
            if process is not None:
                try:
                    process.wait(timeout=INNER_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    teardown_errors.append("mcp_partition_root_reap_timeout")
            nested_survivors, nested_errors = _cleanup_registered_mcp(registry)
            teardown_errors.extend(nested_errors)
        junit_status, counts = _junit_counts(junit)
        tests = failures = errors = skipped = None
        if counts is not None:
            tests, failures, errors, skipped = counts
        try:
            registry_records = load_records(registry)
            validate_partition_confirmations(registry_records, partition_id=partition.name)
            if PARTITIONS.get(partition.name) == partition:
                validate_process_owner_manifest(
                    registry_records,
                    manifest_path=PROCESS_OWNER_MANIFEST,
                    partition_id=partition.name,
                )
        except RuntimeError as exc:
            teardown_errors.append(str(exc))
    orphans = sorted(_wait_new_mcp_children_absent(before, INNER_KILL_GRACE_SECONDS))
    executed = None if tests is None or skipped is None else tests - skipped
    exit_code = None if process is None else process.returncode
    print(
        f"[mcp-partition-result] name={partition.name} collected={tests} "
        f"executed={executed} failures={failures} errors={errors} skipped={skipped} "
        f"exit={exit_code} orphan_count={len(orphans)} junit={junit_status} "
        f"timeout={timed_out} cancelled={cancelled} escalation={','.join(escalation)}",
        flush=True,
    )
    if teardown_errors:
        print(
            f"[mcp-partition-errors] name={partition.name} "
            f"errors={json.dumps(teardown_errors, separators=(',', ':'))}",
            flush=True,
        )
    if registry_owner is not None:
        registry_owner.cleanup()
    if (
        junit_status != "ok"
        or tests != len(partition.nodes)
        or skipped
        or failures
        or errors
        or exit_code
        or timed_out
        or cancelled
        or teardown_errors
        or nested_survivors
    ):
        raise RuntimeError(f"mcp_partition_failed:{partition.name}")
    if orphans:
        raise RuntimeError(f"mcp_partition_orphaned_children:{partition.name}")


def main() -> None:
    requested = sys.argv[1] if len(sys.argv) == 2 else "all"
    if requested not in {*PARTITIONS, "all"}:
        raise SystemExit("usage: run_mcp_test_partitions.py [hermetic|postgres|all]")
    manifest_hash = validate_collected_manifest()
    print(
        f"[mcp-manifest] hermetic={len(HERMETIC_NODES)} "
        f"postgres={len(POSTGRES_NODES)} sha256={manifest_hash}",
        flush=True,
    )
    names = ("hermetic", "postgres") if requested == "all" else (requested,)
    for name in names:
        if name != "postgres":
            run_partition(PARTITIONS[name])
            continue
        isolated = prepare_postgres_partition_environment(dict(os.environ))
        try:
            run_partition(PARTITIONS[name], parent_environment=isolated.environment)
        finally:
            cleanup_postgres_partition_database(isolated)


if __name__ == "__main__":
    main()
