from __future__ import annotations

import inspect
import sys
import tomllib
from pathlib import Path

import pytest

from supportguard.cli import build_parser as build_runtime_parser
from supportguard.validation import cli as validation_cli

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / "backend/src/supportguard"
VALIDATION_ROOT = ROOT / "validation/src/supportguard"


def _project(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_workspace_declares_exact_runtime_validation_dependency() -> None:
    root = _project(ROOT / "pyproject.toml")
    validation = _project(ROOT / "validation/pyproject.toml")
    assert root["tool"]["uv"]["workspace"]["members"] == ["validation"]  # type: ignore[index]
    assert root["tool"]["uv"]["sources"]["supportguard-validation"] == {  # type: ignore[index]
        "workspace": True
    }
    assert "supportguard-validation==0.1.0" in root["dependency-groups"]["dev"]  # type: ignore[index]
    assert validation["project"]["dependencies"] == ["supportguard==0.1.0"]  # type: ignore[index]
    assert validation["tool"]["uv"]["sources"]["supportguard"] == {  # type: ignore[index]
        "workspace": True
    }


def test_runtime_and_validation_wheels_have_distinct_package_roots() -> None:
    root = _project(ROOT / "pyproject.toml")
    validation = _project(ROOT / "validation/pyproject.toml")
    assert root["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [  # type: ignore[index]
        "backend/src/supportguard"
    ]
    assert validation["tool"]["hatch"]["build"]["targets"]["wheel"][  # type: ignore[index]
        "packages"
    ] == ["src/supportguard"]
    assert validation["project"]["scripts"] == {  # type: ignore[index]
        "supportguard-validation": "supportguard.validation.cli:main"
    }


@pytest.mark.parametrize("package", ["acceptance", "diagnostics", "evals", "evidence"])
def test_validation_only_packages_are_absent_from_runtime(package: str) -> None:
    assert not (RUNTIME_ROOT / package).exists()


def test_phase7_validation_distribution_contains_only_current_owners() -> None:
    files = {path.relative_to(VALIDATION_ROOT).as_posix() for path in VALIDATION_ROOT.rglob("*.py")}
    expected_files = {
        "evals/__init__.py",
        "evals/gate.py",
        "evals/deterministic_proof.py",
        "evals/fault_f06.py",
        "evals/journey_j12.py",
        "evals/phase7_common.py",
        "evals/provider_p16.py",
        "evals/rag_dev30.py",
        "evals/scenario_http.py",
        "evidence/__init__.py",
        "evidence/mcp_test_registry.py",
        "evidence/process_contract.py",
        "evidence/publication_window.py",
        "validation/__init__.py",
        "validation/cli.py",
    }
    if (ROOT / "public-mirror-provenance.v1.json").is_file():
        expected_files.add("validation/public_mirror.py")
    assert files == expected_files


def test_validation_distribution_extends_runtime_pkgutil_namespace() -> None:
    runtime_init = (RUNTIME_ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "extend_path(__path__, __name__)" in runtime_init
    assert not (VALIDATION_ROOT / "__init__.py").exists()


def test_runtime_cli_does_not_import_or_expose_evaluation() -> None:
    source = inspect.getsource(sys.modules["supportguard.cli"])
    assert "supportguard.evals" not in source
    command_action = next(
        action for action in build_runtime_parser()._actions if action.dest == "command"
    )
    assert "eval" not in command_action.choices


def test_validation_cli_requires_candidate_binding_before_artifact_or_settings_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        touched.append(f"{args!r}:{kwargs!r}")
        raise AssertionError("unbound route crossed the validation gate")

    monkeypatch.setattr(validation_cli, "get_settings", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(sys, "argv", ["supportguard-validation", "eval", "ie-p16"])
    with pytest.raises(RuntimeError, match="candidate_sha_output_and_identity_required"):
        validation_cli.main()
    assert touched == []
