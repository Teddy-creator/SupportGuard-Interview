from __future__ import annotations

import csv
import importlib.util
import io
import shutil
import subprocess  # nosec B404
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load_runner() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / "run_phase2_package_boundary.py"
        spec = importlib.util.spec_from_file_location("run_phase2_package_boundary", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("phase2_package_boundary_runner_import_failed")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


runner = _load_runner()


def _write_wheel(path: Path, files: dict[str, bytes], *, dist_info: str) -> None:
    record = f"{dist_info}/RECORD"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for name in (*files, record):
        writer.writerow((name, "", ""))
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(record, buffer.getvalue())


def _stable_identity() -> object:
    return runner.SourceIdentity(
        git_head="a" * 40,
        git_tree="b" * 40,
        worktree_state="dirty",
        source_state_digest="c" * 64,
        source_file_count=7,
    )


def _git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git_missing")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(Path(executable).resolve()), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _committed_repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "phase2@example.invalid")
    _git(path, "config", "user.name", "Phase 2 Test")
    (path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (path / "source.txt").write_text("first\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "source.txt")
    _git(path, "commit", "--quiet", "-m", "initial")
    return path


def test_runner_cleans_owned_temp_and_never_serializes_its_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(runner, "source_identity", lambda _: _stable_identity())

    def execute(work: Path, repository_root: Path) -> dict[str, object]:
        observed.append(work)
        assert repository_root == ROOT.resolve()
        (work / "owned-marker").write_text("owned", encoding="utf-8")
        return {"proof": "passed"}

    report = runner.run_package_boundary(ROOT, executor=execute)

    assert report == {
        "candidate_eligible": False,
        "classification": "non_candidate_probe",
        "cleanup": {
            "invocation_temp_removed": True,
            "shared_uv_cache_owned": False,
        },
        "proof": "passed",
        "source_identity": {
            "git_head": "a" * 40,
            "git_tree": "b" * 40,
            "source_file_count": 7,
            "source_state_digest": "c" * 64,
            "worktree_state": "dirty",
        },
        "source_identity_verified_after_execution": True,
        "schema": "supportguard-phase2-package-boundary.v2",
        "status": "passed",
    }
    assert len(observed) == 1
    assert not observed[0].exists()
    assert str(observed[0]) not in runner.json.dumps(report)


def test_runner_cleans_owned_temp_and_emits_stable_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(runner, "source_identity", lambda _: _stable_identity())

    def fail(work: Path, repository_root: Path) -> dict[str, object]:
        del repository_root
        observed.append(work)
        raise RuntimeError(f"phase2_stable_code:{work}:secret-detail")

    report = runner.run_package_boundary(ROOT, executor=fail)

    assert report["status"] == "failed"
    assert report["classification"] == "non_candidate_probe"
    assert report["candidate_eligible"] is False
    assert report["failure_code"] == "phase2_stable_code"
    assert report["cleanup"]["invocation_temp_removed"] is True
    assert not observed[0].exists()
    assert str(observed[0]) not in runner.json.dumps(report)


def test_dirty_worktree_cannot_masquerade_as_candidate_but_probe_is_explicit(
    tmp_path: Path,
) -> None:
    repository = _committed_repository(tmp_path / "repository")
    (repository / "source.txt").write_text("dirty\n", encoding="utf-8")
    executions: list[str] = []

    def execute(work: Path, repository_root: Path) -> dict[str, object]:
        del work, repository_root
        executions.append("executed")
        return {"proof": "passed"}

    candidate = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )
    probe = runner.run_package_boundary(repository, executor=execute, mode="probe")

    assert candidate["status"] == "failed"
    assert candidate["classification"] == "candidate_attempt"
    assert candidate["candidate_eligible"] is False
    assert candidate["failure_code"] == "phase2_candidate_worktree_dirty"
    assert candidate["source_identity"]["worktree_state"] == "dirty"
    assert probe["status"] == "passed"
    assert probe["classification"] == "non_candidate_probe"
    assert probe["candidate_eligible"] is False
    assert executions == ["executed"]


def test_candidate_receipt_binds_head_and_deterministic_source_digest(tmp_path: Path) -> None:
    repository = _committed_repository(tmp_path / "repository")

    def execute(work: Path, repository_root: Path) -> dict[str, object]:
        del work, repository_root
        return {"proof": "passed"}

    first = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )
    repeated = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )
    (repository / "source.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "--quiet", "-m", "second")
    second = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )

    assert first["status"] == "passed"
    assert first["classification"] == "candidate_receipt"
    assert first["candidate_eligible"] is True
    assert first["source_identity_verified_after_execution"] is True
    assert first["source_identity"] == repeated["source_identity"]
    assert first["source_identity"]["git_head"] == _git(repository, "rev-parse", "HEAD~1")
    assert len(first["source_identity"]["source_state_digest"]) == 64
    assert first["source_identity"]["git_head"] != second["source_identity"]["git_head"]
    assert (
        first["source_identity"]["source_state_digest"]
        != second["source_identity"]["source_state_digest"]
    )


def test_candidate_fails_when_source_changes_during_execution(tmp_path: Path) -> None:
    repository = _committed_repository(tmp_path / "repository")

    def mutate(work: Path, repository_root: Path) -> dict[str, object]:
        del work
        (repository_root / "source.txt").write_text("changed-during-run\n", encoding="utf-8")
        return {"proof": "must-not-survive"}

    report = runner.run_package_boundary(
        repository,
        executor=mutate,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )

    assert report["status"] == "failed"
    assert report["failure_code"] == "phase2_source_changed_during_execution"
    assert report["candidate_eligible"] is False
    assert report["source_identity_verified_after_execution"] is False


