"""Validate the history-free public Interview Edition mirror boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any, cast

SOURCE_REPOSITORY = "Teddy-creator/SupportGuard"
SOURCE_COMMIT = "a528a19d1b00c5699af9f5a87f12bd515c1a834d"
SOURCE_TREE = "ba50b72e014e85fb60c85ad9eb8ae253077ac817"
PUBLIC_REPOSITORY = "Teddy-creator/SupportGuard-Interview"
PROVENANCE_PATH = Path("public-mirror-provenance.v1.json")

BOUND_CONTRACTS = {
    "phase6_archive_manifest_sha256": (
        "validation/evidence/interview_v2/phase6/archive-transition-manifest.v1.json",
        "7a62d7c3141d8a6c1bfc6460393d0329b285061dbcb2f229a2ecbaa6d7645f7f",
    ),
    "test_disposition_sha256": (
        "validation/contracts/interview_v2/test-disposition.v1.json",
        "6a46495d35427a09019b04b0e076fa796b45bf3a1922a155837abc29a0acf0dc",
    ),
    "behavior_characterization_sha256": (
        "validation/contracts/interview_v2/behavior-characterization.v1.json",
        "e8c11dd05d82b1ebdf6ba627261aec612411752ea4eee15208de7b6385eabcda",
    ),
    "safety_invariant_manifest_sha256": (
        "validation/contracts/interview_v2/safety-invariant-manifest.v1.json",
        "e562b55f5b614016a86dc0ce24354595c17e7163e0f0c7e5c39a37c28dbfda6a",
    ),
}


class PublicMirrorContractError(RuntimeError):
    """Raised when the public mirror boundary is incomplete or inconsistent."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicMirrorContractError(message)


def load_public_mirror_provenance(root: Path) -> dict[str, Any] | None:
    """Load and verify the public marker, or return ``None`` in the private source repo."""

    path = root / PROVENANCE_PATH
    if not path.is_file():
        return None
    payload = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    _require(
        payload.get("schema_version") == "supportguard.public_mirror_provenance.v1",
        "public mirror provenance schema mismatch",
    )
    _require(
        payload.get("status") == "history_free_public_interview_snapshot",
        "public mirror provenance status mismatch",
    )
    source = payload.get("source", {})
    _require(source.get("repository") == SOURCE_REPOSITORY, "source repository mismatch")
    _require(source.get("visibility") == "private", "source visibility mismatch")
    _require(source.get("commit") == SOURCE_COMMIT, "source commit mismatch")
    _require(source.get("tree") == SOURCE_TREE, "source tree mismatch")
    _require(source.get("exported_tracked_file_count") == 556, "source file count mismatch")
    _require(source.get("export_method") == "git archive HEAD", "export method mismatch")

    mirror = payload.get("public_mirror", {})
    _require(mirror.get("repository") == PUBLIC_REPOSITORY, "public repository mismatch")
    _require(mirror.get("license") == "MIT", "public mirror license mismatch")
    for field in (
        "git_history_included",
        "git_tags_included",
        "actions_history_included",
        "actions_artifacts_included",
        "archive_restore_is_locally_verifiable",
    ):
        _require(mirror.get(field) is False, f"public mirror boundary changed: {field}")

    recorded = payload.get("bound_private_contracts", {})
    for field, (relative_path, expected_hash) in BOUND_CONTRACTS.items():
        _require(recorded.get(field) == expected_hash, f"recorded contract hash changed: {field}")
        _require(
            _sha256(root / relative_path) == expected_hash,
            f"public snapshot contract bytes changed: {relative_path}",
        )
    _require(
        recorded.get("archive_tag") == "archive/interview-v2.0-baseline",
        "archive tag identity mismatch",
    )
    _require(
        recorded.get("archive_tag_object") == "d274ca18abe7c9c4c324a2d6caa7bbec0622f9b9",
        "archive tag object mismatch",
    )
    _require(
        recorded.get("archive_baseline_commit") == "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb",
        "archive baseline identity mismatch",
    )
    _require(
        recorded.get("phase6_archive_source_commit") == "328bc8606fdfbe50c9f3530646e72c1c21269c12",
        "Phase 6 archive source identity mismatch",
    )

    claims = payload.get("claims", {})
    for field in (
        "private_canonical_repository_modified",
        "private_history_published",
        "historical_results_rewritten",
        "protected_evaluation_accessed",
        "phase7_completed",
        "final_definition_of_done_completed",
    ):
        _require(claims.get(field) is False, f"public mirror claim changed: {field}")
    return payload


def validate_public_git_boundary(root: Path) -> dict[str, Any]:
    """Verify that the committed public repository contains no private Git reachability."""

    payload = load_public_mirror_provenance(root)
    _require(payload is not None, "public mirror provenance is missing")
    git = shutil.which("git")
    if git is None:
        raise PublicMirrorContractError("git executable unavailable")

    tags = subprocess.run(  # nosec B603  # noqa: S603 - fixed Git executable and arguments
        [git, "tag", "--list"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    _require(tags == [], "public mirror unexpectedly contains Git tags")
    source_object = subprocess.run(  # nosec B603  # noqa: S603 - fixed Git executable and arguments
        [git, "cat-file", "-e", f"{SOURCE_COMMIT}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    _require(source_object.returncode != 0, "private source commit is reachable in public mirror")
    authors = subprocess.run(  # nosec B603  # noqa: S603 - fixed Git executable and arguments
        [git, "log", "--format=%ae"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    _require(
        bool(authors) and all(email.endswith("@users.noreply.github.com") for email in authors),
        "public mirror commit email is not a GitHub noreply address",
    )
    tracked = subprocess.run(  # nosec B603  # noqa: S603 - fixed Git executable and arguments
        [git, "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    tracked_paths = [Path(item.decode("utf-8")) for item in tracked if item]
    private_workstation_prefix = b"/Users" + b"/cloud"
    leaked_paths = [
        str(path)
        for path in tracked_paths
        if private_workstation_prefix in (root / path).read_bytes()
    ]
    _require(not leaked_paths, f"private workstation path remains: {leaked_paths[:3]!r}")
    _require(not (root / ".env").exists(), "local .env must not be published")
    _require(
        (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n"),
        "MIT License is missing",
    )
    return {
        "result": "pass",
        "source_commit": SOURCE_COMMIT,
        "source_tree": SOURCE_TREE,
        "public_repository": PUBLIC_REPOSITORY,
        "public_commit_count": len(authors),
        "private_source_reachable": False,
        "tag_count": 0,
        "tracked_file_count": len(tracked_paths),
    }
