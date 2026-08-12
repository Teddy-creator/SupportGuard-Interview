from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from supportguard.evidence.mcp_test_registry import (
    OWNER_NODE_ENV,
    PARTITION_ENV,
    PARTITION_LEADER_ENV,
    REGISTRY_ENV,
    scoped_process_environment,
    validate_owned_partition,
    validate_partition_confirmations,
    validate_process_owner_manifest,
)

ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts/run_mcp_test_partitions.py"
    spec = importlib.util.spec_from_file_location("supportguard_mcp_partitions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mcp_partition_manifest_is_exact_mutually_exclusive_and_complete() -> None:
    module = _module()
    hermetic = set(module.HERMETIC_NODES)
    postgres = set(module.POSTGRES_NODES)
    assert len(hermetic) == 6
    assert len(postgres) == 10
    assert (
        "backend/tests/test_phase4_escalation_retirement_postgres.py::"
        "test_three_current_action_proposals_complete_real_stdio_and_postgres"
    ) in postgres
    assert hermetic.isdisjoint(postgres)
    assert all((ROOT / node.split("::", 1)[0]).is_file() for node in hermetic | postgres)
    assert module.PARTITIONS["hermetic"].selector == "mcp and not postgres"
    assert module.PARTITIONS["postgres"].selector == "mcp and postgres"
    assert len(module.validate_collected_manifest()) == 64


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"MCP_READ_DATABASE_URL": None}, "missing:MCP_READ_DATABASE_URL"),
        ({"MCP_READ_DATABASE_URL": "sqlite:///fixture.db"}, "not_postgresql"),
        (
            {"MCP_READ_DATABASE_URL": "postgresql+asyncpg://supportguard:pw@db:5432/app"},
            "wrong_role",
        ),
        (
            {
                "MCP_READ_DATABASE_URL": (
                    "postgresql+asyncpg://supportguard_read_mcp:pw@other:5432/app"
                )
            },
            "target_mismatch",
        ),
    ],
)
def test_postgres_partition_fails_closed_for_invalid_capability_environment(
    mutation: dict[str, str | None], error: str
) -> None:
    module = _module()
    environment: dict[str, str] = {
        "TEST_DATABASE_URL": "postgresql+asyncpg://supportguard:pw@db:5432/app",
        "MCP_READ_DATABASE_URL": ("postgresql+asyncpg://supportguard_read_mcp:pw@db:5432/app"),
        "MCP_ACTION_DATABASE_URL": ("postgresql+asyncpg://supportguard_action_mcp:pw@db:5432/app"),
    }
    for key, value in mutation.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    with pytest.raises(RuntimeError, match=error):
        module.validate_postgres_environment(environment)


def test_postgres_partition_accepts_only_expected_capability_roles() -> None:
    module = _module()
    roles = module.validate_postgres_environment(
        {
            "TEST_DATABASE_URL": "postgresql+asyncpg://supportguard:pw@db:5432/app",
            "MCP_READ_DATABASE_URL": ("postgresql+asyncpg://supportguard_read_mcp:pw@db:5432/app"),
            "MCP_ACTION_DATABASE_URL": (
                "postgresql+asyncpg://supportguard_action_mcp:pw@db:5432/app"
            ),
        }
    )
    assert roles == {
        "TEST_DATABASE_URL": "supportguard",
        "MCP_READ_DATABASE_URL": "supportguard_read_mcp",
        "MCP_ACTION_DATABASE_URL": "supportguard_action_mcp",
    }


