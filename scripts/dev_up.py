"""Build application images explicitly, then start Compose without implicit builds."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PORTS = {
    "POSTGRES_HOST_PORT": 5432,
    "REDIS_HOST_PORT": 6379,
    "API_HOST_PORT": 8000,
    "FRONTEND_HOST_PORT": 5173,
}
DEFAULT_APPLICATION_IMAGES = (
    "supportguard-backend:local",
    "supportguard-frontend:local",
)
DEFAULT_COMPOSE_PROJECT = "supportguard-v15ui"


def port_is_occupied(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def compose_project_name() -> str:
    project = os.environ.get("COMPOSE_PROJECT_NAME", DEFAULT_COMPOSE_PROJECT).strip().lower()
    normalized = project.replace("-", "").replace("_", "")
    if not project.startswith("supportguard") or not normalized.isalnum():
        raise ValueError("COMPOSE_PROJECT_NAME must be an explicit SupportGuard project")
    return project


def compose_owned_ports(docker: str, project: str) -> set[int]:
    result = subprocess.run(  # noqa: S603 - resolved Docker CLI, fixed arguments
        [docker, "compose", "-p", project, "ps", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    records: list[dict[str, Any]] = []
    try:
        parsed = json.loads(result.stdout)
        records = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return {
        int(publisher["PublishedPort"])
        for record in records
        for publisher in record.get("Publishers", [])
        if isinstance(publisher, dict) and publisher.get("PublishedPort")
    }


def application_images() -> tuple[str, str]:
    return (
        os.environ.get("BACKEND_IMAGE", DEFAULT_APPLICATION_IMAGES[0]),
        os.environ.get("FRONTEND_IMAGE", DEFAULT_APPLICATION_IMAGES[1]),
    )


def images_available(docker: str, images: tuple[str, str]) -> bool:
    result = subprocess.run(  # noqa: S603 - resolved Docker CLI, exact image names
        [docker, "image", "inspect", *images],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def build_images(docker: str, project: str) -> None:
    subprocess.run(  # noqa: S603 - resolved Docker CLI, fixed Compose command
        [docker, "compose", "-p", project, "build"],
        cwd=ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="explicitly rebuild application images before starting",
    )
    mode.add_argument(
        "--build-only",
        action="store_true",
        help="build application images and do not start Compose",
    )
    args = parser.parse_args(argv)
    docker = shutil.which("docker")
    if docker is None:
        print("SupportGuard 未启动：找不到 Docker CLI。", file=sys.stderr)
        return 2
    try:
        project = compose_project_name()
    except ValueError as exc:
        print(f"SupportGuard 未启动：{exc}", file=sys.stderr)
        return 2
    images = application_images()
    if args.rebuild or args.build_only or not images_available(docker, images):
        reason = "显式重建" if args.rebuild or args.build_only else "首次运行缺少应用镜像"
        print(f"SupportGuard {reason}：构建 Backend 与 Frontend 镜像。")
        try:
            build_images(docker, project)
        except subprocess.CalledProcessError as exc:
            print(f"SupportGuard 镜像构建失败（退出码 {exc.returncode}）。", file=sys.stderr)
            return 1
    if args.build_only:
        print("SupportGuard 应用镜像已构建；未启动服务。")
        return 0
    configured = {
        name: int(os.environ.get(name, default)) for name, default in PORTS.items()
    }
    owned = compose_owned_ports(docker, project)
    conflicts = [
        (name, port)
        for name, port in configured.items()
        if port_is_occupied(port) and port not in owned
    ]
    if conflicts:
        print("SupportGuard 未启动：以下端口已被其他进程占用：", file=sys.stderr)
        for name, port in conflicts:
            print(f"  - {name}={port}", file=sys.stderr)
        print(
            "请停止占用进程，或用环境变量改端口，例如 "
            "FRONTEND_HOST_PORT=5174 API_HOST_PORT=8001 make dev",
            file=sys.stderr,
        )
        return 2
    os.chdir(ROOT)
    os.execv(  # noqa: S606
        docker,
        [docker, "compose", "-p", project, "up", "--no-build"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
