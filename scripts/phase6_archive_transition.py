#!/usr/bin/env python3
"""Build and verify the Interview v2 Phase 6 archive transition manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from supportguard.validation.public_mirror import load_public_mirror_provenance

ROOT: Final = Path(__file__).resolve().parents[1]
GIT: Final = shutil.which("git")
SOURCE_COMMIT: Final = "328bc8606fdfbe50c9f3530646e72c1c21269c12"
SOURCE_TREE: Final = "054d61844a9acf7a958ddc8ad3800f9aceed5cea"
ARCHIVE_TAG: Final = "archive/interview-v2.0-baseline"
ARCHIVE_TAG_OBJECT: Final = "d274ca18abe7c9c4c324a2d6caa7bbec0622f9b9"
ARCHIVE_BASELINE_COMMIT: Final = "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb"
PHASE0_ARCHIVE_MANIFEST: Final = "validation/evidence/interview_v2/phase0/archive-manifest.v1.json"
PHASE0_ARCHIVE_MANIFEST_SHA256: Final = (
    "6f620e39bbbc42f03744f603e8bb0dd7e6026bbbfbef2aab774544adcceda07f"
)
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
CURRENT_DOCS_UNDER_DOCS: Final = CURRENT_DOCS - {"README.md", "AGENTS.md"}
HISTORICAL_RUNTIME_PATHS: Final = frozenset(
    {
        "backend/src/supportguard/db/upgrade_v125.py",
        "backend/src/supportguard/db/upgrade_v126.py",
    }
)
HISTORICAL_SCRIPT_PATHS: Final = frozenset(
    {
        "scripts/adjudicate_v1510_formal_receipt.py",
        "scripts/annotate_eval_intents.py",
        "scripts/build_corrective_closure.py",
        "scripts/build_v124_closure.py",
        "scripts/build_v124_phase0_manifest.py",
        "scripts/build_v125_phase0_manifest.py",
        "scripts/collect_v129_regression.py",
        "scripts/freeze_rag_golds.py",
        "scripts/freeze_v2_dataset.py",
        "scripts/freeze_v3_dev.py",
        "scripts/phase2_recovery.py",
        "scripts/phase3_provenance.py",
        "scripts/run_corrective_gate.py",
        "scripts/run_local_ci_parity.py",
        "scripts/run_phase3_baseline_schema.py",
        "scripts/run_phase4_escalation_retirement.py",
        "scripts/run_real_provider_v151_matrix.py",
        "scripts/run_real_provider_v153_acceptance.py",
        "scripts/run_real_provider_v155_acceptance.py",
        "scripts/run_real_provider_v159_shadow.py",
        "scripts/run_real_user_journeys_v1512.py",
        "scripts/run_v1218_identity_diagnostics.py",
        "scripts/run_v125_gate.py",
        "scripts/run_v126_gate.py",
        "scripts/run_v129_regression.py",
        "scripts/run_v13_interview_stability.py",
        "scripts/run_v1512_journey_preflight.py",
        "scripts/run_v1515_mcp_diagnostic.py",
        "scripts/run_v1515_provider_diagnostic.py",
        "scripts/run_v1518_dual_worker_diagnostic.py",
        "scripts/v1512_journey_browser_probe.mjs",
        "scripts/validate_corrective_evidence.py",
        "scripts/validate_v124_evidence.py",
        "scripts/validate_v125_evidence.py",
        "scripts/validate_v126_evidence.py",
        "scripts/v159_runtime_identity.py",
        "scripts/verify_v1512_candidate_runtime.py",
    }
)
HISTORICAL_TEST_PATHS: Final = frozenset(
    {
        "backend/tests/test_corrective_evidence_validator.py",
        "backend/tests/test_corrective_v123_phase0.py",
        "backend/tests/test_corrective_v124_phase0.py",
        "backend/tests/test_corrective_v125_phase0.py",
        "backend/tests/test_interview_v2_migration_driver_sql.py",
        "backend/tests/test_phase2_recovery_evidence.py",
        "backend/tests/test_phase3_baseline_provenance.py",
        "backend/tests/test_phase3_delivery_contract.py",
        "backend/tests/test_phase3_schema_equivalence.py",
        "backend/tests/test_rag_eligibility_contract.py",
        "backend/tests/test_v1212_predicate_facts.py",
        "backend/tests/test_v1213_activation_contract.py",
        "backend/tests/test_v1213_gate_ledger.py",
        "backend/tests/test_v1213_migration_reference.py",
        "backend/tests/test_v1214_local_ci_parity.py",
        "backend/tests/test_v1214_parity_receipt.py",
        "backend/tests/test_v1216_shadow_receipt.py",
        "backend/tests/test_v1217_post_evidence_acceptance.py",
        "backend/tests/test_v1218_identity_bound_convergence_contract.py",
        "backend/tests/test_v1218_identity_convergence_diagnostics.py",
        "backend/tests/test_v1219_worker_fault_convergence_contract.py",
        "backend/tests/test_v1220_worker_non_reproduction_resolution.py",
        "backend/tests/test_v1221_controlled_fault_embedding_contract.py",
        "backend/tests/test_v1222_parity_invocation_preflight_contract.py",
        "backend/tests/test_v124_evidence_validator.py",
        "backend/tests/test_v124_mcp_fence_contract.py",
        "backend/tests/test_v125_evidence_runner.py",
        "backend/tests/test_v125_evidence_validator.py",
        "backend/tests/test_v125_phase3_contract.py",
        "backend/tests/test_v126_collectors.py",
        "backend/tests/test_v126_contract_manifest.py",
        "backend/tests/test_v126_evidence_contract.py",
        "backend/tests/test_v126_gate_contract.py",
        "backend/tests/test_v126_phase1_runtime.py",
        "backend/tests/test_v126_reference_contract.py",
        "backend/tests/test_v129_acceptance_http.py",
        "backend/tests/test_v129_acceptance_postgres.py",
        "backend/tests/test_v129_entrypoint_contract.py",
        "backend/tests/test_v129_legacy_migration.py",
        "backend/tests/test_v129_mcp_partition_teardown.py",
        "backend/tests/test_v129_postgres_contract.py",
        "backend/tests/test_v129_process_teardown.py",
        "backend/tests/test_v129_timestamps.py",
        "backend/tests/test_v13_failure_snapshot_contract.py",
        "backend/tests/test_v13_interview_release_closure_contract.py",
        "backend/tests/test_v1510_phase0_contract.py",
        "backend/tests/test_v1510_targeted_carrier.py",
        "backend/tests/test_v1511_formal_adjudication.py",
        "backend/tests/test_v1512_action_source_capabilities.py",
        "backend/tests/test_v1512_b192_contract_closure.py",
        "backend/tests/test_v1512_b194_forward_runtime_semantics.py",
        "backend/tests/test_v1512_b195_exact_action_replay.py",
        "backend/tests/test_v1512_b198_action_result_projection.py",
        "backend/tests/test_v1512_customer_message_run_binding.py",
        "backend/tests/test_v1512_journey_acceptance_carrier.py",
        "backend/tests/test_v1512_journey_adapter.py",
        "backend/tests/test_v1512_journey_browser_contract.py",
        "backend/tests/test_v1512_journey_provider_context_contract.py",
        "backend/tests/test_v1512_journey_semantic_contract.py",
        "backend/tests/test_v1512_journey_preconditions_postgres.py",
        "backend/tests/test_v1512_migration_reentry.py",
        "backend/tests/test_v1512_pending_status_projection.py",
        "backend/tests/test_v1512_phase1_schema.py",
        "backend/tests/test_v1512_post_contract_migration_reentry.py",
        "backend/tests/test_v1512_preflight_contract.py",
        "backend/tests/test_v1512_product_inspection_capabilities.py",
        "backend/tests/test_v1512_public_event_identity.py",
        "backend/tests/test_v1512_public_timeline_identity.py",
        "backend/tests/test_v1512_reader_rollout_postgres.py",
        "backend/tests/test_v1512_upgrade_jobless_reject.py",
        "backend/tests/test_v1512_upgrade_jobless_reject_postgres.py",
        "backend/tests/test_v1514_journey_failure_accounting.py",
        "backend/tests/test_v1515_mcp_diagnostic.py",
        "backend/tests/test_v1515_provider_public_diagnostic.py",
        "backend/tests/test_v1516_mcp_resource_diagnostic.py",
        "backend/tests/test_v1517_provider_schema_contract.py",
        "backend/tests/test_v1518_dual_worker_contract.py",
        "backend/tests/test_v1518_provider_accounting.py",
        "backend/tests/test_v1518_runtime_failure_evidence.py",
        "backend/tests/test_v151_real_provider_acceptance_carrier.py",
        "backend/tests/test_v153_real_provider_acceptance_carrier.py",
        "backend/tests/test_v155_operations_ci_contract.py",
        "backend/tests/test_v155_real_provider_acceptance_contract.py",
        "backend/tests/test_v159_phase0_contract.py",
        "backend/tests/test_v159_shadow_contract.py",
        "backend/tests/test_v16_structural_characterization.py",
        "backend/tests/test_worker_convergence_diagnostics.py",
    }
)


class ArchiveTransitionError(RuntimeError):
    """Raised when Phase 6 archive identity or recoverability is invalid."""


@dataclass(frozen=True)
class GitEntry:
    mode: str
    object_id: str
    size: int
    path: str


def _git(*args: str, text: bool = True) -> str | bytes:
    if GIT is None:
        raise ArchiveTransitionError("git executable unavailable")
    result = subprocess.run(  # noqa: S603 - fixed executable and internal arguments only
        [GIT, *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=text,
    )
    return cast(str | bytes, result.stdout)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tracked_entries(commit: str) -> tuple[GitEntry, ...]:
    raw = _git("ls-tree", "-r", "-z", "--long", commit, text=False)
    assert isinstance(raw, bytes)
    entries: list[GitEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_raw = record.split(b"\t", 1)
        mode, object_type, object_id, size_raw = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        entries.append(
            GitEntry(
                mode=mode,
                object_id=object_id,
                size=int(size_raw),
                path=path_raw.decode("utf-8"),
            )
        )
    return tuple(entries)


def _category(path: str) -> str | None:
    if path == "alembic.ini" or path.startswith("backend/alembic/"):
        return "legacy_migration_chain"
    if path.startswith("evals/reports/"):
        return "historical_evaluation_reports"
    if path.startswith("evals/"):
        return "historical_evaluation_inputs"
    if path.startswith("docs/") and path not in CURRENT_DOCS_UNDER_DOCS:
        return "historical_documentation"
    if path in HISTORICAL_RUNTIME_PATHS:
        return "historical_runtime_upgrade_owners"
    if path in HISTORICAL_SCRIPT_PATHS:
        return "historical_validation_scripts"
    if path in HISTORICAL_TEST_PATHS or (
        path.startswith("backend/tests/test_eval_") and path.endswith(".py")
    ):
        return "historical_test_carriers"
    if path.startswith("validation/src/supportguard/acceptance/") or path.startswith(
        "validation/src/supportguard/diagnostics/"
    ):
        return "historical_validation_packages"
    if path.startswith("validation/src/supportguard/evals/") and path not in {
        "validation/src/supportguard/evals/__init__.py",
        "validation/src/supportguard/evals/gate.py",
    }:
        return "historical_validation_packages"
    if path.startswith("validation/src/supportguard/evidence/") and (
        Path(path).name == "corrective.py" or Path(path).name.startswith("v")
    ):
        return "historical_validation_packages"
    if path in {
        "validation/src/supportguard/validation/schema_equivalence.py",
        "validation/src/supportguard/validation/schema_equivalence_runner.py",
    }:
        return "historical_validation_packages"
    return None


def _selected(entries: Iterable[GitEntry]) -> tuple[tuple[str, GitEntry], ...]:
    selected = [
        (category, entry) for entry in entries if (category := _category(entry.path)) is not None
    ]
    return tuple(sorted(selected, key=lambda item: (item[0], item[1].path)))


def _blob_payloads(entries: Sequence[GitEntry]) -> dict[str, bytes]:
    if GIT is None:
        raise ArchiveTransitionError("git executable unavailable")
    process = subprocess.Popen(  # noqa: S603 - fixed Git subcommand
        [GIT, "cat-file", "--batch"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        raise ArchiveTransitionError("git cat-file batch pipes unavailable")
    payloads: dict[str, bytes] = {}
    try:
        for entry in entries:
            process.stdin.write(f"{entry.object_id}\n".encode("ascii"))
        process.stdin.flush()
        for entry in entries:
            header = process.stdout.readline().decode("ascii").strip().split()
            if len(header) != 3 or header[1] != "blob":
                raise ArchiveTransitionError(
                    f"unexpected git object header for {entry.path}: {header!r}"
                )
            size = int(header[2])
            payload = process.stdout.read(size)
            separator = process.stdout.read(1)
            if len(payload) != size or separator != b"\n":
                raise ArchiveTransitionError(f"truncated git object for {entry.path}")
            payloads[entry.object_id] = payload
    finally:
        process.stdin.close()
        process.wait(timeout=30)
    if process.returncode != 0:
        raise ArchiveTransitionError(f"git cat-file exited {process.returncode}")
    return payloads


def build_manifest() -> dict[str, object]:
    if load_public_mirror_provenance(ROOT) is not None:
        manifest_path = (
            ROOT / "validation/evidence/interview_v2/phase6/archive-transition-manifest.v1.json"
        )
        return cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
    source_tree = str(_git("rev-parse", f"{SOURCE_COMMIT}^{{tree}}")).strip()
    if source_tree != SOURCE_TREE:
        raise ArchiveTransitionError("Phase 5 source tree identity changed")
    tag_object = str(_git("rev-parse", f"{ARCHIVE_TAG}^{{tag}}")).strip()
    tag_commit = str(_git("rev-parse", f"{ARCHIVE_TAG}^{{commit}}")).strip()
    if tag_object != ARCHIVE_TAG_OBJECT or tag_commit != ARCHIVE_BASELINE_COMMIT:
        raise ArchiveTransitionError("annotated Archive tag identity changed")
    phase0_manifest = (ROOT / PHASE0_ARCHIVE_MANIFEST).read_bytes()
    if _sha256(phase0_manifest) != PHASE0_ARCHIVE_MANIFEST_SHA256:
        raise ArchiveTransitionError("Phase 0 Archive manifest changed")

    selected = _selected(_tracked_entries(SOURCE_COMMIT))
    payloads = _blob_payloads([entry for _, entry in selected])
    records = [
        {
            "category": category,
            "path": entry.path,
            "git_blob": entry.object_id,
            "git_mode": entry.mode,
            "bytes": entry.size,
            "sha256": _sha256(payloads[entry.object_id]),
        }
        for category, entry in selected
    ]
    categories: dict[str, dict[str, int]] = {}
    for record in records:
        category = str(record["category"])
        summary = categories.setdefault(category, {"file_count": 0, "bytes": 0})
        summary["file_count"] += 1
        summary["bytes"] += cast(int, record["bytes"])

    return {
        "schema_version": "supportguard.interview_v2.phase6_archive_transition.v1",
        "status": "ready_for_controlled_pruning",
        "source": {
            "commit": SOURCE_COMMIT,
            "tree": SOURCE_TREE,
            "source_is_phase5_evidence_head": True,
        },
        "archive": {
            "annotated_tag": ARCHIVE_TAG,
            "tag_object": ARCHIVE_TAG_OBJECT,
            "baseline_commit": ARCHIVE_BASELINE_COMMIT,
            "phase0_manifest_path": PHASE0_ARCHIVE_MANIFEST,
            "phase0_manifest_sha256": PHASE0_ARCHIVE_MANIFEST_SHA256,
        },
        "current_authority_documents": sorted(CURRENT_DOCS),
        "selection_contract": {
            "historical_documentation": "docs/** except the six current docs",
            "legacy_migration_chain": "alembic.ini and backend/alembic/**",
            "historical_evaluation_reports": "evals/reports/**",
            "historical_evaluation_inputs": "remaining evals/**",
            "historical_runtime_upgrade_owners": "v1.2.5/v1.2.6 upgrade bridge owners",
            "historical_validation_packages": (
                "pre-v2 acceptance, diagnostics, eval and evidence tooling"
            ),
            "historical_validation_scripts": "pre-v2 Gate, parity, diagnostic and receipt runners",
            "historical_test_carriers": (
                "tests whose only owner is an archived migration, report, Gate, parity "
                "or historical acceptance surface"
            ),
        },
        "summary": {
            "file_count": len(records),
            "bytes": sum(cast(int, record["bytes"]) for record in records),
            "categories": categories,
        },
        "files": records,
        "claims": {
            "historical_results_rewritten": False,
            "git_history_deleted": False,
            "restore_source_is_git_reachable": True,
            "protected_evaluation_accessed": False,
        },
    }


def verify_manifest(manifest: dict[str, object], *, require_absent: bool) -> dict[str, object]:
    expected = build_manifest()
    if manifest != expected:
        raise ArchiveTransitionError("archive transition manifest is not canonical")
    public_provenance = load_public_mirror_provenance(ROOT)
    files = manifest["files"]
    assert isinstance(files, list)
    present = [record["path"] for record in files if (ROOT / str(record["path"])).exists()]
    if require_absent and present:
        raise ArchiveTransitionError(f"archived paths remain in workspace: {present[:5]!r}")
    if public_provenance is not None:
        return {
            "result": "pass",
            "source_commit": SOURCE_COMMIT,
            "file_count": len(files),
            "current_paths_absent": not present,
            "archive_tag_verified": False,
            "source_reachable": False,
            "verification_source": "private_canonical_receipt_bound_by_public_provenance",
        }
    if GIT is None:
        raise ArchiveTransitionError("git executable unavailable")
    ancestor = subprocess.run(  # noqa: S603 - fixed Git subcommand and identities
        [GIT, "merge-base", "--is-ancestor", SOURCE_COMMIT, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ArchiveTransitionError("archive source commit is not reachable from HEAD")
    return {
        "result": "pass",
        "source_commit": SOURCE_COMMIT,
        "file_count": len(files),
        "current_paths_absent": not present,
        "archive_tag_verified": True,
        "source_reachable": True,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-absent", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        _write_json(args.output, build_manifest())
        return 0
    manifest = json.loads(args.output.read_text(encoding="utf-8"))
    print(json.dumps(verify_manifest(manifest, require_absent=args.require_absent), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
