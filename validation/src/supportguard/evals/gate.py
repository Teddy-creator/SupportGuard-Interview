from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

CONTRACT_ROOT: Final = Path("validation/contracts/interview_v2")
CONTRACTS: Final = {
    "rag_dev30": (
        "rag-dev30.contract.v1.json",
        "24fbcc4d4ba0694ba0683b9ffb6527b1c5af9a848e3d4f1fc54451e6e02decae",
    ),
    "ie_f06": (
        "ie-f06.v1.json",
        "8cf149e20fc2977bcfa289335afba02167e28e4e8ff1722063c396b612c3422d",
    ),
    "ie_p16": (
        "ie-p16.v1.json",
        "0e8f518da1b3418f79ff89940f77c0bef3078e7ef517dbe31ea07a9b349ca998",
    ),
    "ie_j12": (
        "ie-j12.v1.json",
        "648d93ca4974ff10b8cc4fadab510710b81157006c93ae2cdf2edfdb065589fc",
    ),
}
RAG_DATASET = "rag-dev30.v1.jsonl"
RAG_DATASET_SHA256: Final = "28c652e71d15dac7a5382c219c9d6cadab117f13abf81a595d91f8b30614ac86"
BLOCKED_EXECUTION_COMMANDS: Final = frozenset({"rag-dev30", "ie-f06", "ie-p16", "ie-j12"})


class EvaluationGateError(RuntimeError):
    """The current Phase does not authorize the requested validation execution."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_exact(root: Path, name: str, expected_hash: str) -> dict[str, Any]:
    path = root / CONTRACT_ROOT / name
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationGateError(f"current validation contract unavailable: {name}") from exc
    if hashlib.sha256(payload).hexdigest() != expected_hash or not isinstance(value, dict):
        raise EvaluationGateError(f"current validation contract identity mismatch: {name}")
    return value


def recompute_evaluation_status(root: Path) -> dict[str, object]:
    root = root.resolve()
    payloads = {
        key: _load_exact(root, name, expected_hash)
        for key, (name, expected_hash) in CONTRACTS.items()
    }
    rag = payloads["rag_dev30"]
    rag_dataset = root / CONTRACT_ROOT / RAG_DATASET
    if (
        rag.get("status") != "frozen-before-retrieval-tuning"
        or rag.get("case_count") != 30
        or rag.get("dataset_path") != str(CONTRACT_ROOT / RAG_DATASET)
        or rag.get("dataset_sha256") != RAG_DATASET_SHA256
        or _sha256(rag_dataset) != RAG_DATASET_SHA256
        or len(rag_dataset.read_text(encoding="utf-8").splitlines()) != 30
    ):
        raise EvaluationGateError("RAG Dev30 frozen contract mismatch")
    for key in ("ie_f06", "ie_p16", "ie_j12"):
        item = payloads[key]
        if (
            item.get("status") != "frozen"
            or item.get("execution_state") != "unexecuted"
            or item.get("candidate_sha") is not None
        ):
            raise EvaluationGateError(f"current validation input was consumed early: {key}")
    if len(payloads["ie_f06"].get("cases", [])) != 6:
        raise EvaluationGateError("IE-F06 denominator mismatch")
    if len(payloads["ie_p16"].get("scenarios", [])) != 16:
        raise EvaluationGateError("IE-P16 denominator mismatch")
    if len(payloads["ie_j12"].get("journeys", [])) != 12:
        raise EvaluationGateError("IE-J12 denominator mismatch")
    return {
        "active_dataset": None,
        "candidate_eligible": False,
        "execution_allowed": False,
        "contracts": {
            "rag_dev30": "frozen_unexecuted",
            "ie_f06": "frozen_unexecuted",
            "ie_p16": "frozen_unexecuted",
            "ie_j12": "frozen_unexecuted",
        },
        "denominators": {"rag_dev30": 30, "ie_f06": 6, "ie_p16": 16, "ie_j12": 12},
        "protected_holdout": "not_accessed",
        "cross_encoder": "not_executed",
        "evidence": {
            key: {
                "path": str(CONTRACT_ROOT / name),
                "sha256": expected_hash,
            }
            for key, (name, expected_hash) in CONTRACTS.items()
        },
    }


def enforce_evaluation_route(command: str) -> None:
    if command == "validate":
        return
    if command not in BLOCKED_EXECUTION_COMMANDS:
        raise EvaluationGateError(f"unknown current validation route: {command}")
    raise EvaluationGateError(
        f"Phase 7 execution not started: {command}; blocked before artifact access or Provider"
    )
