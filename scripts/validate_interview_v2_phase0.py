"""Validate the immutable inputs frozen by Interview Edition v2.0 Phase 0."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from supportguard.rag.chunking import chunk_markdown
from supportguard.rag.manifest import load_manifest
from supportguard.validation.public_mirror import load_public_mirror_provenance

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "validation" / "contracts" / "interview_v2"
EVIDENCE = ROOT / "validation" / "evidence" / "interview_v2" / "phase0"

BASELINE_SHA = "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb"
AUTHORITY_SHA = "110d65c854f7dc98e85e775dc6070ee46726b2be"
ARCHIVE_TAG = "archive/interview-v2.0-baseline"
TAG_OBJECT_SHA = "d274ca18abe7c9c4c324a2d6caa7bbec0622f9b9"
ARCHIVE_TREE_SHA = "a192f8a50b3a4c770d2ac1a77620f830364f3289"
ARCHIVE_MANIFEST_SHA256 = "6f620e39bbbc42f03744f603e8bb0dd7e6026bbbfbef2aab774544adcceda07f"
CORPUS_MANIFEST_SHA256 = "faed4612819bddd775907486f77c8a2a955adb413d20f442e8b551f6b5147f3c"
CHUNKER_SHA256 = "ea5db8c22084a542d524806fd76513ae6ba798169165a32d24139eb12c07be13"
RAG_DATASET_SHA256 = "28c652e71d15dac7a5382c219c9d6cadab117f13abf81a595d91f8b30614ac86"
PHASE6_ARCHIVE_SOURCE_SHA = "328bc8606fdfbe50c9f3530646e72c1c21269c12"

CODE_MAP_TARGETS = (
    "backend/src/supportguard/api/messages.py",
    "backend/src/supportguard/agent/graph.py",
    "backend/src/supportguard/agent/decision.py",
    "backend/src/supportguard/agent/tool_loop.py",
    "backend/src/supportguard/agent/evidence.py",
    "backend/src/supportguard/agent/policy.py",
    "backend/src/supportguard/actions/service.py",
    "backend/src/supportguard/rag/service.py",
    "backend/src/supportguard/mcp/runtime.py",
    "backend/src/supportguard/runtime/worker.py",
    "backend/src/supportguard/db/security_contract.py",
    "frontend/src/App.tsx",
)
OWNER_MAPS = {
    "demo_429": [1, 10, 2, 3, 4, 9, 8, 5, 6, 12],
    "demo_duplicate_charge_refund": [1, 10, 2, 3, 4, 9, 8, 5, 6, 7, 11, 12],
    "demo_cross_tenant_denial": [1, 10, 2, 3, 4, 9, 6, 11, 12],
}
P16_CLASSES = {
    "429_diagnosis",
    "duplicate_charge_refund",
    "already_refunded",
    "api_key_revocation",
    "entitlement_change",
    "cross_tenant_denial",
    "insufficient_evidence",
    "natural_scope_continuation",
}


class Phase0ContractError(RuntimeError):
    """Raised when a frozen Phase 0 input is incomplete or inconsistent."""


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Phase0ContractError(message)


def _sequential_ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{index:02d}" for index in range(1, count + 1)]


def _validate_archive() -> dict[str, Any]:
    manifest_path = EVIDENCE / "archive-manifest.v1.json"
    receipt_path = EVIDENCE / "archive-restore-receipt.v1.json"
    verification_path = EVIDENCE / "archive-verification.v1.json"
    manifest = _load(manifest_path)
    receipt = _load(receipt_path)
    verification = _load(verification_path)

    _require(_sha256(manifest_path) == ARCHIVE_MANIFEST_SHA256, "archive manifest changed")
    _require(manifest["archive_tag"] == ARCHIVE_TAG, "archive tag name mismatch")
    _require(manifest["tag_object_type"] == "tag", "archive ref is not annotated")
    _require(manifest["tag_object_sha"] == TAG_OBJECT_SHA, "archive tag object mismatch")
    _require(manifest["commit_sha"] == BASELINE_SHA, "archive baseline mismatch")
    _require(manifest["git_tree_sha"] == ARCHIVE_TREE_SHA, "archive Git tree mismatch")
    _require(
        manifest["file_count"] == len(manifest["files"]) == 2617,
        "archive file count mismatch",
    )
    _require(
        not any(item["path"].startswith("evals/v6/holdout") for item in manifest["files"]),
        "protected Evaluation v6 holdout appeared in archive",
    )
    _require(
        not any(item["path"].startswith("evals/v6/private") for item in manifest["files"]),
        "protected Evaluation v6 private data appeared in archive",
    )
    for category in ("migration", "receipt", "matrix", "manifest", "prompt", "corpus"):
        _require(manifest["coverage_counts"][category] > 0, f"empty archive coverage: {category}")

    _require(receipt["result"] == "pass", "restore dry-run did not pass")
    _require(
        receipt["manifest_sha256"] == ARCHIVE_MANIFEST_SHA256,
        "restore receipt does not bind archive manifest",
    )
    _require(receipt["restored_head_sha"] == BASELINE_SHA, "restored checkout SHA mismatch")
    _require(receipt["worktree_clean"] is True, "restored checkout was dirty")
    _require(
        not receipt["missing_paths"]
        and not receipt["extra_paths"]
        and not receipt["mismatched_paths"],
        "restored checkout bytes differ",
    )

    _require(verification["status"] == "passed", "remote archive verification did not pass")
    _require(verification["remote_tag_object_sha"] == TAG_OBJECT_SHA, "remote tag object mismatch")
    _require(
        verification["remote_peeled_commit_sha"] == BASELINE_SHA, "remote peeled commit mismatch"
    )
    _require(
        verification["restore_worktree_removed"] is True
        and verification["restore_worktree_registry_matches"] == 0,
        "restore worktree cleanup was not proved",
    )

    public_provenance = load_public_mirror_provenance(ROOT)
    git = shutil.which("git")
    if git and public_provenance is None:
        local_type = subprocess.run(  # noqa: S603  # nosec B603
            [git, "cat-file", "-t", ARCHIVE_TAG],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        local_commit = subprocess.run(  # noqa: S603  # nosec B603
            [git, "rev-parse", f"{ARCHIVE_TAG}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _require(
            local_type == "tag" and local_commit == BASELINE_SHA,
            "local archive tag identity mismatch",
        )
    return {"files": manifest["file_count"], "restore": receipt["result"], "remote": "passed"}


def _validate_code_map() -> dict[str, Any]:
    contract = _load(CONTRACTS / "code-map-owner-dependency.v1.json")
    entries = contract["entries"]
    _require(contract["status"] == "approved_frozen_input", "code map is not frozen")
    _require(
        contract["authority"]["authority_commit"] == AUTHORITY_SHA,
        "code map authority SHA mismatch",
    )
    _require(contract["entry_count"] == len(entries) == 12, "code map must contain 12 entries")
    _require(
        [entry["ordinal"] for entry in entries] == list(range(1, 13)),
        "code map ordinals are not sequential",
    )
    _require(
        tuple(entry["target_entry_path"] for entry in entries) == CODE_MAP_TARGETS,
        "code map target entries differ from v2.0",
    )
    for entry in entries:
        _require(
            len(entry["target_one_hop_dependencies"]) <= 2,
            f"{entry['id']} exceeds one-hop dependency limit",
        )
        _require(entry["owns"] and entry["must_not_own"], f"{entry['id']} lacks ownership boundary")
        for owner in entry["current_owner_paths"]:
            _require((ROOT / owner).exists(), f"current owner path does not exist: {owner}")
    observed_maps = {key: value["ordinals"] for key, value in contract["owner_maps"].items()}
    _require(observed_maps == OWNER_MAPS, "Demo Owner Maps differ from v2.0")
    return {"entries": len(entries), "owner_maps": len(observed_maps)}


def _validate_test_node(selector: str) -> None:
    path_text, separator, test_name = selector.partition("::")
    path = ROOT / path_text
    if path.is_file():
        source = path.read_text(encoding="utf-8")
    else:
        if load_public_mirror_provenance(ROOT) is not None:
            disposition = _load(CONTRACTS / "test-disposition.v1.json")
            archived_nodes = {
                node for group in disposition["groups"] for node in group["old_test_nodes"]
            }
            _require(selector in archived_nodes, f"archived test selector is unbound: {selector}")
            return
        git = shutil.which("git")
        if git is None:
            raise Phase0ContractError("git is required to verify archived Phase 0 test nodes")
        result = subprocess.run(  # noqa: S603 - fixed Git read and frozen source commit
            [git, "show", f"{PHASE6_ARCHIVE_SOURCE_SHA}:{path_text}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        _require(result.returncode == 0, f"archived test file missing: {path_text}")
        source = result.stdout
    if separator:
        _require(
            test_name in source,
            f"test selector missing: {selector}",
        )


def _validate_characterization_and_safety() -> dict[str, Any]:
    behavior = _load(CONTRACTS / "behavior-characterization.v1.json")
    safety = _load(CONTRACTS / "safety-invariant-manifest.v1.json")
    observed = behavior["baseline_observed"]
    preserved = behavior["preserved_public_contract"]
    invariants = safety["invariants"]
    _require(
        behavior["status"] == safety["status"] == "approved_frozen_input",
        "behavior or safety input is not frozen",
    )
    _require(len(observed) == len(preserved) == 8, "characterization must freeze 8+8 contracts")
    _require(len(invariants) == 14, "safety manifest must freeze 14 invariants")
    _require(
        [item["id"] for item in observed] == _sequential_ids("IE-BC", 8),
        "behavior IDs are not sequential",
    )
    _require(
        [item["requirement_id"] for item in preserved] == _sequential_ids("IE-PC", 8),
        "preserved contract IDs are not sequential",
    )
    _require(
        [item["requirement_id"] for item in invariants] == _sequential_ids("IE-S", 14),
        "safety IDs are not sequential",
    )
    selectors: set[str] = set()
    for item in observed:
        selectors.update(item["evidence_nodes"])
    for item in preserved:
        selectors.update(item["current_test_nodes"])
    for item in invariants:
        selectors.update(item["current_public_test_nodes"])
    for selector in selectors:
        _validate_test_node(selector)
    _require(
        safety["test_disposition_rule"]["replacement_requires_simultaneous_old_and_new_green"]
        is True,
        "test replacement lost parallel-green rule",
    )
    _require(
        safety["test_disposition_rule"]["archive_only_before_phase_6"] is False,
        "safety tests may not be archived before Phase 6",
    )
    return {
        "baseline_observed": 8,
        "preserved_contracts": 8,
        "safety_invariants": 14,
        "test_nodes": len(selectors),
    }


def _validate_provider_fault_and_journey_contracts() -> dict[str, Any]:
    provider = _load(CONTRACTS / "ie-p16.v1.json")
    faults = _load(CONTRACTS / "ie-f06.v1.json")
    journeys = _load(CONTRACTS / "ie-j12.v1.json")
    scenarios = provider["scenarios"]
    _require(
        provider["status"] == "frozen" and provider["execution_state"] == "unexecuted",
        "IE-P16 must be frozen and unexecuted",
    )
    _require(provider["candidate_sha"] is None, "IE-P16 may not bind a Phase 0 candidate")
    _require(
        [item["id"] for item in scenarios] == _sequential_ids("IE-P", 16),
        "IE-P16 IDs are not sequential",
    )
    _require(
        Counter(item["class"] for item in scenarios) == Counter({name: 2 for name in P16_CLASSES}),
        "IE-P16 must contain two scenarios per semantic class",
    )
    _require(
        sum(len(item["turns"]) >= 2 for item in scenarios) >= 4,
        "IE-P16 must contain at least four multi-turn scenarios",
    )
    _require(
        provider["frozen_denominators"]
        == {
            "semantic_classes": 8,
            "scenarios": 16,
            "scenarios_per_class": 2,
            "multi_turn_minimum": 4,
            "fault_injected": 0,
        },
        "IE-P16 denominator changed",
    )
    _require(provider["runtime"]["provider"] == "deepseek", "IE-P16 provider changed")
    _require(provider["runtime"]["model"] == "deepseek-v4-flash", "IE-P16 model changed")
    _require(
        provider["runtime"]["tool_call_mode"] == "native", "IE-P16 must require native tool calling"
    )

    _require(
        faults["status"] == "frozen" and faults["execution_state"] == "unexecuted",
        "IE-F06 must be frozen and unexecuted",
    )
    _require(
        [item["id"] for item in faults["cases"]] == _sequential_ids("IE-F", 6),
        "IE-F06 IDs are not sequential",
    )
    _require(faults["classification"] == "fault-injected", "IE-F06 classification changed")
    _require(
        faults["separation_contract"]["included_in_ie_p16"] is False, "IE-F06 leaked into IE-P16"
    )
    for item in faults["cases"]:
        _require(item["classification"] == "fault-injected", f"{item['id']} is misclassified")
        for selector in item["deterministic_test_nodes"]:
            _validate_test_node(selector)

    _require(
        journeys["status"] == "frozen" and journeys["execution_state"] == "unexecuted",
        "IE-J12 must be frozen and unexecuted",
    )
    _require(
        [item["id"] for item in journeys["journeys"]] == _sequential_ids("IE-J", 12),
        "IE-J12 IDs are not sequential",
    )
    for item in journeys["journeys"]:
        for key in (
            "identity",
            "actors",
            "public_inputs",
            "seed_contract",
            "allowed_tools",
            "required_observations",
            "citation_or_hitl",
            "authoritative_terminal_facts",
            "security_assertions",
            "visible_acceptance",
            "mode",
        ):
            _require(key in item and item[key], f"{item['id']} lacks {key}")
    _require(
        journeys["claims"]["current_head_verified"] is False,
        "IE-J12 may not claim current verification in Phase 0",
    )
    return {
        "ie_p16": 16,
        "ie_p16_multi_turn": sum(len(item["turns"]) >= 2 for item in scenarios),
        "ie_f06": 6,
        "ie_j12": 12,
    }


def _generated_chunks() -> dict[str, tuple[str, str]]:
    manifest = ROOT / "knowledge" / "manifests" / "documents.json"
    generated: dict[str, tuple[str, str]] = {}
    for metadata in load_manifest(manifest):
        source = (ROOT / metadata.source_path).read_text(encoding="utf-8")
        for chunk in chunk_markdown(metadata, source):
            generated[chunk.chunk_id] = (chunk.document_id, chunk.section_path)
    return generated


def _validate_rag_dev30() -> dict[str, Any]:
    dataset_path = CONTRACTS / "rag-dev30.v1.jsonl"
    contract = _load(CONTRACTS / "rag-dev30.contract.v1.json")
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    _require(contract["status"] == "frozen-before-retrieval-tuning", "RAG Dev30 is not frozen")
    _require(
        _sha256(dataset_path) == contract["dataset_sha256"] == RAG_DATASET_SHA256,
        "RAG Dev30 bytes changed",
    )
    _require(
        _sha256(ROOT / contract["corpus_manifest"]["path"])
        == contract["corpus_manifest"]["sha256"]
        == CORPUS_MANIFEST_SHA256,
        "corpus manifest changed after Dev30 freeze",
    )
    _require(
        _sha256(ROOT / contract["chunker_contract"]["path"])
        == contract["chunker_contract"]["sha256"]
        == CHUNKER_SHA256,
        "chunker contract changed after Dev30 freeze",
    )
    _require(
        [item["id"] for item in cases] == _sequential_ids("IE-R", 30),
        "RAG Dev30 IDs are not sequential",
    )
    distribution = Counter(item["category"] for item in cases)
    _require(distribution == Counter(contract["distribution"]), "RAG Dev30 distribution changed")
    _require(all(item["resolved_chunk_ids"] for item in cases), "RAG case lacks a gold chunk")
    generated = _generated_chunks()
    for item in cases:
        _require(
            item["corpus_manifest_sha256"] == CORPUS_MANIFEST_SHA256,
            f"{item['id']} corpus binding changed",
        )
        _require(
            item["chunker_contract_sha256"] == CHUNKER_SHA256,
            f"{item['id']} chunker binding changed",
        )
        _require(
            bool(item["expected_refusal"]) == (item["category"] == "unanswerable"),
            f"{item['id']} refusal category mismatch",
        )
        _require(
            item["required_claims"] and item["forbidden_claims"],
            f"{item['id']} lacks claim boundaries",
        )
        for chunk_id in item["resolved_chunk_ids"]:
            _require(chunk_id in generated, f"{item['id']} references unknown chunk {chunk_id}")
            document_id, section_path = generated[chunk_id]
            _require(
                document_id in item["gold_document_ids"], f"{item['id']} chunk document is not gold"
            )
            _require(
                section_path in item["gold_section_paths"],
                f"{item['id']} chunk section is not gold",
            )
    _require(contract["online_reranker"] == "disabled", "online Reranker must remain disabled")
    _require(
        "evals/v6/holdout*" in contract["prohibited_inputs"]
        and "evals/datasets/scenarios.jsonl" in contract["prohibited_inputs"],
        "RAG Dev30 prohibited-input boundary is incomplete",
    )
    return {
        "cases": len(cases),
        "distribution": dict(sorted(distribution.items())),
        "generated_chunks": len(generated),
    }


def _validate_candidate_schema() -> dict[str, Any]:
    schema = _load(CONTRACTS / "candidate-identity.schema.json")
    required = set(schema["required"])
    for field in (
        "candidate",
        "archive_identity",
        "runtime_artifacts",
        "provider_contract",
        "prompt_hashes",
        "schema_hashes",
        "knowledge_identity",
        "database_identity",
        "contract_hashes",
        "validation_scope",
    ):
        _require(field in required, f"candidate identity does not require {field}")
    forbidden = schema["x-supportguard-forbidden-content"]
    _require(
        "Secret values" in forbidden and "raw Provider request or response payload" in forbidden,
        "candidate identity forbidden-content boundary is incomplete",
    )
    return {"required_fields": len(required), "forbidden_content_classes": len(forbidden)}


def validate() -> dict[str, Any]:
    return {
        "phase": "Interview Edition v2.0 Phase 0",
        "result": "pass",
        "archive": _validate_archive(),
        "code_map": _validate_code_map(),
        "characterization_and_safety": _validate_characterization_and_safety(),
        "frozen_matrices": _validate_provider_fault_and_journey_contracts(),
        "rag_dev30": _validate_rag_dev30(),
        "candidate_identity_schema": _validate_candidate_schema(),
        "protected_evaluation_accessed": False,
        "provider_executed": False,
    }


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False, indent=2, sort_keys=True))
