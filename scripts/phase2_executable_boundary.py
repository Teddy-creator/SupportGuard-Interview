#!/usr/bin/env -S uv run python
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
import subprocess  # nosec B404
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

RUNTIME_ROOTS = (
    "supportguard.cli",
    "supportguard.main",
    "supportguard.runtime.worker",
    "supportguard.runtime_health",
    "supportguard.mcp.read_server",
    "supportguard.mcp.action_server",
)
FORBIDDEN_RUNTIME_NAMESPACES = (
    "supportguard.acceptance",
    "supportguard.diagnostics",
    "supportguard.evals",
    "supportguard.evidence",
    "supportguard.validation",
)
RUNTIME_WHEEL_FORBIDDEN_PREFIXES = tuple(
    f"{namespace.replace('.', '/')}/" for namespace in FORBIDDEN_RUNTIME_NAMESPACES
)


@dataclass(frozen=True, slots=True)
class ImportGraphReport:
    roots: tuple[str, ...]
    module_count: int
    edge_count: int
    forbidden_reachability: dict[str, tuple[str, ...]]
    strongly_connected_components: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class WheelInventory:
    wheel: str
    record: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WheelBoundaryReport:
    runtime: WheelInventory
    validation: WheelInventory
    overlapping_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    argv: tuple[str, ...]
    returncode: int


def _matches_namespace(module: str, namespaces: Sequence[str]) -> bool:
    return any(module == item or module.startswith(f"{item}.") for item in namespaces)


def _module_name(source_root: Path, path: Path) -> tuple[str, bool]:
    relative = path.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    package = parts[-1] == "__init__"
    if package:
        parts.pop()
    if not parts:
        raise RuntimeError("phase2_import_graph_root_module_invalid")
    return ".".join(parts), package


def _resolve_from_import(
    *,
    current_module: str,
    current_is_package: bool,
    imported_module: str | None,
    level: int,
) -> str:
    if level == 0:
        return imported_module or ""
    package = current_module if current_is_package else current_module.rpartition(".")[0]
    parts = package.split(".") if package else []
    trim = level - 1
    if trim > len(parts):
        return ""
    anchor = parts[: len(parts) - trim] if trim else parts
    if imported_module:
        anchor.extend(imported_module.split("."))
    return ".".join(anchor)


def _imports(
    *,
    source: str,
    current_module: str,
    current_is_package: bool,
) -> set[str]:
    tree = ast.parse(source, filename=current_module)
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(
                alias.name for alias in node.names if alias.name.startswith("supportguard")
            )
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_import(
                current_module=current_module,
                current_is_package=current_is_package,
                imported_module=node.module,
                level=node.level,
            )
            if not base.startswith("supportguard"):
                continue
            result.add(base)
            result.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return result


def build_import_graph(
    source_roots: Sequence[Path],
    *,
    opaque_namespaces: Sequence[str] = FORBIDDEN_RUNTIME_NAMESPACES,
) -> dict[str, set[str]]:
    """Build a supportguard-only AST graph without importing application code.

    Forbidden validation namespaces are opaque: their paths may be discovered,
    but their source is never opened or parsed.
    """

    modules: dict[str, tuple[Path, bool]] = {}
    for source_root in source_roots:
        if not source_root.is_dir():
            raise RuntimeError(f"phase2_import_graph_source_missing:{source_root}")
        for path in sorted(source_root.rglob("*.py")):
            module, is_package = _module_name(source_root, path)
            if module in modules:
                raise RuntimeError(f"phase2_import_graph_duplicate_module:{module}")
            modules[module] = (path, is_package)
    graph: dict[str, set[str]] = {}
    for module, (path, is_package) in modules.items():
        if _matches_namespace(module, opaque_namespaces):
            graph[module] = set()
            continue
        graph[module] = _imports(
            source=path.read_text(encoding="utf-8"),
            current_module=module,
            current_is_package=is_package,
        )
    return graph


def _reachable(graph: Mapping[str, set[str]], root: str) -> set[str]:
    pending = [root]
    observed: set[str] = set()
    while pending:
        module = pending.pop()
        if module in observed:
            continue
        observed.add(module)
        pending.extend(graph.get(module, ()))
    return observed