def test_make_and_ci_share_the_current_partitioned_mcp_entrypoint() -> None:
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "test-mcp-hermetic:" in makefile
    assert "run_mcp_test_partitions.py hermetic" in makefile
    assert "test-mcp-postgres:" in makefile
    assert "run_mcp_test_partitions.py postgres" in makefile
    assert "test-mcp:" in makefile
    assert "run_mcp_test_partitions.py all" in makefile
    assert makefile.count("MCP_READ_DATABASE_URL=$(TEST_MCP_READ_DATABASE_URL)") == 2
    assert makefile.count("MCP_ACTION_DATABASE_URL=$(TEST_MCP_ACTION_DATABASE_URL)") == 2
    assert "pytest -m mcp" not in makefile
    assert "- run: make test-mcp" in workflow
    assert "pytest -m mcp" not in workflow
    assert "run_isolated_integration.py integration" in makefile
    assert 'pytest -m "(postgres or redis) and not mcp"' not in makefile
    assert "run_isolated_integration.py integration" in workflow
    assert makefile.count('pytest -m "not mcp"') == 1
    assert workflow.count('pytest -m "not mcp"') == 1
    assert "--ignore-glob='backend/tests/test_eval*'" in workflow
    assert "corrective-v126" not in makefile
    assert "run_v126_gate.py" not in makefile


def test_postgres_partition_owns_and_cleans_a_unique_database() -> None:
    source = (ROOT / "scripts/run_mcp_test_partitions.py").read_text()
    assert "prepare_postgres_partition_environment(dict(os.environ))" in source
    assert 'f"supportguard_mcp_{os.getpid()}_{uuid4().hex[:10]}"' in source
    assert 'CREATE DATABASE "{database_name}"' in source
    assert "pg_terminate_backend(pid)" in source
    assert 'DROP DATABASE IF EXISTS "{partition.database_name}"' in source
    assert "cleanup_postgres_partition_database(isolated)" in source


def _registry_record(state: str, *, schema_hash: str | None) -> dict[str, object]:
    return {
        "schema": "v129-owned-session-registry.v1",
        "partition_id": "hermetic",
        "state": state,
        "module": "supportguard.mcp.read_server",
        "schema_hash": schema_hash,
        "leader_pid": 12345,
        "pgid": 12345,
        "sid": 12345,
        "process_birth_identity": {
            "platform": "linux",
            "boot_identity": "boot",
            "pid": 12345,
            "start_value": "100",
        },
        "ancestry_hash": "a" * 64,
        "owner_node": "backend/tests/test_read_mcp.py::owner",
        "discovery_count": 1 if state == "closed" else None,
        "call_count": 0 if state == "closed" else None,
    }


def _owned_lifecycle(*, owner: str, module: str, pid: int, start: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for state in ("registered", "confirmed", "closed"):
        record = _registry_record(
            state,
            schema_hash=None if state == "registered" else "b" * 64,
        )
        record.update(
            {
                "partition_id": "semantic:target:nonce",
                "module": module,
                "leader_pid": pid,
                "pgid": pid,
                "sid": pid,
                "owner_node": owner,
                "process_birth_identity": {
                    "platform": "linux",
                    "boot_identity": "boot",
                    "pid": pid,
                    "start_value": start,
                },
            }
        )
        records.append(record)
    return records


def test_mcp_registry_success_requires_matching_health_confirmation() -> None:
    validate_partition_confirmations(
        [
            _registry_record("registered", schema_hash=None),
            _registry_record("confirmed", schema_hash="b" * 64),
            _registry_record("closed", schema_hash="b" * 64),
        ],
        partition_id="hermetic",
    )


@pytest.mark.parametrize("variant", ["missing", "empty-schema", "different-birth"])
def test_mcp_registry_incomplete_confirmation_fails_closed(variant: str) -> None:
    records = [_registry_record("registered", schema_hash=None)]
    if variant != "missing":
        confirmed = _registry_record(
            "confirmed", schema_hash="" if variant == "empty-schema" else "b" * 64
        )
        if variant == "different-birth":
            confirmed = json.loads(json.dumps(confirmed))
            confirmed["process_birth_identity"]["start_value"] = "101"  # type: ignore[index]
        records.append(confirmed)
        records.append(_registry_record("closed", schema_hash="b" * 64))
    with pytest.raises(RuntimeError, match="mcp_registry_confirmation_mismatch"):
        validate_partition_confirmations(records, partition_id="hermetic")


def test_process_owner_manifest_rejects_unmapped_child(tmp_path: Path) -> None:
    manifest = tmp_path / "owners.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "supportguard-mcp-process-owner-manifest.v1",
                "owners": [
                    {
                        "node": "expected-node",
                        "partition": "hermetic",
                        "children": {"supportguard.mcp.read_server": 1},
                        "minimum_calls": 0,
                        "requires_reconnect": False,
                    }
                ],
            }
        )
    )
    record = _registry_record("closed", schema_hash="b" * 64)
    with pytest.raises(RuntimeError, match="owner_node_mismatch"):
        validate_process_owner_manifest([record], manifest_path=manifest, partition_id="hermetic")


