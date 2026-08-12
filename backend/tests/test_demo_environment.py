from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "demo_environment.py"
    spec = importlib.util.spec_from_file_location("supportguard_demo_environment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    ["", "default", "supportguard", "other-project", "supportguard invalid"],
)
def test_project_mutations_require_an_explicit_supportguard_name(name: str) -> None:
    module = _load_module()
    with pytest.raises(module.EnvironmentContractError):
        module.validate_project_name(name)


def test_reset_requires_exact_project_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        module,
        "compose_command",
        lambda project, arguments, **_: calls.append((project, arguments)),
    )

    with pytest.raises(module.EnvironmentContractError):
        module.reset_project(
            "supportguard-v15ui",
            confirmed_project="supportguard-other",
            build=False,
        )

    assert calls == []


def test_reset_deletes_only_confirmed_project_then_starts_without_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        module,
        "compose_command",
        lambda project, arguments, **_: calls.append((project, arguments)),
    )

    module.reset_project(
        "supportguard-v15ui",
        confirmed_project="supportguard-v15ui",
        build=False,
    )

    assert calls == [
        ("supportguard-v15ui", ["down", "--remove-orphans", "--volumes"]),
        (
            "supportguard-v15ui",
            [
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "180",
                "--scale",
                "worker=2",
            ],
        ),
    ]


def test_start_always_uses_the_frozen_two_worker_demo_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    calls: list[tuple[list[str], object]] = []
    commit = "a" * 40
    monkeypatch.setattr(
        module,
        "build_project",
        lambda _project: {"code_commit": commit},
    )
    monkeypatch.setattr(
        module,
        "build_environment",
        lambda _project, **_: {"CODE_VERSION": commit},
    )
    monkeypatch.setattr(
        module,
        "compose_command",
        lambda _project, arguments, **kwargs: calls.append((arguments, kwargs.get("environment"))),
    )

    module.start_project("supportguard-v15ui", build=True)

    assert calls == [
        (
            [
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "180",
                "--scale",
                "worker=2",
            ],
            {"CODE_VERSION": commit},
        )
    ]


def test_failed_owned_build_transactionally_cleans_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    cleanup_calls: list[tuple[str, str | None]] = []
    commit = "a" * 40

    monkeypatch.setattr(module, "current_code_commit", lambda **_: commit)
    monkeypatch.setattr(module, "docker_cli", lambda: "docker")
    monkeypatch.setattr(module, "_resource_exists", lambda _arguments: False)
    monkeypatch.setattr(
        module,
        "cleanup_build",
        lambda project, *, confirmed_project: cleanup_calls.append((project, confirmed_project)),
    )

    def fail_backend_build(arguments: list[str], **_: object) -> object:
        if arguments[:3] == ["docker", "buildx", "build"]:
            raise subprocess.CalledProcessError(1, arguments)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(module, "_run", fail_backend_build)

    with pytest.raises(subprocess.CalledProcessError):
        module.build_project("supportguard-v1510-build-failure")

    assert cleanup_calls == [
        (
            "supportguard-v1510-build-failure",
            "supportguard-v1510-build-failure",
        )
    ]


def test_shared_daemon_build_requires_local_bases_and_uses_no_named_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    commit = "a" * 40
    base_images = {
        "python:3.11-slim",
        "node:22-alpine",
        "nginxinc/nginx-unprivileged:1.27-alpine",
    }
    calls: list[list[str]] = []

    monkeypatch.setenv("SUPPORTGUARD_BUILD_MODE", "shared-daemon-local-base")
    monkeypatch.setattr(module, "current_code_commit", lambda **_: commit)
    monkeypatch.setattr(module, "docker_cli", lambda: "docker")
    monkeypatch.setattr(
        module,
        "_resource_exists",
        lambda arguments: (
            arguments[1:3] == ["image", "inspect"]
            and arguments[-1] in base_images
        ),
    )
    monkeypatch.setattr(
        module,
        "_run",
        lambda arguments, **_: (
            calls.append(arguments)
            or subprocess.CompletedProcess(arguments, 0)
        ),
    )

    receipt = module.build_project("supportguard-v1510-local-build")

    assert receipt["build_mode"] == "shared-daemon-local-base"
    assert not any(call[:3] == ["docker", "buildx", "create"] for call in calls)
    builds = [call for call in calls if call[:2] == ["docker", "build"]]
    assert len(builds) == 2
    assert all("--pull=false" in call for call in builds)


def test_build_cleanup_contract_distinguishes_owned_and_shared_builders() -> None:
    module = _load_module()
    base = {
        "backend_image": "supportguard-backend:candidate",
        "frontend_image": "supportguard-frontend:candidate",
    }
    cleanup = {
        "removed_images": [
            "supportguard-backend:candidate",
            "supportguard-frontend:candidate",
        ],
        "builder_removed": True,
    }

    assert module.build_cleanup_is_clean(base, cleanup) is True
    shared = base | {"build_mode": "shared-daemon-local-base"}
    cleanup["builder_removed"] = False
    assert module.build_cleanup_is_clean(shared, cleanup) is True
    cleanup["removed_images"] = ["supportguard-backend:candidate"]
    assert module.build_cleanup_is_clean(shared, cleanup) is False


