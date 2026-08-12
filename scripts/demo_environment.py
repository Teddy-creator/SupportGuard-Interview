"""Own the lifecycle of a named local SupportGuard Compose demo project.

The command is deliberately narrower than Docker prune: it can inspect all
SupportGuard projects, but mutating commands require an explicit project name.
Volume deletion and temporary image deletion require a second exact-name
confirmation.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = "supportguard-v15ui"
PROJECT_PATTERN = re.compile(r"supportguard[a-z0-9_-]{2,52}\Z")
IMAGE_PATTERN = re.compile(r"supportguard[-_][a-z0-9._:/-]{1,120}\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
RUNTIME_BEARING_PATHS = (
    "backend/src",
    "backend/alembic_baseline",
    "backend/Dockerfile",
    "frontend/src",
    "frontend/index.html",
    "frontend/nginx.conf",
    "frontend/Dockerfile",
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
    "knowledge",
    "docker-compose.yml",
    "alembic-interview.ini",
    "pyproject.toml",
    "uv.lock",
)
BUILD_MODES = ("owned-builder", "shared-daemon-local-base")


class EnvironmentContractError(RuntimeError):
    pass


def validate_project_name(value: str) -> str:
    project = value.strip().lower()
    if project in {"supportguard", "supportguard_default", "default"}:
        raise EnvironmentContractError("demo_project_name_is_ambiguous")
    if PROJECT_PATTERN.fullmatch(project) is None:
        raise EnvironmentContractError("demo_project_name_must_be_explicit_supportguard_name")
    return project


def validate_image_name(value: str) -> str:
    image = value.strip()
    if IMAGE_PATTERN.fullmatch(image) is None:
        raise EnvironmentContractError("cleanup_image_must_be_explicit_supportguard_tag")
    return image


def docker_cli() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise EnvironmentContractError("docker_cli_not_found")
    return executable


def _run(
    arguments: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        arguments,
        cwd=ROOT,
        check=check,
        capture_output=capture_output,
        text=True,
        env=dict(environment) if environment is not None else None,
    )


def _tsv_records(output: str, *, fields: tuple[str, ...]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        values = line.split("\t")
        values.extend([""] * (len(fields) - len(values)))
        records.append(dict(zip(fields, values, strict=True)))
    return records


def inventory() -> dict[str, Any]:
    docker = docker_cli()
    containers = _tsv_records(
        _run(
            [
                docker,
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}',
            ],
            capture_output=True,
        ).stdout,
        fields=("id", "name", "status", "project"),
    )
    volumes = _tsv_records(
        _run(
            [
                docker,
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                '{{.Name}}\t{{.Label "com.docker.compose.project"}}',
            ],
            capture_output=True,
        ).stdout,
        fields=("name", "project"),
    )
    projects: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"containers": [], "volumes": [], "restart_loop_count": 0}
    )
    for item in containers:
        project = item["project"]
        if not project.startswith("supportguard"):
            continue
        projects[project]["containers"].append(item)
        if item["status"].lower().startswith("restarting"):
            projects[project]["restart_loop_count"] += 1
    for item in volumes:
        project = item["project"]
        if project.startswith("supportguard"):
            projects[project]["volumes"].append(item["name"])
    system_df = _run([docker, "system", "df"], check=False, capture_output=True)
    return {
        "schema": "supportguard-demo-environment-inventory.v1",
        "projects": dict(sorted(projects.items())),
        "docker_system_df": system_df.stdout.strip(),
        "docker_system_df_available": system_df.returncode == 0,
    }


def compose_command(
    project: str,
    arguments: list[str],
    *,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [docker_cli(), "compose", "-p", validate_project_name(project), *arguments],
        check=check,
        environment=environment,
    )


def require_confirmation(*, actual: str, confirmed: str | None, kind: str) -> None:
    if confirmed is None or confirmed != actual:
        raise EnvironmentContractError(f"{kind}_confirmation_must_exactly_match")


def build_ownership(project: str) -> dict[str, str]:
    validated = validate_project_name(project)
    return {
        "project": validated,
        "builder": f"{validated}-builder",
        "backend_image": f"supportguard-backend:{validated}",
        "frontend_image": f"supportguard-frontend:{validated}",
    }


def current_code_commit(*, requested_commit: str | None = None) -> str:
    dirty = _run(["git", "status", "--porcelain"], capture_output=True).stdout.strip()
    if dirty:
        raise EnvironmentContractError("owned_build_requires_clean_git_worktree")
    head = _run(["git", "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    commit = requested_commit or head
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise EnvironmentContractError("owned_build_requires_full_git_commit")
    if requested_commit is not None:
        exists = _run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            capture_output=True,
        ).returncode
        if exists:
            raise EnvironmentContractError("owned_build_requested_commit_missing")
        runtime_diff = _run(
            ["git", "diff", "--quiet", commit, "--", *RUNTIME_BEARING_PATHS],
            check=False,
            capture_output=True,
        ).returncode
        if runtime_diff:
            raise EnvironmentContractError("owned_build_runtime_differs_from_requested_commit")
    return commit


def _resource_exists(arguments: list[str]) -> bool:
    return _run(arguments, check=False, capture_output=True).returncode == 0


def build_environment(project: str, *, code_commit: str) -> dict[str, str]:
    if COMMIT_PATTERN.fullmatch(code_commit) is None:
        raise EnvironmentContractError("owned_build_requires_full_git_commit")
    ownership = build_ownership(project)
    environment = dict(os.environ)
    environment.update(
        {
            "BACKEND_IMAGE": ownership["backend_image"],
            "FRONTEND_IMAGE": ownership["frontend_image"],
            "CODE_VERSION": code_commit,
        }
    )
    return environment


def configured_base_image(variable: str, default: str) -> str:
    value = os.environ.get(variable, default).strip()
    if not value or any(character.isspace() for character in value):
        raise EnvironmentContractError(f"owned_build_invalid_base_image:{variable}")
    return value


def configured_build_mode() -> str:
    mode = os.environ.get("SUPPORTGUARD_BUILD_MODE", "owned-builder").strip()
    if mode not in BUILD_MODES:
        raise EnvironmentContractError("owned_build_mode_invalid")
    return mode


def build_project(project: str, *, code_commit: str | None = None) -> dict[str, str]:
    ownership = build_ownership(project)
    commit = (
        current_code_commit()
        if code_commit is None
        else current_code_commit(requested_commit=code_commit)
    )
    docker = docker_cli()
    builder = ownership["builder"]
    images = (ownership["backend_image"], ownership["frontend_image"])
    python_base_image = configured_base_image("PYTHON_BASE_IMAGE", "python:3.11-slim")
    node_base_image = configured_base_image("NODE_BASE_IMAGE", "node:22-alpine")
    nginx_base_image = configured_base_image(
        "NGINX_BASE_IMAGE", "nginxinc/nginx-unprivileged:1.27-alpine"
    )
    build_mode = configured_build_mode()
    if _resource_exists([docker, "buildx", "inspect", builder]):
        raise EnvironmentContractError("owned_builder_already_exists")
    if any(_resource_exists([docker, "image", "inspect", image]) for image in images):
        raise EnvironmentContractError("owned_build_image_already_exists")
    if build_mode == "shared-daemon-local-base":
        base_images = (python_base_image, node_base_image, nginx_base_image)
        if not all(_resource_exists([docker, "image", "inspect", image]) for image in base_images):
            raise EnvironmentContractError("shared_daemon_build_requires_local_base_images")

    if build_mode == "owned-builder":
        _run(
            [
                docker,
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
            ]
        )
    try:
        common = [docker]
        if build_mode == "owned-builder":
            common.extend(
                [
                    "buildx",
                    "build",
                    "--builder",
                    builder,
                    "--load",
                ]
            )
        else:
            common.extend(["build", "--pull=false"])
        common.extend(
            [
                "--label",
                f"io.supportguard.validation.project={ownership['project']}",
                "--label",
                f"org.opencontainers.image.revision={commit}",
                "--build-arg",
                f"CODE_VERSION={commit}",
            ]
        )
        _run(
            [
                *common,
                "--file",
                "backend/Dockerfile",
                "--tag",
                ownership["backend_image"],
                "--build-arg",
                f"PYTHON_BASE_IMAGE={python_base_image}",
                ".",
            ]
        )
        _run(
            [
                *common,
                "--file",
                "frontend/Dockerfile",
                "--tag",
                ownership["frontend_image"],
                "--build-arg",
                f"NODE_BASE_IMAGE={node_base_image}",
                "--build-arg",
                f"NGINX_BASE_IMAGE={nginx_base_image}",
                "frontend",
            ]
        )
    except BaseException:
        # A failed Buildx bootstrap/build must not strand the named builder or
        # a partially loaded image.  Preserve the original failure while
        # making the owned resource lifecycle transactional.
        with contextlib.suppress(Exception):
            cleanup_build(
                ownership["project"],
                confirmed_project=ownership["project"],
            )
        raise
    return {
        **ownership,
        "code_commit": commit,
        "python_base_image": python_base_image,
        "node_base_image": node_base_image,
        "nginx_base_image": nginx_base_image,
        "build_mode": build_mode,
    }


def cleanup_build(project: str, *, confirmed_project: str | None) -> dict[str, Any]:
    ownership = build_ownership(project)
    require_confirmation(
        actual=ownership["project"],
        confirmed=confirmed_project,
        kind="cleanup_build_project",
    )
    docker = docker_cli()
    images = (ownership["backend_image"], ownership["frontend_image"])
    for image in images:
        references = _run(
            [docker, "ps", "-a", "--filter", f"ancestor={image}", "--quiet"],
            capture_output=True,
        ).stdout.split()
        if references:
            raise EnvironmentContractError("cleanup_build_image_is_still_referenced")

    removed_images: list[str] = []
    for image in images:
        if _resource_exists([docker, "image", "inspect", image]):
            _run([docker, "image", "rm", image])
            removed_images.append(image)
    builder_removed = _resource_exists([docker, "buildx", "inspect", ownership["builder"]])
    if builder_removed:
        _run([docker, "buildx", "rm", "--force", ownership["builder"]])
    return {
        "schema": "supportguard-owned-build-cleanup.v1",
        "project": ownership["project"],
        "builder": ownership["builder"],
        "builder_removed": builder_removed,
        "removed_images": removed_images,
    }


def build_cleanup_is_clean(
    build_receipt: Mapping[str, str],
    cleanup: Mapping[str, Any],
) -> bool:
    expected_images = {
        build_receipt.get("backend_image"),
        build_receipt.get("frontend_image"),
    }
    removed_images = set(cleanup.get("removed_images") or [])
    if not expected_images <= removed_images:
        return False
    if build_receipt.get("build_mode", "owned-builder") == "owned-builder":
        return cleanup.get("builder_removed") is True
    return (
        build_receipt.get("build_mode") == "shared-daemon-local-base"
        and cleanup.get("builder_removed") is False
    )


def start_project(project: str, *, build: bool) -> None:
    validated = validate_project_name(project)
    environment: Mapping[str, str] | None = None
    if build:
        built = build_project(validated)
        environment = build_environment(validated, code_commit=built["code_commit"])
    arguments = [
        "up",
        "-d",
        "--no-build",
        "--wait",
        "--wait-timeout",
        "180",
    ]
    arguments.extend(("--scale", "worker=2"))
    compose_command(validated, arguments, environment=environment)


def stop_project(project: str) -> None:
    compose_command(project, ["stop"])


def teardown_project(
    project: str,
    *,
    delete_volumes: bool,
    confirmed_project: str | None,
) -> None:
    validated = validate_project_name(project)
    arguments = ["down", "--remove-orphans"]
    if delete_volumes:
        require_confirmation(
            actual=validated,
            confirmed=confirmed_project,
            kind="volume_delete_project",
        )
        arguments.append("--volumes")
    compose_command(validated, arguments)


def reset_project(
    project: str,
    *,
    confirmed_project: str | None,
    build: bool,
) -> None:
    validated = validate_project_name(project)
    require_confirmation(
        actual=validated,
        confirmed=confirmed_project,
        kind="reset_project",
    )
    teardown_project(
        validated,
        delete_volumes=True,
        confirmed_project=confirmed_project,
    )
    start_project(validated, build=build)


def cleanup_image(image: str, *, confirmed_image: str | None) -> None:
    validated = validate_image_name(image)
    require_confirmation(
        actual=validated,
        confirmed=confirmed_image,
        kind="cleanup_image",
    )
    docker = docker_cli()
    references = _run(
        [docker, "ps", "-a", "--filter", f"ancestor={validated}", "--quiet"],
        capture_output=True,
    ).stdout.split()
    if references:
        raise EnvironmentContractError("cleanup_image_is_still_referenced")
    inspected = _run(
        [docker, "image", "inspect", validated],
        check=False,
        capture_output=True,
    )
    if inspected.returncode != 0:
        raise EnvironmentContractError("cleanup_image_not_found")
    _run([docker, "image", "rm", validated])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("inventory")
    for name in ("start", "stop", "teardown", "reset"):
        command = subcommands.add_parser(name)
        command.add_argument(
            "--project",
            default=os.environ.get("COMPOSE_PROJECT_NAME", DEFAULT_PROJECT),
        )
        if name in {"start", "reset"}:
            command.add_argument("--build", action="store_true")
        if name == "teardown":
            command.add_argument("--delete-volumes", action="store_true")
            command.add_argument("--confirm-project")
        if name == "reset":
            command.add_argument("--confirm-project", required=True)
    cleanup = subcommands.add_parser("cleanup-image")
    cleanup.add_argument("--image", required=True)
    cleanup.add_argument("--confirm-image", required=True)
    cleanup_build_parser = subcommands.add_parser("cleanup-build")
    cleanup_build_parser.add_argument("--project", required=True)
    cleanup_build_parser.add_argument("--confirm-project", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "inventory":
            print(json.dumps(inventory(), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "start":
            start_project(args.project, build=bool(args.build))
        elif args.command == "stop":
            stop_project(args.project)
        elif args.command == "teardown":
            teardown_project(
                args.project,
                delete_volumes=bool(args.delete_volumes),
                confirmed_project=args.confirm_project,
            )
        elif args.command == "reset":
            reset_project(
                args.project,
                confirmed_project=args.confirm_project,
                build=bool(args.build),
            )
        elif args.command == "cleanup-image":
            cleanup_image(args.image, confirmed_image=args.confirm_image)
        elif args.command == "cleanup-build":
            print(
                json.dumps(
                    cleanup_build(
                        args.project,
                        confirmed_project=args.confirm_project,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except (EnvironmentContractError, subprocess.CalledProcessError) as exc:
        print(f"SupportGuard demo environment error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
