from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from supportguard.contracts import mcp_lifecycle as _lifecycle
from supportguard.contracts.process_identity import (
    ProcessBirthIdentity,
    identity_matches,
)

OWNER_NODE_ENV = _lifecycle.OWNER_NODE_ENV
PARTITION_ENV = _lifecycle.PARTITION_ENV
PARTITION_LEADER_ENV = _lifecycle.PARTITION_LEADER_ENV
REGISTRY_ENV = _lifecycle.REGISTRY_ENV
REGISTRY_SCHEMA = _lifecycle.REGISTRY_SCHEMA
ancestry_hash = _lifecycle.ancestry_hash
write_closed_record = _lifecycle.write_closed_record
write_record = _lifecycle.write_record

_ENVIRONMENT_LOCK = threading.RLock()


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise RuntimeError("mcp_registry_record_malformed")
    return value


def create_registry(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(path, 0o700)
    return path


def load_records(registry: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    identities: set[tuple[str, int, str]] = set()
    for path in sorted(registry.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("mcp_registry_record_malformed") from exc
        if not isinstance(payload, dict) or payload.get("schema") != REGISTRY_SCHEMA:
            raise RuntimeError("mcp_registry_record_malformed")
        birth_payload = payload.get("process_birth_identity")
        if not isinstance(birth_payload, dict):
            raise RuntimeError("mcp_registry_record_malformed")
        identity = (
            str(payload.get("state")),
            _integer(payload.get("leader_pid")),
            str(birth_payload.get("start_value")),
        )
        if identity in identities:
            raise RuntimeError("mcp_registry_duplicate_identity")
        identities.add(identity)
        records.append(payload)
    return records


def validate_partition_confirmations(
    records: list[dict[str, object]], *, partition_id: str
) -> None:
    partition_records = [item for item in records if item.get("partition_id") == partition_id]

    def identity_key(item: dict[str, object]) -> tuple[object, object, object]:
        birth = item.get("process_birth_identity")
        if not isinstance(birth, dict):
            raise RuntimeError("mcp_registry_record_malformed")
        return item.get("module"), item.get("leader_pid"), birth.get("start_value")

    registered = {
        identity_key(item) for item in partition_records if item.get("state") == "registered"
    }
    confirmed = {
        identity_key(item)
        for item in partition_records
        if item.get("state") == "confirmed"
        and isinstance(item.get("schema_hash"), str)
        and bool(item.get("schema_hash"))
    }
    closed = {
        identity_key(item)
        for item in partition_records
        if item.get("state") == "closed"
        and isinstance(item.get("discovery_count"), int)
        and _integer(item.get("discovery_count")) >= 1
        and isinstance(item.get("call_count"), int)
        and _integer(item.get("call_count")) >= 0
    }
    if registered != confirmed or confirmed != closed:
        raise RuntimeError("mcp_registry_confirmation_mismatch")


def validate_owned_partition(
    records: list[dict[str, object]],
    *,
    partition_id: str,
    expected_children: Mapping[str, Mapping[str, int]],
) -> dict[str, object]:
    """Validate one semantic/live partition from registration through process exit."""

    partition_records = [item for item in records if item.get("partition_id") == partition_id]
    validate_partition_confirmations(records, partition_id=partition_id)

    def identity_key(item: dict[str, object]) -> tuple[str, int, str]:
        birth = item.get("process_birth_identity")
        if not isinstance(birth, dict):
            raise RuntimeError("mcp_registry_record_malformed")
        return (
            str(item.get("module")),
            _integer(item.get("leader_pid")),
            str(birth.get("start_value")),
        )

    by_state: dict[str, dict[tuple[str, int, str], dict[str, object]]] = {
        "registered": {},
        "confirmed": {},
        "closed": {},
    }
    for record in partition_records:
        state = str(record.get("state"))
        if state not in by_state:
            raise RuntimeError("mcp_registry_record_malformed")
        identity = identity_key(record)
        if identity in by_state[state]:
            raise RuntimeError("mcp_registry_duplicate_identity")
        by_state[state][identity] = record

    identities = set(by_state["closed"])
    actual_children: dict[str, dict[str, int]] = {}
    schema_hashes: set[str] = set()
    for identity in identities:
        lifecycle = [by_state[state][identity] for state in ("registered", "confirmed", "closed")]
        owners = {item.get("owner_node") for item in lifecycle}
        if len(owners) != 1 or None in owners or "" in owners:
            raise RuntimeError("mcp_registry_owner_node_mismatch")
        owner = str(next(iter(owners)))
        module, pid, _start = identity
        if module not in {"supportguard.mcp.read_server", "supportguard.mcp.action_server"}:
            raise RuntimeError("mcp_registry_module_invalid")
        births = [item.get("process_birth_identity") for item in lifecycle]
        if (
            any(not isinstance(item, dict) or item.get("pid") != pid for item in births)
            or births[0] != births[1]
            or births[1] != births[2]
        ):
            raise RuntimeError("mcp_registry_process_identity_mismatch")
        ancestry_hashes = {item.get("ancestry_hash") for item in lifecycle}
        if (
            any(
                item.get("leader_pid") != pid
                or item.get("pgid") != pid
                or item.get("sid") != pid
                or not isinstance(item.get("ancestry_hash"), str)
                or len(str(item.get("ancestry_hash"))) != 64
                for item in lifecycle
            )
            or len(ancestry_hashes) != 1
        ):
            raise RuntimeError("mcp_registry_process_identity_mismatch")
        confirmed_schema = by_state["confirmed"][identity].get("schema_hash")
        closed_schema = by_state["closed"][identity].get("schema_hash")
        if (
            not isinstance(confirmed_schema, str)
            or not confirmed_schema
            or confirmed_schema != closed_schema
        ):
            raise RuntimeError("mcp_registry_schema_hash_mismatch")
        closed = by_state["closed"][identity]
        if (
            not isinstance(closed.get("discovery_count"), int)
            or _integer(closed.get("discovery_count")) < 1
            or not isinstance(closed.get("call_count"), int)
            or _integer(closed.get("call_count")) < 0
        ):
            raise RuntimeError("mcp_registry_confirmation_mismatch")
        if identity_matches(birth_from_record(closed)):
            raise RuntimeError("mcp_registry_process_still_alive")
        modules = actual_children.setdefault(owner, {})
        modules[module] = modules.get(module, 0) + 1
        schema_hashes.add(confirmed_schema)

    normalized_expected = {
        str(owner): {str(module): _integer(count) for module, count in children.items()}
        for owner, children in expected_children.items()
    }
    if set(actual_children) != set(normalized_expected):
        raise RuntimeError("mcp_registry_owner_node_mismatch")
    if actual_children != normalized_expected:
        raise RuntimeError("mcp_registry_owner_child_count_mismatch")
    return {
        "schema": "supportguard-mcp-owned-partition-summary.v1",
        "partition_id": partition_id,
        "owners": sorted(actual_children),
        "children": actual_children,
        "lifecycle_count": len(identities),
        "registered_count": len(by_state["registered"]),
        "confirmed_count": len(by_state["confirmed"]),
        "closed_count": len(by_state["closed"]),
        "schema_hashes": sorted(schema_hashes),
        "explicit_zero_child": not normalized_expected,
        "orphan_count": 0,
    }


@contextmanager
def scoped_process_environment(environment: Mapping[str, str]) -> Iterator[None]:
    """Install one exact child environment and restore the host even on failure."""

    if "DEEPSEEK_API_KEY" in environment:
        raise RuntimeError("mcp_owner_environment_secret_forbidden")
    with _ENVIRONMENT_LOCK:
        original = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update({str(key): str(value) for key, value in environment.items()})
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)


def validate_process_owner_manifest(
    records: list[dict[str, object]], *, manifest_path: Path, partition_id: str
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("mcp_process_owner_manifest_malformed") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "supportguard-mcp-process-owner-manifest.v1"
        or not isinstance(manifest.get("owners"), list)
    ):
        raise RuntimeError("mcp_process_owner_manifest_malformed")
    owners = [
        item
        for item in manifest["owners"]
        if isinstance(item, dict) and item.get("partition") == partition_id
    ]
    expected_nodes = {str(item.get("node")) for item in owners}
    closed = [
        item
        for item in records
        if item.get("partition_id") == partition_id and item.get("state") == "closed"
    ]
    observed_nodes = {str(item.get("owner_node")) for item in closed}
    if observed_nodes != expected_nodes:
        raise RuntimeError("mcp_process_owner_node_mismatch")
    for owner in owners:
        node = str(owner.get("node"))
        children = owner.get("children")
        minimum_calls = owner.get("minimum_calls")
        if not isinstance(children, dict) or not isinstance(minimum_calls, int):
            raise RuntimeError("mcp_process_owner_manifest_malformed")
        owned = [item for item in closed if item.get("owner_node") == node]
        actual_children: dict[str, int] = {}
        for item in owned:
            module = str(item.get("module"))
            actual_children[module] = actual_children.get(module, 0) + 1
            if _integer(item.get("discovery_count")) < 1:
                raise RuntimeError("mcp_process_owner_discovery_missing")
        expected_children = {str(key): _integer(value) for key, value in children.items()}
        if actual_children != expected_children:
            raise RuntimeError("mcp_process_owner_child_count_mismatch")
        actual_calls = sum(_integer(item.get("call_count")) for item in owned)
        if actual_calls < minimum_calls:
            raise RuntimeError("mcp_process_owner_call_count_mismatch")
        requires_reconnect = owner.get("requires_reconnect")
        if not isinstance(requires_reconnect, bool):
            raise RuntimeError("mcp_process_owner_manifest_malformed")
        if requires_reconnect and max(actual_children.values(), default=0) < 2:
            raise RuntimeError("mcp_process_owner_reconnect_missing")


def birth_from_record(record: dict[str, object]) -> ProcessBirthIdentity:
    raw = record.get("process_birth_identity")
    if not isinstance(raw, dict):
        raise RuntimeError("mcp_registry_record_malformed")
    return ProcessBirthIdentity(
        platform=str(raw.get("platform")),
        boot_identity=str(raw.get("boot_identity")),
        pid=int(raw.get("pid", 0)),
        start_value=str(raw.get("start_value")),
    )
