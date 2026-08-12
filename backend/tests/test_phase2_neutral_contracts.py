from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from supportguard.contracts import mcp_lifecycle, process_identity
from supportguard.evidence import mcp_test_registry, process_contract

SRC = Path(__file__).resolve().parents[1] / "src" / "supportguard"


def test_process_identity_is_owned_by_neutral_contract_and_reexported() -> None:
    assert process_contract.ProcessBirthIdentity is process_identity.ProcessBirthIdentity
    assert process_contract.process_birth_identity is process_identity.process_birth_identity
    assert process_contract.identity_matches is process_identity.identity_matches
    assert process_identity.ProcessBirthIdentity.__module__ == (
        "supportguard.contracts.process_identity"
    )


def test_mcp_lifecycle_writer_is_neutral_and_evidence_reader_reexports_it() -> None:
    assert mcp_test_registry.REGISTRY_SCHEMA == mcp_lifecycle.REGISTRY_SCHEMA
    assert mcp_test_registry.REGISTRY_ENV == mcp_lifecycle.REGISTRY_ENV
    assert mcp_test_registry.PARTITION_ENV == mcp_lifecycle.PARTITION_ENV
    assert mcp_test_registry.PARTITION_LEADER_ENV == mcp_lifecycle.PARTITION_LEADER_ENV
    assert mcp_test_registry.OWNER_NODE_ENV == mcp_lifecycle.OWNER_NODE_ENV
    assert mcp_test_registry.ancestry_hash is mcp_lifecycle.ancestry_hash
    assert mcp_test_registry.write_record is mcp_lifecycle.write_record
    assert mcp_test_registry.write_closed_record is mcp_lifecycle.write_closed_record


def test_neutral_writer_emits_lifecycle_that_evidence_reader_adjudicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 3210
    birth = process_identity.ProcessBirthIdentity("test", "boot-1", pid, "start-1")
    registry = mcp_test_registry.create_registry(tmp_path / "registry")
    monkeypatch.setattr(mcp_lifecycle, "process_birth_identity", lambda value: birth)
    monkeypatch.setattr(mcp_lifecycle.os, "getpgid", lambda value: value)
    monkeypatch.setattr(mcp_lifecycle.os, "getsid", lambda value: value)
    monkeypatch.setattr(
        mcp_lifecycle,
        "ancestry_hash",
        lambda **values: "a" * 64,
    )

    registered = mcp_lifecycle.write_record(
        registry=registry,
        partition_id="partition-1",
        state="registered",
        module="supportguard.mcp.read_server",
        pid=pid,
        partition_leader_pid=3000,
        schema_hash=None,
        owner_node="test-node",
    )
    confirmed = mcp_lifecycle.write_record(
        registry=registry,
        partition_id="partition-1",
        state="confirmed",
        module="supportguard.mcp.read_server",
        pid=pid,
        partition_leader_pid=3000,
        schema_hash="b" * 64,
        owner_node="test-node",
    )
    mcp_lifecycle.write_closed_record(
        registry=registry,
        confirmed=confirmed,
        discovery_count=1,
        call_count=2,
    )

    records = mcp_test_registry.load_records(registry)
    mcp_test_registry.validate_partition_confirmations(
        records,
        partition_id="partition-1",
    )
    assert [record["state"] for record in records] == [
        "closed",
        "confirmed",
        "registered",
    ]
    assert registered["process_birth_identity"] == birth.payload()
    closed_path = next(registry.glob("closed-*.json"))
    assert json.loads(closed_path.read_text(encoding="utf-8"))["call_count"] == 2


def test_neutral_lifecycle_writer_rejects_unknown_state(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="mcp_registry_state_invalid"):
        mcp_lifecycle.write_record(
            registry=tmp_path,
            partition_id="partition-1",
            state=cast(mcp_lifecycle.LifecycleState, "accepted"),
            module="supportguard.mcp.read_server",
            pid=3210,
            partition_leader_pid=3000,
            schema_hash=None,
        )


def test_runtime_source_tree_has_no_evidence_imports() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if "evidence" in path.relative_to(SRC).parts:
            continue
        if "supportguard.evidence" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_neutral_contracts_do_not_depend_on_evidence_package() -> None:
    for module in (mcp_lifecycle, process_identity):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "supportguard.evidence" not in source
