#!/usr/bin/env -S uv run python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from phase2_executable_boundary import (
    FORBIDDEN_RUNTIME_NAMESPACES,
    RUNTIME_ROOTS,
    ProbeResult,
    inspect_runtime_import_boundary,
    inspect_wheel_boundary,
    run_clean_environment_probes,
)

SCHEMA = "supportguard-phase2-package-boundary.v2"
VALIDATION_TOOL_PACKAGES = (
    "supportguard.evals",
    "supportguard.evidence",
)
VALIDATION_CLI_MODULE = "supportguard.validation.cli"
DEFAULT_TIMEOUT_SECONDS = 300.0

BoundaryExecutor = Callable[[Path, Path], dict[str, Any]]
ReportMode = Literal["probe", "candidate"]


class PackageBoundaryError(RuntimeError):
    """A stable, non-secret Phase 2 package-boundary failure."""


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    git_head: str
    git_tree: str
    worktree_state: Literal["clean", "dirty"]
    source_state_digest: str
    source_file_count: int


def _venv_executable(venv: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" and name == "python" else ""
    return venv / directory / f"{name}{suffix}"


def _command_environment() -> dict[str, str]:
    """Return the minimum environment required for an offline uv invocation."""

    environment = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SUPPORTGUARD_DISABLE_DOTENV": "1",
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    for name in ("UV_CACHE_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_checked(
    *,
    name: str,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        list(argv),
        cwd=cwd,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise PackageBoundaryError(f"phase2_command_failed:{name}:{completed.returncode}")
    return {"name": name, "returncode": completed.returncode, "status": "passed"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(
    repository_root: Path,
    *arguments: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if executable is None:
        raise PackageBoundaryError("phase2_git_missing")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(Path(executable).resolve()), "-C", str(repository_root), *arguments],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode not in allowed_returncodes:
        raise PackageBoundaryError("phase2_git_command_failed")
    return completed


def _source_files(repository_root: Path) -> tuple[Path, ...]:
    raw = _git(
        repository_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
    ).stdout
    files: list[Path] = []
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        relative = PurePosixPath(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts:
            raise PackageBoundaryError("phase2_source_path_unsafe")
        files.append(repository_root.joinpath(*relative.parts))
    return tuple(files)


def _source_state_digest(repository_root: Path) -> tuple[str, int]:
    manifest: list[dict[str, object]] = []
    for path in _source_files(repository_root):
        relative = path.relative_to(repository_root).as_posix()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            manifest.append(
                {
                    "executable": False,
                    "kind": "missing",
                    "path": relative,
                    "sha256": None,
                }
            )
            continue
        except OSError as exc:
            raise PackageBoundaryError("phase2_source_file_unreadable") from exc
        executable = bool(metadata.st_mode & 0o111)
        if stat.S_ISREG(metadata.st_mode):
            kind = "file"
            content_digest = _sha256(path)
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            content_digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        else:
            raise PackageBoundaryError("phase2_source_file_type_unsupported")
        manifest.append(
            {
                "executable": executable,
                "kind": kind,
                "path": relative,
                "sha256": content_digest,
            }
        )
    manifest.sort(key=lambda item: str(item["path"]))
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest(), len(manifest)


def source_identity(repository_root: Path) -> SourceIdentity:
    repository = repository_root.resolve()
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
    tree = _git(repository, "rev-parse", "--verify", "HEAD^{tree}").stdout.decode().strip()
    status_before = _git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    digest, count = _source_state_digest(repository)
    status_after = _git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    if status_before != status_after:
        raise PackageBoundaryError("phase2_source_state_unstable")
    return SourceIdentity(
        git_head=head,
        git_tree=tree,
        worktree_state="clean" if not status_after else "dirty",
        source_state_digest=digest,
        source_file_count=count,
    )


def _runtime_contract_code() -> str:
    roots = json.dumps(list(RUNTIME_ROOTS), separators=(",", ":"))
    forbidden = json.dumps(list(FORBIDDEN_RUNTIME_NAMESPACES), separators=(",", ":"))
    return (
        "import importlib,importlib.util,json;"
        f"roots=json.loads({roots!r});"
        f"forbidden=json.loads({forbidden!r});"
        "[importlib.import_module(module) for module in roots];"
        "missing=[module for module in forbidden if importlib.util.find_spec(module) is not None];"
        "assert not missing,('forbidden_runtime_namespace',missing);"
        "cli=importlib.import_module('supportguard.cli');"
        "parser=cli.build_parser();"
        "choices={choice for action in parser._actions "
        "for choice in (getattr(action,'choices',None) or {})};"
        "assert 'eval' not in choices,'runtime_cli_exposes_eval'"
    )


def _probe_summary(results: Sequence[ProbeResult]) -> list[dict[str, object]]:
    return [
        {
            "name": result.name,
            "returncode": result.returncode,
            "status": "passed",
        }
        for result in results
    ]


def _execute_boundary(work: Path, repository_root: Path) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        raise PackageBoundaryError("phase2_uv_missing")
    uv_executable = str(Path(uv).resolve())
    environment = _command_environment()
    subprocess_temp = work / "tmp"
    dist = work / "dist"
    runtime_venv = work / "runtime-venv"
    validation_venv = work / "validation-venv"
    dist.mkdir()
    subprocess_temp.mkdir()
    environment["TMPDIR"] = str(subprocess_temp)

    commands: list[dict[str, object]] = []
    common_build = (
        uv_executable,
        "build",
        "--wheel",
        "--offline",
        "--no-python-downloads",
        "--no-build-logs",
        "--no-create-gitignore",
        "--out-dir",
        str(dist),
    )
    commands.append(
        _run_checked(
            name="build-runtime-wheel",
            argv=(*common_build, "--package", "supportguard"),
            cwd=repository_root,
            environment=environment,
        )
    )
    commands.append(
        _run_checked(
            name="build-validation-wheel",
            argv=(*common_build, "--package", "supportguard-validation"),
            cwd=repository_root,
            environment=environment,
        )
    )

    wheel_report = inspect_wheel_boundary(dist, dist)
    runtime_wheel = Path(wheel_report.runtime.wheel)
    validation_wheel = Path(wheel_report.validation.wheel)
    graph_report = inspect_runtime_import_boundary(
        (repository_root / "backend/src",),
    )
    if graph_report.forbidden_reachability:
        raise PackageBoundaryError("phase2_runtime_import_graph_forbidden_reachability")

    for name, venv in (
        ("create-runtime-venv", runtime_venv),
        ("create-validation-venv", validation_venv),
    ):
        commands.append(
            _run_checked(
                name=name,
                argv=(
                    uv_executable,
                    "venv",
                    "--offline",
                    "--no-python-downloads",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(venv),
                ),
                cwd=work,
                environment=environment,
            )
        )

    runtime_python = _venv_executable(runtime_venv, "python")
    validation_python = _venv_executable(validation_venv, "python")
    commands.append(
        _run_checked(
            name="install-runtime-wheel",
            argv=(
                uv_executable,
                "pip",
                "install",
                "--offline",
                "--no-config",
                "--strict",
                "--python",
                str(runtime_python),
                str(runtime_wheel),
            ),
            cwd=work,
            environment=environment,
        )
    )
    commands.append(
        _run_checked(
            name="install-runtime-and-validation-wheels",
            argv=(
                uv_executable,
                "pip",
                "install",
                "--offline",
                "--no-config",
                "--strict",
                "--python",
                str(validation_python),
                str(runtime_wheel),
                str(validation_wheel),
            ),
            cwd=work,
            environment=environment,
        )
    )

    runtime_probes = run_clean_environment_probes(
        python_executable=runtime_python,
        import_modules=RUNTIME_ROOTS,
        cli_commands={
            "runtime-boundary": (
                str(runtime_python.absolute()),
                "-I",
                "-c",
                _runtime_contract_code(),
            ),
            "runtime-cli-help": (
                str(_venv_executable(runtime_venv, "supportguard").absolute()),
                "--help",
            ),
        },
    )
    validation_probes = run_clean_environment_probes(
        python_executable=validation_python,
        import_modules=(*VALIDATION_TOOL_PACKAGES, VALIDATION_CLI_MODULE),
        cli_commands={
            "validation-cli-help": (
                str(_venv_executable(validation_venv, "supportguard-validation").absolute()),
                "--help",
            )
        },
    )

    return {
        "commands": commands,
        "import_graph": {
            **asdict(graph_report),
            "forbidden_reachability_count": sum(
                len(items) for items in graph_report.forbidden_reachability.values()
            ),
            "strongly_connected_component_count": len(graph_report.strongly_connected_components),
        },
        "runtime_environment": {
            "cli_exposes_eval": False,
            "forbidden_find_spec": {namespace: None for namespace in FORBIDDEN_RUNTIME_NAMESPACES},
            "imports": list(RUNTIME_ROOTS),
            "probes": _probe_summary(runtime_probes),
        },
        "validation_environment": {
            "cli_help_reachable": True,
            "imports": [*VALIDATION_TOOL_PACKAGES, VALIDATION_CLI_MODULE],
            "probes": _probe_summary(validation_probes),
        },
        "wheels": {
            "overlapping_paths": list(wheel_report.overlapping_paths),
            "runtime": {
                "filename": runtime_wheel.name,
                "record": wheel_report.runtime.record,
                "record_count": len(wheel_report.runtime.paths),
                "sha256": _sha256(runtime_wheel),
            },
            "validation": {
                "filename": validation_wheel.name,
                "record": wheel_report.validation.record,
                "record_count": len(wheel_report.validation.paths),
                "sha256": _sha256(validation_wheel),
            },
        },
    }


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "phase2_command_timeout"
    value = str(exc).partition(":")[0]
    if value.startswith("phase2_") and value.replace("_", "").isalnum():
        return value
    return "phase2_package_boundary_unexpected_failure"


def _candidate_output_allowed(repository_root: Path, output: Path) -> bool:
    repository = repository_root.resolve()
    resolved = output.resolve(strict=False)
    try:
        relative = resolved.relative_to(repository)
    except ValueError:
        return True
    if not relative.parts or relative.parts[0] == ".git":
        return False
    checked = _git(
        repository,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        relative.as_posix(),
        allowed_returncodes=(0, 1),
    )
    return checked.returncode == 0


def run_package_boundary(
    repository_root: Path,
    *,
    executor: BoundaryExecutor | None = None,
    mode: ReportMode = "probe",
    output: Path | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Run one source-bound proof and fail closed for durable Candidate evidence."""

    repository = repository_root.resolve()
    owned: Path | None = None
    body: dict[str, Any] = {}
    status = "passed"
    identity: SourceIdentity | None = None
    source_stable = False
    try:
        if mode not in {"probe", "candidate"}:
            raise PackageBoundaryError("phase2_report_mode_invalid")
        identity = source_identity(repository)
        if mode == "candidate":
            if (
                expected_head is None
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_head) is None
            ):
                raise PackageBoundaryError("phase2_candidate_expected_head_required")
            if identity.git_head != expected_head:
                raise PackageBoundaryError("phase2_candidate_head_mismatch")
            if output is None:
                raise PackageBoundaryError("phase2_candidate_output_required")
            if not _candidate_output_allowed(repository, output):
                raise PackageBoundaryError("phase2_candidate_output_not_ignored")
            if identity.worktree_state != "clean":
                raise PackageBoundaryError("phase2_candidate_worktree_dirty")
        with tempfile.TemporaryDirectory(prefix="supportguard-phase2-package-") as raw:
            owned = Path(raw)
            body = (executor or _execute_boundary)(owned, repository)
        observed = source_identity(repository)
        source_stable = observed == identity
        if not source_stable:
            raise PackageBoundaryError("phase2_source_changed_during_execution")
        if mode == "candidate" and observed.worktree_state != "clean":
            raise PackageBoundaryError("phase2_candidate_worktree_dirty")
    except Exception as exc:
        status = "failed"
        body = {"failure_code": _failure_code(exc)}
    cleanup = owned is None or not owned.exists()
    return {
        "candidate_eligible": status == "passed" and mode == "candidate",
        "classification": "candidate_receipt"
        if status == "passed" and mode == "candidate"
        else ("non_candidate_probe" if mode == "probe" else "candidate_attempt"),
        "cleanup": {
            "invocation_temp_removed": cleanup,
            "shared_uv_cache_owned": False,
        },
        **body,
        "source_identity": asdict(identity) if identity is not None else None,
        "source_identity_verified_after_execution": source_stable,
        "schema": SCHEMA,
        "status": status,
    }


def _write_json(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-phase2-package-boundary")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("probe", "candidate"), default="probe")
    parser.add_argument("--expected-head")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = run_package_boundary(
        arguments.repository_root,
        mode=arguments.mode,
        output=arguments.output,
        expected_head=arguments.expected_head,
    )
    if arguments.output is not None:
        _write_json(arguments.output, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
