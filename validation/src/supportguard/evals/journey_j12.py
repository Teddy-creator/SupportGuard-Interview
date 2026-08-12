from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

_REQUIRED_PROOF_KEYS = (
    "backend_full",
    "integration_current",
    "mcp_current",
    "frontend_unit",
    "browser_current_19",
    "clean_compose",
)
_JOURNEY_PROOFS: dict[str, tuple[str, ...]] = {
    "IE-J01": ("backend_full", "integration_current", "browser_current_19", "clean_compose"),
    "IE-J02": (
        "backend_full",
        "integration_current",
        "mcp_current",
        "browser_current_19",
        "clean_compose",
    ),
    "IE-J03": ("backend_full", "integration_current", "browser_current_19"),
    "IE-J04": ("backend_full", "integration_current"),
    "IE-J05": ("backend_full", "integration_current", "mcp_current", "clean_compose"),
    "IE-J06": ("backend_full", "integration_current", "mcp_current", "clean_compose"),
    "IE-J07": ("backend_full", "integration_current", "browser_current_19"),
    "IE-J08": ("backend_full", "integration_current", "frontend_unit"),
    "IE-J09": ("backend_full", "integration_current", "browser_current_19"),
    "IE-J10": ("backend_full", "integration_current", "mcp_current"),
    "IE-J11": ("backend_full", "integration_current", "mcp_current", "clean_compose"),
    "IE-J12": ("backend_full", "integration_current", "frontend_unit", "browser_current_19"),
}


def _load_contract(root: Path) -> dict[str, Any]:
    name, expected_hash = CONTRACTS["ie_j12"]
    path = root / CONTRACT_ROOT / name
    if sha256_file(path) != expected_hash:
        raise Phase7ContractError("ie_j12_contract_hash_mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value.get("journeys", [])) != 12:
        raise Phase7ContractError("ie_j12_denominator_mismatch")
    ids = [str(item.get("id")) for item in value["journeys"]]
    if ids != [f"IE-J{ordinal:02d}" for ordinal in range(1, 13)]:
        raise Phase7ContractError("ie_j12_identity_mismatch")
    if set(ids) != set(_JOURNEY_PROOFS):
        raise Phase7ContractError("ie_j12_proof_mapping_mismatch")
    return value


def preflight(root: Path) -> dict[str, Any]:
    contract = _load_contract(root.resolve())
    return {
        "schema": "supportguard.interview_v2.ie_j12_preflight.v1",
        "contract_sha256": CONTRACTS["ie_j12"][1],
        "journeys": len(contract["journeys"]),
        "proof_keys": list(_REQUIRED_PROOF_KEYS),
        "provider_quality_measured": False,
        "protected_holdout_accessed": False,
        "cross_encoder_executed": False,
    }


def _load_proof(root: Path, relative_path: str, expected_candidate: str) -> dict[str, Any]:
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Phase7ContractError("ie_j12_proof_outside_repository") from exc
    if not path.is_file():
        raise Phase7ContractError(f"ie_j12_proof_missing:{relative_path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase7ContractError(f"ie_j12_proof_malformed:{relative_path}") from exc
    if not isinstance(value, dict):
        raise Phase7ContractError(f"ie_j12_proof_shape_invalid:{relative_path}")
    candidate = value.get("candidate_sha")
    if candidate is None and isinstance(value.get("candidate"), dict):
        candidate = value["candidate"].get("candidate_sha")
    if candidate != expected_candidate:
        raise Phase7ContractError(f"ie_j12_proof_candidate_mismatch:{relative_path}")
    if value.get("passed") is not True:
        raise Phase7ContractError(f"ie_j12_proof_not_passed:{relative_path}")
    return value


def execute(
    root: Path,
    *,
    candidate_sha: str,
    output: Path,
    proof_manifest: Path,
) -> dict[str, Any]:
    root = root.resolve()
    identity_before = require_candidate(root, candidate_sha)
    output = require_ignored_output(root, output)
    contract = _load_contract(root)
    manifest = json.loads(proof_manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != set(_REQUIRED_PROOF_KEYS):
        raise Phase7ContractError("ie_j12_proof_manifest_inventory_mismatch")
    proofs = {
        key: _load_proof(root, str(manifest[key]), candidate_sha) for key in _REQUIRED_PROOF_KEYS
    }
    proof_receipts = {
        key: {
            "path": str(manifest[key]),
            "sha256": sha256_file(root / str(manifest[key])),
            "schema": proofs[key].get("schema"),
            "denominator": proofs[key].get("denominator"),
            "passed": True,
        }
        for key in _REQUIRED_PROOF_KEYS
    }
    journey_by_id = {str(item["id"]): item for item in contract["journeys"]}
    results = [
        {
            "id": journey_id,
            "title": journey_by_id[journey_id]["title"],
            "mode": journey_by_id[journey_id]["mode"],
            "proof_keys": list(required),
            "passed": all(proofs[key].get("passed") is True for key in required),
        }
        for journey_id, required in _JOURNEY_PROOFS.items()
    ]
    identity_after = require_candidate(root, candidate_sha)
    if identity_after != identity_before:
        raise Phase7ContractError("candidate_source_changed_during_ie_j12")
    passed = sum(bool(item["passed"]) for item in results)
    receipt = {
        "schema": "supportguard.interview_v2.ie_j12_receipt.v1",
        "classification": "public_deterministic_product_journeys_not_provider_quality",
        "recorded_at": utc_now(),
        "candidate": identity_before.as_dict(),
        "contract_sha256": CONTRACTS["ie_j12"][1],
        "denominator": 12,
        "passed": passed,
        "failed": 12 - passed,
        "journeys": results,
        "proof_receipts": proof_receipts,
        "claims": {
            "passed": passed == 12,
            "provider_quality_measured": False,
            "evaluation_v6_holdout_accessed": False,
            "cross_encoder_executed": False,
            "three_primary_web_demos_included": all(item["passed"] for item in results[:3]),
        },
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    atomic_write_json(output, receipt)
    if passed != 12:
        raise Phase7ContractError("ie_j12_matrix_failed")
    return receipt
