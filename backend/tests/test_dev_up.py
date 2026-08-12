from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_dev_up() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "dev_up.py"
    spec = importlib.util.spec_from_file_location("supportguard_dev_up", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normal_start_reuses_images_and_never_implicitly_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_dev_up()
    builds: list[str] = []
    executions: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(module, "images_available", lambda *_: True)
    monkeypatch.setattr(module, "build_images", lambda *_: builds.append("build"))
    monkeypatch.setattr(module, "compose_owned_ports", lambda *_: set())
    monkeypatch.setattr(module, "port_is_occupied", lambda _: False)
    monkeypatch.setattr(module.os, "chdir", lambda _: None)
    monkeypatch.setattr(
        module.os,
        "execv",
        lambda executable, argv: executions.append((executable, argv)),
    )

    assert module.main([]) == 0
    assert builds == []
    assert executions == [
        (
            "/usr/local/bin/docker",
            [
                "/usr/local/bin/docker",
                "compose",
                "-p",
                "supportguard-v15ui",
                "up",
                "--no-build",
            ],
        )
    ]


@pytest.mark.parametrize("arguments", [["--rebuild"], ["--build-only"]])
def test_explicit_build_modes_build_exactly_once(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_dev_up()
    builds: list[str] = []
    executions: list[list[str]] = []
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(module, "images_available", lambda *_: True)
    monkeypatch.setattr(module, "build_images", lambda *_: builds.append("build"))
    monkeypatch.setattr(module, "compose_owned_ports", lambda *_: set())
    monkeypatch.setattr(module, "port_is_occupied", lambda _: False)
    monkeypatch.setattr(module.os, "chdir", lambda _: None)
    monkeypatch.setattr(module.os, "execv", lambda _, argv: executions.append(argv))

    assert module.main(arguments) == 0
    assert builds == ["build"]
    if arguments == ["--build-only"]:
        assert executions == []
    else:
        assert executions == [
            [
                "/usr/local/bin/docker",
                "compose",
                "-p",
                "supportguard-v15ui",
                "up",
                "--no-build",
            ]
        ]


def test_first_start_builds_missing_images_before_no_build_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_dev_up()
    sequence: list[str] = []
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(module, "images_available", lambda *_: False)
    monkeypatch.setattr(module, "build_images", lambda *_: sequence.append("build"))
    monkeypatch.setattr(module, "compose_owned_ports", lambda *_: set())
    monkeypatch.setattr(module, "port_is_occupied", lambda _: False)
    monkeypatch.setattr(module.os, "chdir", lambda _: None)
    monkeypatch.setattr(module.os, "execv", lambda *_: sequence.append("up"))

    assert module.main([]) == 0
    assert sequence == ["build", "up"]


def test_development_project_name_is_stable_across_worktree_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_dev_up()
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    assert module.compose_project_name() == "supportguard-v15ui"


def test_non_supportguard_project_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_dev_up()
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "unrelated-project")
    with pytest.raises(ValueError):
        module.compose_project_name()


def test_compose_has_one_backend_build_owner() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert compose.count("dockerfile: backend/Dockerfile") == 1
    api_build_owner = (
        "api:\n    <<: *backend\n"
        "    # The API owns the single shared Backend image build"
    )
    assert api_build_owner in compose


def test_backend_dependency_layer_is_not_invalidated_by_readme_changes() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "backend" / "Dockerfile").read_text()
    pyproject = (root / "pyproject.toml").read_text()
    dependency_copy = dockerfile.index("COPY pyproject.toml uv.lock ./")
    dependency_install = dockerfile.index("uv sync --locked")
    readme_copy = dockerfile.index("COPY README.md ./")
    source_copy = dockerfile.index("COPY backend/src ./backend/src")
    project_install = dockerfile.index(
        "uv pip install --python /app/runtime-venv/bin/python"
    )
    assert dependency_copy < dependency_install < readme_copy < source_copy < project_install
    assert "UV_PROJECT_ENVIRONMENT=/app/build-venv" in dockerfile[dependency_install:readme_copy]
    assert "--no-build-isolation" in dockerfile[source_copy:project_install]
    assert "--no-deps --no-build" in dockerfile[project_install:]
    assert 'build = [\n  "hatchling>=1.27,<2",' in pyproject
    assert "--mount=type=cache,target=/root/.cache/huggingface" in dockerfile
    assert "cp -a /root/.cache/huggingface /app/.cache/huggingface" in dockerfile
    runtime_stage = dockerfile.split("FROM ${PYTHON_BASE_IMAGE} AS runtime", 1)[1]
    assert "COPY backend/src" not in runtime_stage
    assert "COPY --from=builder /app/runtime-venv" in runtime_stage


def test_compose_builds_accept_registry_mirror_base_images_without_changing_defaults() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text()
    backend = (root / "backend" / "Dockerfile").read_text()
    frontend = (root / "frontend" / "Dockerfile").read_text()

    assert "ARG PYTHON_BASE_IMAGE=python:3.11-slim" in backend
    assert backend.count("FROM ${PYTHON_BASE_IMAGE}") == 2
    assert "PYTHON_BASE_IMAGE: ${PYTHON_BASE_IMAGE:-python:3.11-slim}" in compose

    assert "ARG NODE_BASE_IMAGE=node:22-alpine" in frontend
    assert (
        "ARG NGINX_BASE_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine" in frontend
    )
    assert "FROM ${NODE_BASE_IMAGE} AS builder" in frontend
    assert "FROM ${NGINX_BASE_IMAGE}" in frontend
    assert "NODE_BASE_IMAGE: ${NODE_BASE_IMAGE:-node:22-alpine}" in compose
    assert (
        "NGINX_BASE_IMAGE: "
        "${NGINX_BASE_IMAGE:-nginxinc/nginx-unprivileged:1.27-alpine}" in compose
    )
