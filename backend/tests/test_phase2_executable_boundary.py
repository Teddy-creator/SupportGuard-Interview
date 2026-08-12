from __future__ import annotations

import csv
import importlib.util
import io
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SOURCE = ROOT / "backend/src"


def _load_helper() -> ModuleType:
    path = ROOT / "scripts/phase2_executable_boundary.py"
    spec = importlib.util.spec_from_file_location("phase2_executable_boundary", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("phase2_executable_boundary_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


boundary = _load_helper()


def _write_module(root: Path, module: str, source: str) -> None:
    path = root.joinpath(*module.split("."))
    path = path / "__init__.py" if path.name == "supportguard" else path.with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_wheel(path: Path, files: dict[str, bytes], *, dist_info: str) -> Path:
    record = f"{dist_info}/RECORD"
    names = [*files, record]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for name in names:
        writer.writerow((name, "", ""))
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(record, buffer.getvalue())
    return path


def test_current_six_runtime_roots_cannot_reach_validation_namespaces() -> None:
    report = boundary.inspect_runtime_import_boundary((RUNTIME_SOURCE,))

    assert report.roots == boundary.RUNTIME_ROOTS
    assert len(report.roots) == 6
    assert report.forbidden_reachability == {}
    assert report.module_count > 0
    assert report.edge_count > 0
    assert report.strongly_connected_components == ()


def test_import_graph_reports_transitive_forbidden_path_and_records_scc(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _write_module(source, "supportguard", "")
    _write_module(source, "supportguard.root", "from supportguard import allowed\n")
    _write_module(source, "supportguard.allowed", "from supportguard import loop\n")
    _write_module(
        source,
        "supportguard.loop",
        "from supportguard import allowed\nfrom supportguard.evidence import reader\n",
    )
    evidence = source / "supportguard/evidence/reader.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        "raise AssertionError('opaque source must not be parsed')\n", encoding="utf-8"
    )

    report = boundary.inspect_runtime_import_boundary(
        (source,),
        roots=("supportguard.root",),
    )

    assert report.forbidden_reachability == {
        "supportguard.root": (
            "supportguard.evidence",
            "supportguard.evidence.reader",
        )
    }
    assert ("supportguard.allowed", "supportguard.loop") in report.strongly_connected_components


def test_dual_wheel_record_inventory_accepts_disjoint_namespace_overlay(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(
        dist / "supportguard-0.1.0-py3-none-any.whl",
        {
            "supportguard/__init__.py": b"",
            "supportguard/main.py": b"APP = object()\n",
            "supportguard/contracts/context.py": b"",
        },
        dist_info="supportguard-0.1.0.dist-info",
    )
    _write_wheel(
        dist / "supportguard_validation-0.1.0-py3-none-any.whl",
        {
            "supportguard/validation/__init__.py": b"",
            "supportguard/validation/cli.py": b"",
        },
        dist_info="supportguard_validation-0.1.0.dist-info",
    )

    report = boundary.inspect_wheel_boundary(dist, dist)

    assert report.overlapping_paths == ()
    assert report.runtime.record.endswith(".dist-info/RECORD")
    assert report.validation.record.endswith(".dist-info/RECORD")


def test_wheel_inventory_rejects_a_path_missing_from_record(tmp_path: Path) -> None:
    wheel = tmp_path / "supportguard-0.1.0-py3-none-any.whl"
    record = "supportguard-0.1.0.dist-info/RECORD"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("supportguard/main.py", b"")
        archive.writestr(record, f"{record},,\n")

    with pytest.raises(RuntimeError, match="phase2_wheel_record_inventory_mismatch"):
        boundary.inventory_wheel(wheel, distribution="supportguard")


@pytest.mark.parametrize(
    ("runtime_files", "validation_files", "error"),
    [
        (
            {"supportguard/evidence/reader.py": b""},
            {"supportguard/validation/cli.py": b""},
            "phase2_runtime_wheel_validation_namespace",
        ),
        (
            {"supportguard/main.py": b""},
            {"supportguard/__init__.py": b""},
            "phase2_validation_wheel_runtime_namespace_owner",
        ),
        (
            {"supportguard/shared.py": b""},
            {"supportguard/shared.py": b""},
            "phase2_wheel_path_overlap",
        ),
    ],
)
def test_dual_wheel_boundary_fails_closed(
    tmp_path: Path,
    runtime_files: dict[str, bytes],
    validation_files: dict[str, bytes],
    error: str,
) -> None:
    runtime = _write_wheel(
        tmp_path / "supportguard-0.1.0-py3-none-any.whl",
        runtime_files,
        dist_info="supportguard-0.1.0.dist-info",
    )
    validation = _write_wheel(
        tmp_path / "supportguard_validation-0.1.0-py3-none-any.whl",
        validation_files,
        dist_info="supportguard_validation-0.1.0.dist-info",
    )

    with pytest.raises(RuntimeError, match=error):
        boundary.inspect_wheel_boundary(runtime, validation)


def test_clean_environment_runs_isolated_import_and_non_mutating_cli_probe() -> None:
    python = Path(sys.executable)
    runtime_cli = python.with_name("supportguard")
    assert runtime_cli.is_file()

    results = boundary.run_clean_environment_probes(
        python_executable=python,
        import_modules=("supportguard.contracts.process_identity",),
        cli_commands={"runtime-help": (str(runtime_cli.resolve()), "--help")},
    )

    assert [(item.name, item.returncode) for item in results] == [
        ("imports", 0),
        ("runtime-help", 0),
    ]
