from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "supportguard_process_faults", ROOT / "scripts/process_faults.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _workers(health: str) -> list[dict[str, object]]:
    return [
        {
            "Name": f"worker-{index}",
            "Service": "worker",
            "State": "running",
            "Health": health,
            "ExitCode": 0,
        }
        for index in (1, 2)
    ]


def test_worker_rows_accepts_compose_json_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    output = "\n".join(
        [
            '{"Name":"worker-1","State":"running","Health":"healthy"}',
            '{"Name":"worker-2","State":"running","Health":"healthy"}',
        ]
    )
    monkeypatch.setattr(module, "command_output", lambda *_args: output)

    assert [row["Name"] for row in module.worker_rows()] == ["worker-1", "worker-2"]


def test_worker_rows_accepts_compose_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "command_output", lambda *_args: json.dumps(_workers("healthy")))

    assert module.worker_rows() == _workers("healthy")


def test_worker_convergence_waits_through_transient_unhealthy_state() -> None:
    module = _module()
    states = iter([_workers("unhealthy"), _workers("healthy")])
    sleeps: list[float] = []

    module.wait_for_worker_health(
        row_loader=lambda: next(states),
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert sleeps == [module.WORKER_CONVERGENCE_POLL_SECONDS]


def test_worker_convergence_fails_closed_after_bounded_timeout() -> None:
    module = _module()
    times = iter([0.0, module.WORKER_CONVERGENCE_TIMEOUT_SECONDS])

    with pytest.raises(RuntimeError, match="workers did not converge before timeout"):
        module.wait_for_worker_health(
            row_loader=lambda: _workers("unhealthy"),
            clock=lambda: next(times),
            sleeper=lambda _seconds: None,
        )


def test_worker_convergence_rejects_extra_replica_without_retry() -> None:
    module = _module()
    rows = _workers("healthy") + [
        {
            "Name": "worker-3",
            "Service": "worker",
            "State": "running",
            "Health": "healthy",
            "ExitCode": 0,
        }
    ]
    sleeps: list[float] = []

    with pytest.raises(RuntimeError, match="unexpected worker replica count"):
        module.wait_for_worker_health(
            row_loader=lambda: rows,
            clock=lambda: 0.0,
            sleeper=sleeps.append,
        )

    assert sleeps == []


def test_worker_readiness_waits_for_identity_bound_fresh_heartbeats() -> None:
    module = _module()
    identities = [
        {"hostname": "worker-a"},
        {"hostname": "worker-b"},
    ]
    snapshots = iter(
        [
            {"rows": []},
            {
                "rows": [
                    {"instance_id": "worker-a", "fresh_ready": True},
                    {"instance_id": "worker-b", "fresh_ready": True},
                ]
            },
        ]
    )
    sleeps: list[float] = []

    transitions = module.wait_for_worker_ready_heartbeats(
        identity_loader=lambda: identities,
        snapshot_loader=lambda _instance_ids: next(snapshots),
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert sleeps == [module.WORKER_CONVERGENCE_POLL_SECONDS]
    assert transitions[-1]["worker_heartbeat"] == {
        "instance_ids": ["worker-a", "worker-b"],
        "ready_ids": ["worker-a", "worker-b"],
    }


def test_worker_readiness_fails_with_specific_code_inside_bounded_timeout() -> None:
    module = _module()
    times = iter([0.0, module.WORKER_CONVERGENCE_TIMEOUT_SECONDS])

    with pytest.raises(module.WorkerHealthError) as exc_info:
        module.wait_for_worker_ready_heartbeats(
            identity_loader=lambda: [
                {"hostname": "worker-a"},
                {"hostname": "worker-b"},
            ],
            snapshot_loader=lambda _instance_ids: {"rows": []},
            clock=lambda: next(times),
            sleeper=lambda _seconds: None,
        )

    assert exc_info.value.code == "worker_heartbeat_timeout"


def test_worker_convergence_does_not_delegate_readiness_to_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[tuple[str, ...]] = []
    waited = 0
    heartbeat_timeouts: list[float] = []
    times = iter([100.0, 120.0])

    def wait_for_health(**_kwargs: object) -> list[dict[str, object]]:
        nonlocal waited
        waited += 1
        return []

    def wait_for_heartbeats(**kwargs: object) -> list[dict[str, object]]:
        nonlocal waited
        waited += 1
        heartbeat_timeouts.append(float(kwargs["timeout_seconds"]))
        return []

    monkeypatch.setattr(module, "compose", lambda *args: calls.append(args))
    monkeypatch.setattr(module, "wait_for_worker_health", wait_for_health)
    monkeypatch.setattr(
        module,
        "wait_for_worker_ready_heartbeats",
        wait_for_heartbeats,
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))

    module.converge_workers(force_recreate=True)

    assert calls == [
        ("up", "-d", "--no-deps", "--force-recreate", "--scale", "worker=2", "worker")
    ]
    assert "--wait" not in calls[0]
    assert waited == 2
    assert heartbeat_timeouts == [module.WORKER_CONVERGENCE_TIMEOUT_SECONDS - 20.0]


def test_failure_path_writes_redacted_append_only_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    path = tmp_path / "process-faults.json"
    monkeypatch.setenv("PROCESS_FAULT_REPORT_PATH", str(path))
    monkeypatch.setattr(
        module,
        "execution_identity",
        lambda: {
            "tested_code_commit": "a" * 40,
            "tested_tree": "b" * 40,
            "runner_or_image_digest": "sha256:" + "c" * 64,
        },
    )
    diagnostic = {
        "outcome": "worker_healthcheck_failed",
        "worker_logs": [{"unstructured_text_retained": False}],
    }

    async def fail_convergence(*, force_recreate: bool = False) -> dict[str, object]:
        assert force_recreate is True
        raise module.DiagnosedWorkerConvergenceError(diagnostic)

    monkeypatch.setattr(module, "diagnosed_converge_workers", fail_convergence)

    with pytest.raises(RuntimeError, match="worker_convergence_failed") as exc_info:
        module.asyncio.run(module.main())

    report = json.loads(path.read_text())
    assert report["status"] == "failed"
    assert report["worker_force_recreate_diagnostic"] == diagnostic
    assert report["failure"] == {
        "stage": "worker_force_recreate",
        "error_code": "worker_convergence_failed:worker_healthcheck_failed",
    }
    assert f"artifact={path}" in str(exc_info.value)
    assert "sha256=" in str(exc_info.value)
    with pytest.raises(RuntimeError, match="append-only"):
        module.write_process_fault_report(report)


def test_convergence_only_writes_preflight_without_customer_or_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    path = tmp_path / "preflight.json"
    monkeypatch.setenv("PROCESS_FAULT_REPORT_PATH", str(path))
    monkeypatch.setenv("PROCESS_FAULT_CONVERGENCE_ONLY", "true")
    monkeypatch.setattr(
        module,
        "execution_identity",
        lambda: {
            "tested_code_commit": "a" * 40,
            "tested_tree": "b" * 40,
            "runner_or_image_digest": "sha256:" + "c" * 64,
        },
    )

    async def pass_convergence(*, force_recreate: bool = False) -> dict[str, object]:
        assert force_recreate is True
        return {"outcome": "contract_pass"}

    async def forbidden_customer_client() -> tuple[object, str]:
        raise AssertionError("convergence-only mode must not create a customer session")

    monkeypatch.setattr(module, "diagnosed_converge_workers", pass_convergence)
    monkeypatch.setattr(module, "customer_client", forbidden_customer_client)

    module.asyncio.run(module.main())

    report = json.loads(path.read_text())
    assert report["schema_version"] == "worker-convergence-preflight.v1"
    assert report["status"] == "passed"
    assert report["worker_convergence"] == {"outcome": "contract_pass"}
    assert "redis_recovery" not in report
    assert "worker_kill" not in report


def test_worker_kill_uses_strict_force_recreate_epoch_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    convergence_calls: list[bool] = []
    wait_results = iter(
        [
            SimpleNamespace(
                lease_owner="worker-exact-owner",
                fencing_token=7,
                status="leased",
            ),
            SimpleNamespace(
                lease_owner="replacement-owner",
                fencing_token=8,
                status="succeeded",
            ),
        ]
    )

    async def converge(*, force_recreate: bool = False) -> dict[str, object]:
        convergence_calls.append(force_recreate)
        return {
            "outcome": "contract_pass",
            "identities_replaced": True,
            "facts": {"restart_count_total": 0},
        }

    async def submit(_client: object, _csrf: str, label: str) -> dict[str, str]:
        assert label == "worker-kill"
        return {"job_id": "job-1", "run_id": "run-1"}

    async def wait_job(
        job_id: str, statuses: set[str], timeout_seconds: float
    ) -> SimpleNamespace:
        assert job_id == "job-1"
        assert timeout_seconds in {60, 120}
        assert statuses in ({"leased"}, {"succeeded", "dead"})
        return next(wait_results)

    monkeypatch.setattr(module, "diagnosed_converge_workers", converge)
    monkeypatch.setattr(module, "submit", submit)
    monkeypatch.setattr(module, "wait_job", wait_job)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda args, check: module.subprocess.CompletedProcess(args, 0),
    )

    result = module.asyncio.run(module.worker_kill(object(), "csrf"))

    assert convergence_calls == [True, True]
    assert result["killed_owner"] == "worker-exact-owner"
    assert result["stale_fence_rejected"] is True
    assert result["epoch_reset_contract"] == "force-recreate-new-identity.v1"
    assert result["before_kill_convergence"]["outcome"] == "contract_pass"
    assert result["after_kill_convergence"]["outcome"] == "contract_pass"


def test_diagnostic_text_never_retains_free_form_or_secret_like_values() -> None:
    module = _module()

    assert module._safe_diagnostic_text("worker.started") == "worker.started"
    assert module._safe_diagnostic_text("customer said my token is abc").startswith(
        "sha256:"
    )
