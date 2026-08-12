"""Build and verify the immutable pre-v2.0 repository archive manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
GIT_BIN = shutil.which("git") or "/usr/bin/git"
ARCHIVE_TAG = "archive/interview-v2.0-baseline"
BASELINE_COMMIT = "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb"
FORBIDDEN_V6_PREFIXES = ("evals/v6/holdout", "evals/v6/private")


class ArchiveContractError(RuntimeError):
    """Raised when the archive cannot satisfy the frozen v2.0 contract."""


def _git(*args: str, cwd: Path = ROOT, text: bool = True) -> str | bytes:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [GIT_BIN, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=text,
    )
    return cast(str | bytes, completed.stdout)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw in {"", "."}:
        raise ArchiveContractError(f"unsafe archive path: {raw!r}")
    if raw.startswith(FORBIDDEN_V6_PREFIXES):
        raise ArchiveContractError(f"protected Evaluation v6 path is forbidden: {raw}")
    return path


def _coverage(path: str) -> tuple[str, ...]:
    lowered = path.lower()
    categories: list[str] = []
    if path.startswith("backend/alembic/versions/"):
        categories.append("migration")
    if "receipt" in lowered:
        categories.append("receipt")
    if "matrix" in lowered:
        categories.append("matrix")
    if "manifest" in lowered:
        categories.append("manifest")
    if "/prompts/" in f"/{lowered}" or "prompt" in PurePosixPath(lowered).name:
        categories.append("prompt")
    if path.startswith("knowledge/"):
        categories.append("corpus")
    return tuple(categories)


def _tree_metadata(ref: str) -> dict[str, dict[str, str | int]]:
    output = _git("ls-tree", "-r", "-z", "--long", ref, text=False)
    assert isinstance(output, bytes)
    entries: dict[str, dict[str, str | int]] = {}
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        header, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id, raw_size = header.decode("ascii").split()
        if object_type != "blob":
            raise ArchiveContractError(
                f"unsupported tracked object {object_type} at {raw_path!r}"
            )
        path = raw_path.decode("utf-8", errors="strict")
        _safe_path(path)
        entries[path] = {
            "mode": mode,
            "git_blob": object_id,
            "size": int(raw_size),
        }
    return entries


def build_manifest(ref: str = ARCHIVE_TAG) -> dict[str, Any]:
    tag_object = str(_git("rev-parse", ref)).strip()
    tag_object_type = str(_git("cat-file", "-t", ref)).strip()
    if tag_object_type != "tag":
        raise ArchiveContractError(f"archive ref must be annotated, got {tag_object_type}")
    commit = str(_git("rev-parse", f"{ref}^{{commit}}")).strip()
    if commit != BASELINE_COMMIT:
        raise ArchiveContractError(
            f"archive ref resolves to {commit}, expected {BASELINE_COMMIT}"
        )
    metadata = _tree_metadata(ref)
    archive = _git("archive", "--format=tar", ref, text=False)
    assert isinstance(archive, bytes)
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    coverage_counts = {
        "migration": 0,
        "receipt": 0,
        "matrix": 0,
        "manifest": 0,
        "prompt": 0,
        "corpus": 0,
    }
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle:
            if member.isdir():
                continue
            path = str(_safe_path(member.name))
            if path not in metadata:
                raise ArchiveContractError(f"archive contains untracked entry: {path}")
            if member.issym():
                payload = member.linkname.encode("utf-8")
            elif member.isfile():
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise ArchiveContractError(f"unable to read archive entry: {path}")
                payload = extracted.read()
            else:
                raise ArchiveContractError(f"unsupported archive entry type: {path}")
            expected_size = int(metadata[path]["size"])
            if len(payload) != expected_size:
                raise ArchiveContractError(
                    f"size mismatch for {path}: {len(payload)} != {expected_size}"
                )
            categories = _coverage(path)
            for category in categories:
                coverage_counts[category] += 1
            files.append(
                {
                    "path": path,
                    "mode": metadata[path]["mode"],
                    "git_blob": metadata[path]["git_blob"],
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "coverage": list(categories),
                }
            )
            seen.add(path)
    missing = sorted(set(metadata) - seen)
    if missing:
        raise ArchiveContractError(f"archive omitted tracked paths: {missing[:5]}")
    if any(count == 0 for count in coverage_counts.values()):
        raise ArchiveContractError(f"required coverage category is empty: {coverage_counts}")
    files.sort(key=lambda item: item["path"])
    source_time = str(_git("show", "-s", "--format=%cI", commit)).strip()
    git_tree_sha = str(_git("rev-parse", f"{commit}^{{tree}}")).strip()
    return {
        "contract_version": "supportguard-interview-v2-archive.v1",
        "status": "frozen",
        "classification": "full_tracked_repository_archive_no_protected_v6_holdout",
        "archive_tag": ref,
        "tag_object_sha": tag_object,
        "tag_object_type": tag_object_type,
        "commit_sha": commit,
        "git_tree_sha": git_tree_sha,
        "source_commit_time": source_time,
        "file_count": len(files),
        "coverage_counts": coverage_counts,
        "tree_sha256": _canonical_sha256(files),
        "files": files,
    }


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkout_payload(root: Path, relative: str, mode: str) -> bytes:
    path = root / relative
    if mode == "120000":
        if not path.is_symlink():
            raise ArchiveContractError(f"expected symlink: {relative}")
        return os.readlink(path).encode("utf-8")
    if not path.is_file():
        raise ArchiveContractError(f"missing restored file: {relative}")
    return path.read_bytes()


def verify_checkout(manifest_path: Path, checkout: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("commit_sha") != BASELINE_COMMIT:
        raise ArchiveContractError("manifest is not bound to the frozen baseline")
    head = str(_git("rev-parse", "HEAD", cwd=checkout)).strip()
    tracked_raw = _git("ls-files", "-z", cwd=checkout, text=False)
    assert isinstance(tracked_raw, bytes)
    tracked = {
        item.decode("utf-8", errors="strict")
        for item in tracked_raw.split(b"\0")
        if item
    }
    expected = {item["path"] for item in manifest["files"]}
    mismatches: list[str] = []
    for item in manifest["files"]:
        payload = _checkout_payload(checkout, item["path"], item["mode"])
        if len(payload) != item["size"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
            mismatches.append(item["path"])
    status = str(_git("status", "--porcelain", cwd=checkout)).strip()
    receipt = {
        "contract_version": "supportguard-interview-v2-archive-restore.v1",
        "verified_at": datetime.now(UTC).isoformat(),
        "result": "pass"
        if head == BASELINE_COMMIT
        and tracked == expected
        and not mismatches
        and not status
        else "fail",
        "manifest_path": manifest_path.as_posix(),
        "manifest_sha256": _manifest_sha256(manifest_path),
        "expected_commit_sha": BASELINE_COMMIT,
        "restored_head_sha": head,
        "expected_file_count": len(expected),
        "restored_file_count": len(tracked),
        "missing_paths": sorted(expected - tracked),
        "extra_paths": sorted(tracked - expected),
        "mismatched_paths": mismatches,
        "worktree_clean": not status,
    }
    if receipt["result"] != "pass":
        raise ArchiveContractError(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return receipt


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--ref", default=ARCHIVE_TAG)
    build.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--checkout", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_manifest(args.ref)
        _write_json(args.output, result)
    else:
        result = verify_checkout(args.manifest, args.checkout)
        _write_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
