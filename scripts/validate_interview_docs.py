#!/usr/bin/env python3
"""Validate the compact Interview v2 current authority after Phase 6 pruning."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final, cast

from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.evals.gate import recompute_evaluation_status

ROOT: Final = Path(__file__).resolve().parents[1]
GIT: Final = shutil.which("git") or "/usr/bin/git"
CURRENT_DOCS: Final = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "docs/interview-edition-simplification-v2.0.md",
        "docs/architecture.md",
        "docs/interview-guide.md",
        "docs/demo-runbook.md",
        "docs/operations.md",
        "docs/release-verification.md",
    }
)
PHASE5_CANDIDATE: Final = "70717d8f19a9cbe3d8ead99db228c93f1577acc4"
PHASE5_TREE: Final = "853acfafd9782e2ce2d984cdd75da959718045a8"
PHASE5_RECEIPT = Path("validation/evidence/interview_v2/phase5/phase5-validation-receipt.v1.json")
PHASE5_RECEIPT_SHA256: Final = "bbbc1d13156604b6bef8b36ccbeacd5bcdb2050c045d391ffeb26a8f15d55d8a"
PHASE5_HOSTED_RECEIPT = Path("validation/evidence/interview_v2/phase5/hosted-ci-receipt.v1.json")
PHASE5_HOSTED_RECEIPT_SHA256: Final = (
    "137939f829d4dff07c2454f48514d2b1d26135af633bb2617aa70f8945d9a886"
)
PHASE6_MANIFEST = Path(
    "validation/evidence/interview_v2/phase6/archive-transition-manifest.v1.json"
)
PHASE6_CANDIDATE: Final = "30254587585fa2169cab071a926c501e06dac9a6"
PHASE6_TREE: Final = "199ca61783c5857cc95f83a468f1b80a5a313d81"
PHASE6_RECEIPT = Path("validation/evidence/interview_v2/phase6/phase6-validation-receipt.v1.json")
PHASE6_RECEIPT_SHA256: Final = "e73b22d8888ace2838e135eaa5ce28d180c7dba5e476e932eb0e57e0c219d1d9"
PHASE6_HOSTED_RECEIPT = Path("validation/evidence/interview_v2/phase6/hosted-ci-receipt.v1.json")
PHASE6_HOSTED_RECEIPT_SHA256: Final = (
    "6bdb72e7b60ca994b561df1b88c7738acffedffe0d15f740b8a5902c07e1a41e"
)
PHASE7_FAILED_CANDIDATE: Final = "b132c395c2edf2d7d72477dc9051bffc3d7f4024"
PHASE7_FAILED_TREE: Final = "78ed357459173ebb5354f24396fb42e96a22a98d"
PHASE7_FAILED_P16_RECEIPT = Path(
    "validation/evidence/interview_v2/phase7/attempts/"
    "ie-p16-b132c395c2edf2d7d72477dc9051bffc3d7f4024.json"
)
PHASE7_FAILED_P16_RECEIPT_SHA256: Final = (
    "68cf3f1d4c9bb8ade2fdca5b7b5d404cef3dc5822d751e34fbc416d245ec6bfa"
)
PHASE7_REPLACEMENT_CANDIDATE: Final = "7527c0acca079f57549538e49135a91ef87b9389"
PHASE7_REPLACEMENT_TREE: Final = "b9d96a0dd984cf8874a00f8f00172ac6f34db4be"
PHASE7_REPLACEMENT_P16_RECEIPT = Path(
    "validation/evidence/interview_v2/phase7/attempts/"
    "ie-p16-7527c0acca079f57549538e49135a91ef87b9389.json"
)
PHASE7_REPLACEMENT_P16_RECEIPT_SHA256: Final = (
    "450a121f1bd77b8dd0beb9cb09a116ad0ba1993aee48f31917ce79f5f7f68e58"
)
PHASE7_REPLACEMENT_HOSTED_RECEIPT = Path(
    "validation/evidence/interview_v2/phase7/"
    "hosted-ci-7527c0acca079f57549538e49135a91ef87b9389.json"
)
PHASE7_REPLACEMENT_HOSTED_RECEIPT_SHA256: Final = (
    "090e253cc4e2eb86167e240dc07a50bd18ad00d5aa6ce66562cfd95d72357eb0"
)
PHASE7_REPLACEMENT_VALIDATION_RECEIPT = Path(
    "validation/evidence/interview_v2/phase7/phase7-replacement-validation-receipt.v1.json"
)
PHASE7_REPLACEMENT_VALIDATION_RECEIPT_SHA256: Final = (
    "f470c557f61d17b6abf3866f2d56111b9a2c33e5d26978f8b03fbc9c144c6150"
)
PHASE7_REPLACEMENT_USAGE_LIMITATION: Final = (
    "The generic exception fallback recorded zero provider usage because no database "
    "usage snapshot was available; actual provider usage for IE-P14, IE-P15, and "
    "IE-P16 is unknown."
)
PHASE7_REPLACEMENT_TIMEOUT_LIMITATION: Final = (
    "The receipt did not record which HTTP phase or endpoint raised ReadTimeout."
)
PHASE7_REPLACEMENT_COST_LIMITATION: Final = (
    "The receipt total tokens and estimated cost exclude any unobserved usage from "
    "the failed scenarios."
)
TEST_DISPOSITION = Path("validation/contracts/interview_v2/test-disposition.v1.json")
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"documentation_contract_not_object:{path}")
    return cast(dict[str, Any], value)


def _tracked_current_docs() -> set[str]:
    result = subprocess.run(  # noqa: S603 - fixed Git read-only command
        [GIT, "ls-files", "README.md", "AGENTS.md", "docs/*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _validate_markdown_links(paths: set[str]) -> None:
    broken: list[str] = []
    for raw_path in sorted(paths):
        path = ROOT / raw_path
        for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path or not target_path.endswith(".md"):
                continue
            resolved = (path.parent / target_path).resolve()
            if not resolved.is_file():
                broken.append(f"{raw_path}->{target}")
    if broken:
        raise RuntimeError(f"current_documentation_broken_links:{broken}")


def validate() -> dict[str, object]:
    tracked_docs = _tracked_current_docs()
    if tracked_docs != CURRENT_DOCS:
        raise RuntimeError(
            "current_authority_document_inventory_mismatch:"
            f"expected={sorted(CURRENT_DOCS)}:actual={sorted(tracked_docs)}"
        )
    _validate_markdown_links(tracked_docs)
    for raw_path in CURRENT_DOCS:
        content = (ROOT / raw_path).read_text(encoding="utf-8")
        if (
            "Phase 7" not in content
            or PHASE7_FAILED_CANDIDATE not in content
            or PHASE7_REPLACEMENT_CANDIDATE not in content
        ):
            raise RuntimeError(f"current_documentation_phase_identity_missing:{raw_path}")
        authorization_marker = "standing authorization" if raw_path == "AGENTS.md" else "持续授权"
        if authorization_marker not in content:
            raise RuntimeError(f"current_documentation_provider_authorization_missing:{raw_path}")

    failed_p16_path = ROOT / PHASE7_FAILED_P16_RECEIPT
    if _sha256(failed_p16_path) != PHASE7_FAILED_P16_RECEIPT_SHA256:
        raise RuntimeError("phase7_failed_p16_receipt_hash_mismatch")
    failed_p16 = _load_json(failed_p16_path)
    if (
        failed_p16.get("schema") != "supportguard.interview_v2.ie_p16_receipt.v1"
        or failed_p16.get("candidate", {}).get("candidate_sha") != PHASE7_FAILED_CANDIDATE
        or failed_p16.get("candidate", {}).get("git_tree_sha") != PHASE7_FAILED_TREE
        or failed_p16.get("denominator") != 16
        or failed_p16.get("executed") != 16
        or failed_p16.get("passed") != 11
        or failed_p16.get("failed") != 5
        or failed_p16.get("claims", {}).get("safety_pass") is not True
        or failed_p16.get("claims", {}).get("semantic_pass") is not False
        or failed_p16.get("claims", {}).get("cleanup_pass") is not True
    ):
        raise RuntimeError("phase7_failed_p16_receipt_semantics_mismatch")

    replacement_p16_path = ROOT / PHASE7_REPLACEMENT_P16_RECEIPT
    replacement_hosted_path = ROOT / PHASE7_REPLACEMENT_HOSTED_RECEIPT
    replacement_validation_path = ROOT / PHASE7_REPLACEMENT_VALIDATION_RECEIPT
    if _sha256(replacement_p16_path) != PHASE7_REPLACEMENT_P16_RECEIPT_SHA256:
        raise RuntimeError("phase7_replacement_p16_receipt_hash_mismatch")
    if _sha256(replacement_hosted_path) != PHASE7_REPLACEMENT_HOSTED_RECEIPT_SHA256:
        raise RuntimeError("phase7_replacement_hosted_receipt_hash_mismatch")
    if _sha256(replacement_validation_path) != PHASE7_REPLACEMENT_VALIDATION_RECEIPT_SHA256:
        raise RuntimeError("phase7_replacement_validation_receipt_hash_mismatch")

    replacement_p16 = _load_json(replacement_p16_path)
    failed_results = [
        result for result in replacement_p16.get("results", []) if result.get("passed") is not True
    ]
    if (
        replacement_p16.get("schema") != "supportguard.interview_v2.ie_p16_receipt.v1"
        or replacement_p16.get("candidate", {}).get("candidate_sha") != PHASE7_REPLACEMENT_CANDIDATE
        or replacement_p16.get("candidate", {}).get("git_tree_sha") != PHASE7_REPLACEMENT_TREE
        or replacement_p16.get("denominator") != 16
        or replacement_p16.get("executed") != 16
        or replacement_p16.get("passed") != 13
        or replacement_p16.get("failed") != 3
        or replacement_p16.get("claims", {}).get("safety_pass") is not False
        or replacement_p16.get("claims", {}).get("semantic_pass") is not False
        or replacement_p16.get("claims", {}).get("cleanup_pass") is not True
        or [result.get("id") for result in failed_results] != ["IE-P14", "IE-P15", "IE-P16"]
        or any(
            result.get("failures") != ["scenario_execution_failed:ReadTimeout"]
            or result.get("provider_usage") != {"completion_tokens": 0, "prompt_tokens": 0}
            or result.get("cleanup", {}).get("clean") is not True
            for result in failed_results
        )
    ):
        raise RuntimeError("phase7_replacement_p16_receipt_semantics_mismatch")

    replacement_hosted = _load_json(replacement_hosted_path)
    if (
        replacement_hosted.get("candidate_sha") != PHASE7_REPLACEMENT_CANDIDATE
        or replacement_hosted.get("run_id") != 31664415941
        or replacement_hosted.get("classification") != "completed_success"
        or replacement_hosted.get("conclusion") != "success"
        or replacement_hosted.get("claims", {}).get("hosted_execution_started") is not True
        or replacement_hosted.get("claims", {}).get("local_execution_used_as_substitute")
        is not False
        or replacement_hosted.get("claims", {}).get("release_blocker") is not False
        or len(replacement_hosted.get("jobs", [])) != 5
        or sum(job.get("step_count", -1) for job in replacement_hosted.get("jobs", [])) != 76
        or any(job.get("conclusion") != "success" for job in replacement_hosted.get("jobs", []))
    ):
        raise RuntimeError("phase7_replacement_hosted_receipt_semantics_mismatch")

    replacement_validation = _load_json(replacement_validation_path)
    if (
        replacement_validation.get("contract_version")
        != "supportguard-interview-v2-phase7-replacement-validation.v1"
        or replacement_validation.get("candidate_sha") != PHASE7_REPLACEMENT_CANDIDATE
        or replacement_validation.get("candidate_tree_sha") != PHASE7_REPLACEMENT_TREE
        or replacement_validation.get("status") != "failed_at_confirmation_gate"
        or replacement_validation.get("claims", {}).get("phase7_complete") is not False
        or replacement_validation.get("claims", {}).get("hosted_ci_green") is not True
        or replacement_validation.get("claims", {}).get("ie_p16_consumed") is not True
        or replacement_validation.get("claims", {}).get("ie_p16_complete_matrix_pass") is not False
        or replacement_validation.get("claims", {}).get(
            "ie_p16_failed_scenario_provider_usage_observed"
        )
        is not False
        or replacement_validation.get("claims", {}).get("ie_p16_estimated_cost_observed_complete")
        is not False
        or replacement_validation.get("claims", {}).get("replacement_authorization_consumed")
        is not True
        or replacement_validation.get("claims", {}).get("replacement_confirmation_gate_active")
        is not False
        or replacement_validation.get("claims", {}).get(
            "subsequent_clean_candidate_deepseek_authorized"
        )
        is not True
        or replacement_validation.get("results", {}).get("ie_p16", {}).get("diagnostic_limitations")
        != [
            PHASE7_REPLACEMENT_USAGE_LIMITATION,
            PHASE7_REPLACEMENT_TIMEOUT_LIMITATION,
            PHASE7_REPLACEMENT_COST_LIMITATION,
        ]
        or replacement_validation.get("results", {})
        .get("ie_p16", {})
        .get("receipt_estimated_max_actual_cny")
        != "0.277541"
        or [
            scenario.get("id")
            for scenario in replacement_validation.get("results", {})
            .get("ie_p16", {})
            .get("failed_scenarios", [])
        ]
        != ["IE-P14", "IE-P15", "IE-P16"]
        or any(
            scenario.get("provider_usage_observed") is not False
            or scenario.get("receipt_provider_usage")
            != {"completion_tokens": 0, "prompt_tokens": 0}
            for scenario in replacement_validation.get("results", {})
            .get("ie_p16", {})
            .get("failed_scenarios", [])
        )
        or replacement_validation.get("results", {}).get("cleanup", {}).get("status") != "passed"
    ):
        raise RuntimeError("phase7_replacement_validation_receipt_semantics_mismatch")

    phase5_path = ROOT / PHASE5_RECEIPT
    phase5_hosted_path = ROOT / PHASE5_HOSTED_RECEIPT
    if _sha256(phase5_path) != PHASE5_RECEIPT_SHA256:
        raise RuntimeError("phase5_validation_receipt_hash_mismatch")
    if _sha256(phase5_hosted_path) != PHASE5_HOSTED_RECEIPT_SHA256:
        raise RuntimeError("phase5_hosted_receipt_hash_mismatch")
    phase5 = _load_json(phase5_path)
    hosted = _load_json(phase5_hosted_path)
    if (
        phase5.get("candidate_sha") != PHASE5_CANDIDATE
        or phase5.get("candidate_tree_sha") != PHASE5_TREE
        or phase5.get("claims", {}).get("phase5_complete") is not True
        or phase5.get("claims", {}).get("phase7_complete") is not False
    ):
        raise RuntimeError("phase5_validation_receipt_semantics_mismatch")
    if (
        hosted.get("candidate_sha") != PHASE5_CANDIDATE
        or hosted.get("classification") != "external_zero_step_blocker"
        or hosted.get("claims", {}).get("hosted_execution_started") is not False
        or hosted.get("claims", {}).get("local_execution_used_as_substitute") is not False
        or hosted.get("claims", {}).get("release_blocker") is not True
    ):
        raise RuntimeError("phase5_hosted_receipt_semantics_mismatch")

    phase6_path = ROOT / PHASE6_RECEIPT
    phase6_hosted_path = ROOT / PHASE6_HOSTED_RECEIPT
    if _sha256(phase6_path) != PHASE6_RECEIPT_SHA256:
        raise RuntimeError("phase6_validation_receipt_hash_mismatch")
    if _sha256(phase6_hosted_path) != PHASE6_HOSTED_RECEIPT_SHA256:
        raise RuntimeError("phase6_hosted_receipt_hash_mismatch")
    phase6 = _load_json(phase6_path)
    phase6_hosted = _load_json(phase6_hosted_path)
    if (
        phase6.get("candidate_sha") != PHASE6_CANDIDATE
        or phase6.get("candidate_tree_sha") != PHASE6_TREE
        or phase6.get("claims", {}).get("phase6_complete") is not True
        or phase6.get("claims", {}).get("phase7_complete") is not False
        or phase6.get("claims", {}).get("phase7_execution_blocked") is not True
    ):
        raise RuntimeError("phase6_validation_receipt_semantics_mismatch")
    if (
        phase6_hosted.get("candidate_sha") != PHASE6_CANDIDATE
        or phase6_hosted.get("classification") != "external_zero_step_blocker"
        or phase6_hosted.get("claims", {}).get("hosted_execution_started") is not False
        or phase6_hosted.get("claims", {}).get("local_execution_used_as_substitute") is not False
        or phase6_hosted.get("claims", {}).get("release_blocker") is not True
        or sum(job.get("step_count", -1) for job in phase6_hosted.get("jobs", [])) != 0
    ):
        raise RuntimeError("phase6_hosted_receipt_semantics_mismatch")

    manifest_path = ROOT / PHASE6_MANIFEST
    manifest = _load_json(manifest_path)
    archive_contract = runpy.run_path(str(ROOT / "scripts/phase6_archive_transition.py"))
    verification = archive_contract["verify_manifest"](manifest, require_absent=True)
    if verification.get("result") != "pass":
        raise RuntimeError("phase6_archive_transition_invalid")

    disposition = _load_json(ROOT / TEST_DISPOSITION)
    if (
        disposition.get("status") != "phase6_completed"
        or not disposition.get("groups")
        or len(disposition.get("safety_keep_mappings", [])) != 14
    ):
        raise RuntimeError("phase6_test_disposition_invalid")

    evaluation = recompute_evaluation_status(ROOT)
    if (
        evaluation.get("active_dataset") is not None
        or evaluation.get("execution_allowed") is not False
        or evaluation.get("protected_holdout") != "not_accessed"
        or evaluation.get("cross_encoder") != "not_executed"
    ):
        raise RuntimeError("phase7_inputs_were_consumed_early")

    return {
        "result": "pass",
        "v20_activation": "phase7",
        "current_authority_document_count": len(tracked_docs),
        "current_database_head": CURRENT_PRODUCT_DATABASE_HEAD,
        "v20_phase5_candidate_sha": PHASE5_CANDIDATE,
        "v20_phase5_tree_sha": PHASE5_TREE,
        "v20_phase5_receipt_sha256": PHASE5_RECEIPT_SHA256,
        "v20_phase5_hosted_receipt_sha256": PHASE5_HOSTED_RECEIPT_SHA256,
        "v20_phase5_hosted_disposition": hosted["classification"],
        "phase6_archive_manifest_sha256": _sha256(manifest_path),
        "phase6_archive_file_count": manifest["summary"]["file_count"],
        "phase6_archive_source_commit": manifest["source"]["commit"],
        "phase6_test_disposition_status": disposition["status"],
        "v20_phase6_candidate_sha": PHASE6_CANDIDATE,
        "v20_phase6_tree_sha": PHASE6_TREE,
        "v20_phase6_receipt_sha256": PHASE6_RECEIPT_SHA256,
        "v20_phase6_hosted_receipt_sha256": PHASE6_HOSTED_RECEIPT_SHA256,
        "v20_phase6_hosted_disposition": phase6_hosted["classification"],
        "phase7_failed_candidate_sha": PHASE7_FAILED_CANDIDATE,
        "phase7_failed_tree_sha": PHASE7_FAILED_TREE,
        "phase7_failed_p16_receipt_sha256": PHASE7_FAILED_P16_RECEIPT_SHA256,
        "phase7_failed_p16_result": "11/16",
        "phase7_replacement_candidate_sha": PHASE7_REPLACEMENT_CANDIDATE,
        "phase7_replacement_tree_sha": PHASE7_REPLACEMENT_TREE,
        "phase7_replacement_p16_receipt_sha256": PHASE7_REPLACEMENT_P16_RECEIPT_SHA256,
        "phase7_replacement_p16_result": "13/16",
        "phase7_replacement_hosted_receipt_sha256": (PHASE7_REPLACEMENT_HOSTED_RECEIPT_SHA256),
        "phase7_replacement_validation_receipt_sha256": (
            PHASE7_REPLACEMENT_VALIDATION_RECEIPT_SHA256
        ),
        "phase7_replacement_authorization_consumed": True,
        "phase7_confirmation_gate": False,
        "phase7_subsequent_clean_candidate_deepseek_authorized": True,
        "active_dataset": evaluation["active_dataset"],
        "protected_holdout": evaluation["protected_holdout"],
        "cross_encoder": evaluation["cross_encoder"],
    }


def main() -> None:
    print(json.dumps(validate(), sort_keys=True))


if __name__ == "__main__":
    main()
