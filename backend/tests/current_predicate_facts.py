"""Current test-only raw predicate operand recorder.

The historical v1.2 evidence collectors are archived in Phase 6.  Current
contract tests retain this small, result-free recorder without depending on
the Validation distribution or restoring any historical adjudicator.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FACT_SINK_ENV = "SUPPORTGUARD_EVIDENCE_FACT_SINK"
EXPECTED_PREDICATES_ENV = "SUPPORTGUARD_EVIDENCE_EXPECTED_PREDICATES"
SCHEMA_VERSION = "supportguard.interview-v2.raw-predicate-operands.v1"
_BINDING_ENV = {
    "target_invocation_id": "SUPPORTGUARD_EVIDENCE_TARGET_INVOCATION_ID",
    "runner_nonce": "SUPPORTGUARD_EVIDENCE_RUNNER_NONCE",
    "collector_nonce": "SUPPORTGUARD_EVIDENCE_COLLECTOR_NONCE",
    "window_started_at": "SUPPORTGUARD_EVIDENCE_WINDOW_STARTED_AT",
}
_FORBIDDEN_OPERAND_KEYS = {
    "fixed",
    "not_applicable",
    "observed",
    "passed",
    "result",
    "value",
}


def _binding_from_environment() -> dict[str, str] | None:
    values = {name: os.getenv(env_name, "") for name, env_name in _BINDING_ENV.items()}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise RuntimeError("predicate_fact_binding_incomplete")
    return values


def _assert_operand_shape(value: Any) -> None:
    if isinstance(value, dict):
        if _FORBIDDEN_OPERAND_KEYS & set(value):
            raise RuntimeError("predicate_fact_semantic_result_forbidden")
        for nested in value.values():
            _assert_operand_shape(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_operand_shape(nested)


def record_predicate_operands(
    *,
    requirement_id: str,
    predicate_id: str,
    subject_kind: str,
    operands: dict[str, Any],
) -> None:
    """Append raw operands only when an explicitly bound private sink is present."""

    sink_raw = os.getenv(FACT_SINK_ENV, "")
    binding = _binding_from_environment()
    if not sink_raw and binding is None:
        return
    if not sink_raw or binding is None:
        raise RuntimeError("predicate_fact_sink_binding_mismatch")
    node_id = os.getenv("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
    if not node_id or not requirement_id or not predicate_id or not subject_kind or not operands:
        raise RuntimeError("predicate_fact_identity_incomplete")
    expected_raw = os.getenv(EXPECTED_PREDICATES_ENV, "")
    if expected_raw:
        try:
            decoded = json.loads(expected_raw)
            expected = {
                (str(item[0]), str(item[1]))
                for item in decoded
                if isinstance(item, list)
                and len(item) == 2
                and all(isinstance(value, str) and value for value in item)
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError("predicate_fact_allowlist_invalid") from exc
        if not isinstance(decoded, list) or len(expected) != len(decoded):
            raise RuntimeError("predicate_fact_allowlist_invalid")
        if (requirement_id, predicate_id) not in expected:
            return
    _assert_operand_shape(operands)
    captured_at = datetime.now(UTC).isoformat()
    identity = {
        "requirement_id": requirement_id,
        "predicate_id": predicate_id,
        "producer_node_id": node_id,
        "producer_pid": os.getpid(),
        "captured_at": captured_at,
        **binding,
    }
    observation = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": "predicate:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "observation_class": "predicate_business_fact",
        "subject_kind": subject_kind,
        **binding,
        "captured_at": captured_at,
        "payload": {**identity, "operands": operands},
    }
    encoded = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    sink = Path(sink_raw)
    sink.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(sink, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