def test_owned_partition_accepts_exact_lifecycle_and_explicit_zero_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "backend/tests/test_v126_collectors.py::owned"
    records = _owned_lifecycle(
        owner=owner,
        module="supportguard.mcp.read_server",
        pid=12345,
        start="100",
    )
    monkeypatch.setattr(
        "supportguard.evidence.mcp_test_registry.identity_matches",
        lambda _birth: False,
    )
    result = validate_owned_partition(
        records,
        partition_id="semantic:target:nonce",
        expected_children={owner: {"supportguard.mcp.read_server": 1}},
    )
    assert result["lifecycle_count"] == 1
    assert result["owners"] == [owner]
    zero = validate_owned_partition(
        records,
        partition_id="semantic:zero:nonce",
        expected_children={},
    )
    assert zero["lifecycle_count"] == 0
    assert zero["explicit_zero_child"] is True


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing-closed", "confirmation_mismatch"),
        ("wrong-owner", "owner_node_mismatch"),
        ("extra-module", "owner_child_count_mismatch"),
        ("live-process", "process_still_alive"),
    ],
)
def test_owned_partition_fails_closed_for_partial_or_unowned_lifecycle(
    monkeypatch: pytest.MonkeyPatch, mutation: str, error: str
) -> None:
    owner = "backend/tests/test_v126_collectors.py::owned"
    records = _owned_lifecycle(
        owner=owner,
        module="supportguard.mcp.read_server",
        pid=12345,
        start="100",
    )
    if mutation == "missing-closed":
        records.pop()
    elif mutation == "wrong-owner":
        records[-1]["owner_node"] = "other-node"
    elif mutation == "extra-module":
        records.extend(
            _owned_lifecycle(
                owner=owner,
                module="supportguard.mcp.action_server",
                pid=12346,
                start="101",
            )
        )
    monkeypatch.setattr(
        "supportguard.evidence.mcp_test_registry.identity_matches",
        lambda _birth: mutation == "live-process",
    )
    with pytest.raises(RuntimeError, match=error):
        validate_owned_partition(
            records,
            partition_id="semantic:target:nonce",
            expected_children={owner: {"supportguard.mcp.read_server": 1}},
        )


def test_scoped_process_environment_is_exact_secret_free_and_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRESERVED_HOST_VALUE", "before")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    projected = {
        "PATH": os.environ["PATH"],
        "PRESERVED_HOST_VALUE": "inside",
        REGISTRY_ENV: "/private/registry",
        PARTITION_ENV: "live:target:nonce",
        PARTITION_LEADER_ENV: str(os.getpid()),
        OWNER_NODE_ENV: "live-observation:target",
    }
    with (
        pytest.raises(RuntimeError, match="sentinel"),
        scoped_process_environment(projected),
    ):
        assert os.environ["PRESERVED_HOST_VALUE"] == "inside"
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert os.environ[OWNER_NODE_ENV] == "live-observation:target"
        raise RuntimeError("sentinel")
    assert os.environ["PRESERVED_HOST_VALUE"] == "before"
    assert os.environ["DEEPSEEK_API_KEY"] == "must-not-leak"
    assert OWNER_NODE_ENV not in os.environ