def test_candidate_requires_durable_ignored_or_external_output(tmp_path: Path) -> None:
    repository = _committed_repository(tmp_path / "repository")
    calls: list[str] = []

    def execute(work: Path, repository_root: Path) -> dict[str, object]:
        del work, repository_root
        calls.append("executed")
        return {}

    head = _git(repository, "rev-parse", "HEAD")
    missing = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        expected_head=head,
    )
    unignored = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "receipt.json",
        expected_head=head,
    )
    mismatched = runner.run_package_boundary(
        repository,
        executor=execute,
        mode="candidate",
        output=repository / "dist/phase2/receipt.json",
        expected_head="0" * 40,
    )

    assert missing["failure_code"] == "phase2_candidate_output_required"
    assert unignored["failure_code"] == "phase2_candidate_output_not_ignored"
    assert mismatched["failure_code"] == "phase2_candidate_head_mismatch"
    assert calls == []


def test_durable_json_write_atomically_replaces_existing_receipt(tmp_path: Path) -> None:
    output = tmp_path / "receipts/phase2.json"
    output.parent.mkdir()
    output.write_text("stale", encoding="utf-8")

    runner._write_json(output, {"schema": "test.v1", "status": "passed"})

    assert runner.json.loads(output.read_text(encoding="utf-8")) == {
        "schema": "test.v1",
        "status": "passed",
    }
    assert list(output.parent.glob(f".{output.name}.*")) == []


def test_make_target_requests_exact_candidate_receipt() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PHASE2_PACKAGE_BOUNDARY_RECEIPT ?= dist/phase2/" in makefile
    assert "--mode candidate" in makefile
    assert '--expected-head "$(PHASE2_CANDIDATE_SHA)"' in makefile
    assert '--output "$(PHASE2_PACKAGE_BOUNDARY_RECEIPT)"' in makefile


def test_uv_environment_is_offline_allowlisted_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross")
    monkeypatch.setenv("DATABASE_URL", "must-not-cross")

    environment = runner._command_environment()

    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["UV_LINK_MODE"] == "copy"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "DATABASE_URL" not in environment


def test_execute_boundary_builds_two_wheels_and_probes_both_clean_environments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[str, tuple[str, ...]]] = []
    sync_project_environments: list[str] = []
    temporary_roots: list[str] = []

    def run_checked(**kwargs: object) -> dict[str, object]:
        name = str(kwargs["name"])
        argv = tuple(str(item) for item in kwargs["argv"])  # type: ignore[arg-type]
        invocations.append((name, argv))
        temporary_roots.append(str(kwargs["environment"]["TMPDIR"]))  # type: ignore[index]
        if name == "build-runtime-wheel":
            _write_wheel(
                tmp_path / "dist/supportguard-0.1.0-py3-none-any.whl",
                {
                    "supportguard/__init__.py": b"",
                    "supportguard/main.py": b"",
                },
                dist_info="supportguard-0.1.0.dist-info",
            )
        elif name == "build-validation-wheel":
            _write_wheel(
                tmp_path / "dist/supportguard_validation-0.1.0-py3-none-any.whl",
                {"supportguard/validation/cli.py": b""},
                dist_info="supportguard_validation-0.1.0.dist-info",
            )
        elif name.startswith("sync-"):
            sync_project_environments.append(
                str(kwargs["environment"]["UV_PROJECT_ENVIRONMENT"])  # type: ignore[index]
            )
            venv = tmp_path / ("runtime-venv" if "runtime" in name else "validation-venv")
            (venv / "bin").mkdir(parents=True)
            for executable in ("python", "supportguard", "supportguard-validation"):
                (venv / "bin" / executable).write_text("", encoding="utf-8")
        return {"name": name, "returncode": 0, "status": "passed"}

    def clean_probe(**kwargs: object) -> tuple[SimpleNamespace, ...]:
        commands = kwargs["cli_commands"]
        return (
            SimpleNamespace(name="imports", returncode=0),
            *(SimpleNamespace(name=name, returncode=0) for name in sorted(commands)),  # type: ignore[arg-type]
        )

    graph = runner.inspect_runtime_import_boundary((ROOT / "backend/src",))
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/uv")
    monkeypatch.setattr(runner, "_run_checked", run_checked)
    monkeypatch.setattr(runner, "run_clean_environment_probes", clean_probe)
    monkeypatch.setattr(runner, "inspect_runtime_import_boundary", lambda _: graph)

    report = runner._execute_boundary(tmp_path, ROOT)

    assert [name for name, _ in invocations] == [
        "build-runtime-wheel",
        "build-validation-wheel",
        "sync-runtime-dependencies",
        "sync-validation-dependencies",
        "install-runtime-wheel",
        "install-runtime-and-validation-wheels",
    ]
    for name, argv in invocations:
        if name.startswith(("build-", "sync-", "install-")):
            assert "--offline" in argv
    assert sync_project_environments == [
        str(tmp_path / "runtime-venv"),
        str(tmp_path / "validation-venv"),
    ]
    assert set(temporary_roots) == {str(tmp_path / "tmp")}
    assert report["wheels"]["overlapping_paths"] == []
    assert report["runtime_environment"]["cli_exposes_eval"] is False
    assert report["runtime_environment"]["forbidden_find_spec"] == {
        namespace: None for namespace in runner.FORBIDDEN_RUNTIME_NAMESPACES
    }
    assert report["validation_environment"]["imports"] == [
        *runner.VALIDATION_TOOL_PACKAGES,
        runner.VALIDATION_CLI_MODULE,
    ]