def test_teardown_preserves_volumes_without_delete_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "compose_command",
        lambda _project, arguments, **_: calls.append(arguments),
    )

    module.teardown_project(
        "supportguard-demo",
        delete_volumes=False,
        confirmed_project=None,
    )

    assert calls == [["down", "--remove-orphans"]]


def test_inventory_groups_only_supportguard_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "docker_cli", lambda: "/usr/bin/docker")
    outputs = iter(
        [
            subprocess.CompletedProcess(
                [],
                0,
                "a\tsg-worker\tRestarting (1)\tsupportguard-demo\nb\tother\tUp\tother-project\n",
                "",
            ),
            subprocess.CompletedProcess(
                [], 0, "supportguard-demo_db\tsupportguard-demo\nother_db\tother-project\n", ""
            ),
            subprocess.CompletedProcess([], 0, "Images 10GB", ""),
        ]
    )
    monkeypatch.setattr(module, "_run", lambda *_args, **_kwargs: next(outputs))

    result = module.inventory()

    assert list(result["projects"]) == ["supportguard-demo"]
    assert result["projects"]["supportguard-demo"]["restart_loop_count"] == 1
    assert result["projects"]["supportguard-demo"]["volumes"] == ["supportguard-demo_db"]


def test_cleanup_image_refuses_a_referenced_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "docker_cli", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "container-id\n", ""),
    )

    with pytest.raises(module.EnvironmentContractError, match="still_referenced"):
        module.cleanup_image(
            "supportguard-backend:verify-v152",
            confirmed_image="supportguard-backend:verify-v152",
        )


def test_cleanup_build_refuses_any_still_referenced_owned_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "docker_cli", lambda: "/usr/bin/docker")

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = "container-id\n" if arguments[:3] == ["/usr/bin/docker", "ps", "-a"] else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(module, "_run", run)
    with pytest.raises(module.EnvironmentContractError, match="still_referenced"):
        module.cleanup_build(
            "supportguard-v155-phase4",
            confirmed_project="supportguard-v155-phase4",
        )


def test_cleanup_build_removes_only_derived_images_and_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "docker_cli", lambda: "/usr/bin/docker")
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(module, "_run", run)
    result = module.cleanup_build(
        "supportguard-v155-phase4",
        confirmed_project="supportguard-v155-phase4",
    )

    assert result["removed_images"] == [
        "supportguard-backend:supportguard-v155-phase4",
        "supportguard-frontend:supportguard-v155-phase4",
    ]
    assert [
        "/usr/bin/docker",
        "buildx",
        "rm",
        "--force",
        "supportguard-v155-phase4-builder",
    ] in calls
    assert all("prune" not in call for call in calls)


def test_owned_build_forwards_configured_base_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "docker_cli", lambda: "/usr/bin/docker")
    monkeypatch.setattr(module, "current_code_commit", lambda: "a" * 40)
    monkeypatch.setattr(module, "_resource_exists", lambda _arguments: False)
    monkeypatch.setenv("PYTHON_BASE_IMAGE", "mirror.gcr.io/library/python:3.11-slim")
    monkeypatch.setenv("NODE_BASE_IMAGE", "mirror.gcr.io/library/node:22-alpine")
    monkeypatch.setenv(
        "NGINX_BASE_IMAGE",
        "mirror.gcr.io/nginxinc/nginx-unprivileged:1.27-alpine",
    )
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(module, "_run", run)

    result = module.build_project("supportguard-v155-phase4")

    backend = next(call for call in calls if "backend/Dockerfile" in call)
    frontend = next(call for call in calls if "frontend/Dockerfile" in call)
    assert "PYTHON_BASE_IMAGE=mirror.gcr.io/library/python:3.11-slim" in backend
    assert "NODE_BASE_IMAGE=mirror.gcr.io/library/node:22-alpine" in frontend
    assert "NGINX_BASE_IMAGE=mirror.gcr.io/nginxinc/nginx-unprivileged:1.27-alpine" in frontend
    assert result["python_base_image"] == "mirror.gcr.io/library/python:3.11-slim"


def test_receipt_only_build_can_reuse_a_runtime_identical_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    requested = "a" * 40
    head = "b" * 40

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[:3] == ["git", "status", "--porcelain"]:
            output = ""
        elif arguments[:3] == ["git", "rev-parse", "HEAD"]:
            output = head + "\n"
        else:
            output = ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(module, "_run", run)

    assert module.current_code_commit(requested_commit=requested) == requested


def test_receipt_only_build_rejects_runtime_bearing_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    requested = "a" * 40

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        returncode = 1 if arguments[:3] == ["git", "diff", "--quiet"] else 0
        output = "b" * 40 + "\n" if arguments[:3] == ["git", "rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(arguments, returncode, output, "")

    monkeypatch.setattr(module, "_run", run)

    with pytest.raises(
        module.EnvironmentContractError,
        match="runtime_differs_from_requested_commit",
    ):
        module.current_code_commit(requested_commit=requested)
