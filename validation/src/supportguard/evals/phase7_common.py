from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Phase7ContractError(RuntimeError):
    """A Phase 7 run cannot be attributed to the requested Candidate."""


_GIT = shutil.which("git")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256_bytes(payload)


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    if _GIT is None:
        raise Phase7ContractError("git_executable_unavailable")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [_GIT, *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        },
    )
    if check and completed.returncode:
        raise Phase7ContractError(f"git_command_failed:{arguments[0]}")
    return completed.stdout.strip()


def _tracked_source_state(root: Path) -> tuple[str, int]:
    paths = [item for item in _git(root, "ls-files", "-z").split("\0") if item]
    records: list[dict[str, str]] = []
    for relative in sorted(paths):
        path = root / relative
        if not path.is_file():
            raise Phase7ContractError(f"tracked_source_unavailable:{relative}")
        records.append({"path": relative, "sha256": sha256_file(path)})
    return canonical_sha256(records), len(records)


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    candidate_sha: str
    git_tree_sha: str
    origin_main_sha: str
    branch: str
    source_state_sha256: str
    source_file_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_sha": self.candidate_sha,
            "git_tree_sha": self.git_tree_sha,
            "origin_main_sha": self.origin_main_sha,
            "branch": self.branch,
            "worktree_clean": True,
            "head_equals_origin_main": True,
            "source_state_sha256": self.source_state_sha256,
            "source_file_count": self.source_file_count,
        }


def require_candidate(root: Path, expected_sha: str) -> CandidateIdentity:
    root = root.resolve()
    if len(expected_sha) != 40 or any(
        character not in "0123456789abcdef" for character in expected_sha
    ):
        raise Phase7ContractError("candidate_sha_must_be_full_lowercase_git_sha")
    head = _git(root, "rev-parse", "HEAD")
    origin = _git(root, "rev-parse", "origin/main")
    branch = _git(root, "branch", "--show-current")
    if head != expected_sha:
        raise Phase7ContractError("candidate_head_mismatch")
    if origin != expected_sha:
        raise Phase7ContractError("candidate_origin_main_mismatch")
    if branch != "main":
        raise Phase7ContractError("candidate_branch_mismatch")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise Phase7ContractError("candidate_worktree_not_clean")
    source_state_sha256, source_file_count = _tracked_source_state(root)
    return CandidateIdentity(
        candidate_sha=head,
        git_tree_sha=_git(root, "rev-parse", "HEAD^{tree}"),
        origin_main_sha=origin,
        branch=branch,
        source_state_sha256=source_state_sha256,
        source_file_count=source_file_count,
    )


def require_ignored_output(root: Path, output: Path) -> Path:
    if _GIT is None:
        raise Phase7ContractError("git_executable_unavailable")
    output = output.resolve()
    try:
        relative = output.relative_to(root.resolve())
    except ValueError as exc:
        raise Phase7ContractError("phase7_output_must_be_inside_repository") from exc
    ignored = subprocess.run(  # noqa: S603  # nosec B603
        [_GIT, "check-ignore", "--quiet", "--", str(relative)],
        cwd=root,
        check=False,
        timeout=30,
    ).returncode
    if ignored:
        raise Phase7ContractError("phase7_output_must_be_gitignored")
    return output


def atomic_write_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_bytes(payload)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