def _strongly_connected_components(graph: Mapping[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indexes[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        stacked.add(module)
        for dependency in sorted(graph.get(module, ())):
            if dependency not in graph:
                continue
            if dependency not in indexes:
                visit(dependency)
                lowlinks[module] = min(lowlinks[module], lowlinks[dependency])
            elif dependency in stacked:
                lowlinks[module] = min(lowlinks[module], indexes[dependency])
        if lowlinks[module] != indexes[module]:
            return
        component: list[str] = []
        while stack:
            dependency = stack.pop()
            stacked.remove(dependency)
            component.append(dependency)
            if dependency == module:
                break
        if len(component) > 1 or module in graph.get(module, set()):
            components.append(tuple(sorted(component)))

    for module in sorted(graph):
        if module not in indexes:
            visit(module)
    return tuple(sorted(components))


def inspect_runtime_import_boundary(
    source_roots: Sequence[Path],
    *,
    roots: Sequence[str] = RUNTIME_ROOTS,
    forbidden_namespaces: Sequence[str] = FORBIDDEN_RUNTIME_NAMESPACES,
) -> ImportGraphReport:
    graph = build_import_graph(source_roots, opaque_namespaces=forbidden_namespaces)
    missing = sorted(set(roots) - set(graph))
    if missing:
        raise RuntimeError(f"phase2_import_graph_roots_missing:{','.join(missing)}")
    forbidden: dict[str, tuple[str, ...]] = {}
    for root in roots:
        violations = tuple(
            sorted(
                module
                for module in _reachable(graph, root)
                if _matches_namespace(module, forbidden_namespaces)
            )
        )
        if violations:
            forbidden[root] = violations
    return ImportGraphReport(
        roots=tuple(roots),
        module_count=len(graph),
        edge_count=sum(len(edges) for edges in graph.values()),
        forbidden_reachability=forbidden,
        strongly_connected_components=_strongly_connected_components(graph),
    )


def _normalized_distribution(value: str) -> str:
    return value.casefold().replace("-", "_").replace(".", "_")


def _resolve_wheel(dist: Path, distribution: str) -> Path:
    normalized = _normalized_distribution(distribution)

    def matches(path: Path) -> bool:
        return path.name.partition("-")[0].casefold() == normalized

    if dist.is_file() and dist.suffix == ".whl":
        candidates = [dist] if matches(dist) else []
    elif dist.is_dir():
        candidates = sorted(path for path in dist.glob("*.whl") if matches(path))
    else:
        raise RuntimeError(f"phase2_wheel_dist_missing:{dist}")
    if len(candidates) != 1:
        raise RuntimeError(f"phase2_wheel_selection_invalid:{distribution}:{len(candidates)}")
    return candidates[0]


def inventory_wheel(dist: Path, *, distribution: str) -> WheelInventory:
    wheel = _resolve_wheel(dist, distribution)
    try:
        with zipfile.ZipFile(wheel) as archive:
            paths = tuple(sorted(item.filename for item in archive.infolist() if not item.is_dir()))
            if len(paths) != len(set(paths)):
                raise RuntimeError("phase2_wheel_duplicate_path")
            if any(
                PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or "\\" in path
                for path in paths
            ):
                raise RuntimeError("phase2_wheel_path_unsafe")
            record_paths = [path for path in paths if path.endswith(".dist-info/RECORD")]
            if len(record_paths) != 1:
                raise RuntimeError("phase2_wheel_record_missing")
            record_path = record_paths[0]
            record_distribution = PurePosixPath(record_path).parent.name.partition("-")[0]
            if record_distribution.casefold() != _normalized_distribution(distribution):
                raise RuntimeError("phase2_wheel_distribution_mismatch")
            rows = list(csv.reader(io.StringIO(archive.read(record_path).decode("utf-8"))))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError("phase2_wheel_malformed") from exc
    recorded = [row[0] for row in rows if row]
    if len(recorded) != len(set(recorded)) or set(recorded) != set(paths):
        raise RuntimeError("phase2_wheel_record_inventory_mismatch")
    return WheelInventory(wheel=str(wheel), record=record_path, paths=paths)


def inspect_wheel_boundary(runtime_dist: Path, validation_dist: Path) -> WheelBoundaryReport:
    runtime = inventory_wheel(runtime_dist, distribution="supportguard")
    validation = inventory_wheel(validation_dist, distribution="supportguard-validation")
    overlaps = tuple(sorted(set(runtime.paths) & set(validation.paths)))
    if overlaps:
        raise RuntimeError(f"phase2_wheel_path_overlap:{','.join(overlaps)}")
    forbidden = tuple(
        path for path in runtime.paths if path.startswith(RUNTIME_WHEEL_FORBIDDEN_PREFIXES)
    )
    if forbidden:
        raise RuntimeError(f"phase2_runtime_wheel_validation_namespace:{','.join(forbidden)}")
    if "supportguard/__init__.py" in validation.paths:
        raise RuntimeError("phase2_validation_wheel_runtime_namespace_owner")
    return WheelBoundaryReport(runtime=runtime, validation=validation, overlapping_paths=overlaps)


def _clean_environment(home: Path, python_executable: Path) -> dict[str, str]:
    path_entries = [str(python_executable.parent), "/usr/bin", "/bin"]
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.pathsep.join(path_entries),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SUPPORTGUARD_DISABLE_DOTENV": "1",
        "TMPDIR": str(home),
    }


def _run_probe(
    *,
    name: str,
    argv: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float,
) -> ProbeResult:
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
        detail = (completed.stderr or completed.stdout)[-2000:].replace("\x00", "")
        raise RuntimeError(f"phase2_clean_probe_failed:{name}:{completed.returncode}:{detail}")
    return ProbeResult(name=name, argv=tuple(argv), returncode=completed.returncode)


def run_clean_environment_probes(
    *,
    python_executable: Path,
    import_modules: Sequence[str],
    cli_commands: Mapping[str, Sequence[str]],
    timeout_seconds: float = 15,
) -> tuple[ProbeResult, ...]:
    """Run import and non-mutating CLI probes without ambient project state or secrets."""

    # Preserve a virtual-environment launcher path. Resolving its symlink would
    # silently switch `python -I` to the base interpreter and invalidate the
    # clean installed-wheel probe.
    executable = python_executable.absolute()
    if not executable.is_file():
        raise RuntimeError("phase2_clean_probe_python_missing")
    if any(not command or not Path(command[0]).is_absolute() for command in cli_commands.values()):
        raise RuntimeError("phase2_clean_probe_cli_must_be_absolute")
    with tempfile.TemporaryDirectory(prefix="supportguard-phase2-clean-") as temp:
        home = Path(temp)
        environment = _clean_environment(home, executable)
        code = (
            "import importlib,json;"
            f"modules=json.loads({json.dumps(list(import_modules))!r});"
            "[importlib.import_module(module) for module in modules]"
        )
        results = [
            _run_probe(
                name="imports",
                argv=(str(executable), "-I", "-c", code),
                environment=environment,
                cwd=home,
                timeout_seconds=timeout_seconds,
            )
        ]
        for name, command in sorted(cli_commands.items()):
            results.append(
                _run_probe(
                    name=name,
                    argv=tuple(command),
                    environment=environment,
                    cwd=home,
                    timeout_seconds=timeout_seconds,
                )
            )
    return tuple(results)


def _json_payload(value: ImportGraphReport | WheelBoundaryReport) -> str:
    return json.dumps(asdict(value), indent=2, sort_keys=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase2-executable-boundary")
    commands = parser.add_subparsers(dest="command", required=True)
    graph = commands.add_parser("import-graph")
    graph.add_argument("source", nargs="+", type=Path)
    wheels = commands.add_parser("wheels")
    wheels.add_argument("runtime_dist", type=Path)
    wheels.add_argument("validation_dist", type=Path)
    clean = commands.add_parser("clean-probe")
    clean.add_argument("--python", type=Path, required=True)
    clean.add_argument("--import-module", action="append", default=[])
    clean.add_argument("--cli-json", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "import-graph":
        print(_json_payload(inspect_runtime_import_boundary(arguments.source)))
    elif arguments.command == "wheels":
        print(
            _json_payload(inspect_wheel_boundary(arguments.runtime_dist, arguments.validation_dist))
        )
    else:
        commands: dict[str, list[str]] = {}
        for index, raw in enumerate(arguments.cli_json):
            value: Any = json.loads(raw)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise RuntimeError("phase2_clean_probe_cli_json_invalid")
            commands[f"cli-{index + 1}"] = value
        report = run_clean_environment_probes(
            python_executable=arguments.python,
            import_modules=arguments.import_module,
            cli_commands=commands,
        )
        print(json.dumps([asdict(item) for item in report], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
