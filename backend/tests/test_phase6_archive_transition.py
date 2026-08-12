from __future__ import annotations

import json
import runpy
from pathlib import Path

MANIFEST = Path(
    "validation/evidence/interview_v2/phase6/archive-transition-manifest.v1.json"
)
CONTRACT = runpy.run_path("scripts/phase6_archive_transition.py")


def test_phase6_archive_transition_is_exact_and_recoverable() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest == CONTRACT["build_manifest"]()
    assert manifest["source"]["commit"] == CONTRACT["SOURCE_COMMIT"]
    assert manifest["archive"]["baseline_commit"] == CONTRACT["ARCHIVE_BASELINE_COMMIT"]
    assert set(manifest["current_authority_documents"]) == CONTRACT["CURRENT_DOCS"]
    assert manifest["summary"]["file_count"] == 2197
    assert manifest["summary"]["bytes"] == 20264669
    assert manifest["summary"]["categories"] == {
        "historical_documentation": {"bytes": 3422477, "file_count": 175},
        "historical_evaluation_inputs": {"bytes": 1216996, "file_count": 70},
        "historical_evaluation_reports": {"bytes": 10208907, "file_count": 1552},
        "historical_runtime_upgrade_owners": {"bytes": 62900, "file_count": 2},
        "historical_test_carriers": {"bytes": 1458864, "file_count": 98},
        "historical_validation_packages": {"bytes": 1463381, "file_count": 71},
        "historical_validation_scripts": {"bytes": 716499, "file_count": 37},
        "legacy_migration_chain": {"bytes": 1714645, "file_count": 192},
    }
    assert manifest["claims"] == {
        "git_history_deleted": False,
        "historical_results_rewritten": False,
        "protected_evaluation_accessed": False,
        "restore_source_is_git_reachable": True,
    }


def test_phase6_archive_manifest_paths_are_absent_after_pruning() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    present = [record["path"] for record in manifest["files"] if Path(record["path"]).exists()]

    assert present == []
