#!/usr/bin/env python3
"""Inspect and smoke-test the wheel-only SupportGuard runtime image."""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - fixed Docker CLI with argument-vector inputs
import tempfile
from pathlib import Path
from typing import Any

INVENTORY_PROBE = r"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import re
import shutil
from pathlib import Path, PurePosixPath

import supportguard
from supportguard.cli import build_parser

forbidden_modules = (
    "supportguard.acceptance",
    "supportguard.diagnostics",
    "supportguard.evals",
    "supportguard.evidence",
    "supportguard.validation",
)
forbidden_distribution_prefixes = tuple(
    f"supportguard/{package}/"
    for package in ("acceptance", "diagnostics", "evals", "evidence", "validation")
)
forbidden_distributions = (
    "bandit",
    "build",
    "hatchling",
    "mypy",
    "pip-audit",
    "pytest",
    "ruff",
    "uv",
)
forbidden_executables = (
    "bandit",
    "hatchling",
    "mypy",
    "pip-audit",
    "pytest",
    "ruff",
    "uv",
)
forbidden_paths = (
    "/app/backend/src",
    "/app/backend/tests",
    "/app/validation",
    "/app/tests",
    "/app/scripts",
    "/app/src",
    "/app/pyproject.toml",
    "/app/uv.lock",
)


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


distribution = importlib.metadata.distribution("supportguard")
distribution_files = sorted(str(item) for item in (distribution.files or ()))
distribution_file_leaks = [
    item
    for item in distribution_files
    if item.startswith(forbidden_distribution_prefixes)
    or {"tests", "scripts"}.intersection(PurePosixPath(item).parts)
]
command_action = next(
    action for action in build_parser()._actions if action.dest == "command"
)
module_leaks = [name for name in forbidden_modules if importlib.util.find_spec(name) is not None]
distribution_leaks = {
    name: found
    for name in forbidden_distributions
    if (found := version(name)) is not None
}
executable_leaks = {
    name: found
    for name in forbidden_executables
    if (found := shutil.which(name)) is not None
}
path_leaks = [path for path in forbidden_paths if Path(path).exists()]
package_path = str(Path(supportguard.__file__).resolve())
wheel_hash = Path("/app/runtime-wheel.sha256").read_text().strip()
violations = {
    "module_leaks": module_leaks,
    "distribution_leaks": distribution_leaks,
    "runtime_distribution_file_leaks": distribution_file_leaks,
    "executable_leaks": executable_leaks,
    "path_leaks": path_leaks,
    "runtime_cli_exposes_eval": "eval" in command_action.choices,
    "runtime_package_outside_venv": not package_path.startswith("/app/runtime-venv/"),
    "runtime_distribution_empty": not distribution_files,
    "runtime_wheel_hash_invalid": re.fullmatch(r"[0-9a-f]{64}", wheel_hash) is None,
}
payload = {
    "schema": "supportguard-runtime-image-inventory.v1",
    "status": "passed" if not any(violations.values()) else "failed",
    "supportguard_version": version("supportguard"),
    "supportguard_package_path": package_path,
    "supportguard_distribution_file_count": len(distribution_files),
    "runtime_commands": sorted(command_action.choices),
    "pip_distribution": version("pip"),
    "pip_executable": shutil.which("pip"),
    "wheel_sha256": wheel_hash,
    "violations": violations,
}
print(json.dumps(payload, sort_keys=True))
if payload["status"] != "passed":
    raise SystemExit(1)
"""

SMOKE_PROBE = r"""
from __future__ import annotations

import importlib
import json

modules = (
    "supportguard.main",
    "supportguard.runtime.worker",
    "supportguard.mcp.read_server",
    "supportguard.mcp.action_server",
)
for name in modules:
    importlib.import_module(name)
print(json.dumps({
    "schema": "supportguard-runtime-image-smoke.v1",
    "status": "passed",
    "imports": modules,
}, sort_keys=True))
"""


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        list(arguments),
        check=check,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _remove_container(cid_file: Path) -> None:
    if not cid_file.is_file():
        return
    container_id = cid_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise RuntimeError("runtime image smoke produced an invalid container id")
    _run("docker", "rm", "--force", container_id, check=False)


def _run_container(image: str, *, entrypoint: str, arguments: list[str]) -> str:
    with tempfile.TemporaryDirectory(prefix="supportguard-runtime-image-") as directory:
        cid_file = Path(directory) / "container.cid"
        try:
            result = _run(
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--cidfile",
                str(cid_file),
                "--label",
                "io.supportguard.validation=runtime-image-contract",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:size=16m,mode=1777",  # noqa: S108
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                entrypoint,
                image,
                *arguments,
            )
            return result.stdout
        finally:
            _remove_container(cid_file)


def _image_metadata(image: str) -> dict[str, Any]:
    result = _run("docker", "image", "inspect", image)
    value = json.loads(result.stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError("runtime image inspect response is invalid")
    return value[0]


def inventory(image: str, *, expected_revision: str | None) -> dict[str, Any]:
    metadata = _image_metadata(image)
    config = metadata.get("Config")
    if not isinstance(config, dict):
        raise RuntimeError("runtime image config is missing")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise RuntimeError("runtime image labels are missing")
    if labels.get("io.supportguard.package-boundary") != "runtime-wheel-only":
        raise RuntimeError("runtime image package boundary label is invalid")
    if (
        expected_revision is not None
        and labels.get("org.opencontainers.image.revision") != expected_revision
    ):
        raise RuntimeError("runtime image revision does not match the requested Candidate")
    raw_probe = _run_container(
        image,
        entrypoint="python",
        arguments=["-I", "-c", INVENTORY_PROBE],
    )
    probe = json.loads(raw_probe)
    if not isinstance(probe, dict) or probe.get("status") != "passed":
        raise RuntimeError("runtime image inventory probe did not pass")
    return {
        "schema": "supportguard-runtime-image-contract.v1",
        "status": "passed",
        "image_id": metadata.get("Id"),
        "size_bytes": metadata.get("Size"),
        "revision": labels.get("org.opencontainers.image.revision"),
        "inventory": probe,
    }


def smoke(image: str) -> dict[str, Any]:
    raw_imports = _run_container(
        image,
        entrypoint="python",
        arguments=["-I", "-c", SMOKE_PROBE],
    )
    imports = json.loads(raw_imports)
    if not isinstance(imports, dict) or imports.get("status") != "passed":
        raise RuntimeError("runtime image import smoke did not pass")
    help_output = _run_container(
        image,
        entrypoint="supportguard",
        arguments=["--help"],
    )
    if "{serve,db,knowledge,runtime,maintenance,demo}" not in help_output:
        raise RuntimeError("runtime image CLI help does not expose the runtime command set")
    if "eval" in help_output:
        raise RuntimeError("runtime image CLI unexpectedly exposes validation commands")
    return {
        "schema": "supportguard-runtime-image-smoke-result.v1",
        "status": "passed",
        "imports": imports["imports"],
        "runtime_cli": "passed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtime-image-contract")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory_parser = commands.add_parser("inventory")
    inventory_parser.add_argument("--image", required=True)
    inventory_parser.add_argument("--expected-revision")
    smoke_parser = commands.add_parser("smoke")
    smoke_parser.add_argument("--image", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "inventory":
        result = inventory(str(args.image), expected_revision=args.expected_revision)
    else:
        result = smoke(str(args.image))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
