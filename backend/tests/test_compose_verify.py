from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "supportguard_compose_verify", ROOT / "scripts/compose_verify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_mcp_readiness_waits_for_missing_children_to_converge() -> None:
    module = _module()
    rows = [{"ID": "worker-1"}, {"ID": "worker-2"}]
    calls = 0
    sleeps: list[float] = []

    def runner(*_args: str) -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return "python -m supportguard.mcp.read_server\n"
        return (
            "python -m supportguard.mcp.read_server\n"
            "python -m supportguard.mcp.action_server\n"
        )

    module.wait_for_worker_mcp_children(
        rows,
        runner=runner,
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert calls == 4
    assert sleeps == [module.MCP_READY_POLL_SECONDS]


def test_worker_mcp_readiness_rejects_duplicate_children_without_retry() -> None:
    module = _module()
    sleeps: list[float] = []

    with pytest.raises(RuntimeError, match="duplicate action MCP"):
        module.wait_for_worker_mcp_children(
            [{"ID": "worker-1"}],
            runner=lambda *_args: (
                "python -m supportguard.mcp.read_server\n"
                "python -m supportguard.mcp.action_server\n"
                "python -m supportguard.mcp.action_server\n"
            ),
            clock=lambda: 0.0,
            sleeper=sleeps.append,
        )

    assert sleeps == []


def test_worker_mcp_readiness_fails_closed_after_bounded_timeout() -> None:
    module = _module()
    times = iter([0.0, module.MCP_READY_TIMEOUT_SECONDS])

    with pytest.raises(RuntimeError, match="did not converge before timeout"):
        module.wait_for_worker_mcp_children(
            [{"ID": "worker-1"}],
            runner=lambda *_args: "python -m supportguard.mcp.read_server\n",
            clock=lambda: next(times),
            sleeper=lambda _seconds: None,
        )


def _dependency_snapshot(ready_instances: object) -> dict[str, object]:
    return {
        "dependencies": {
            "postgres": {"status": "healthy"},
            "workers": {"ready_instances": ready_instances},
        }
    }


def test_worker_heartbeat_readiness_waits_for_stale_instance_to_expire() -> None:
    module = _module()
    snapshots = iter([_dependency_snapshot(3), _dependency_snapshot(2)])
    sleeps: list[float] = []

    result = module.wait_for_worker_heartbeats(
        snapshot_loader=lambda: next(snapshots),
        expected=2,
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert result == _dependency_snapshot(2)
    assert sleeps == [module.WORKER_HEARTBEAT_POLL_SECONDS]


def test_worker_heartbeat_readiness_waits_for_new_instance_to_publish() -> None:
    module = _module()
    snapshots = iter([_dependency_snapshot(1), _dependency_snapshot(2)])

    result = module.wait_for_worker_heartbeats(
        snapshot_loader=lambda: next(snapshots),
        expected=2,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert result == _dependency_snapshot(2)


def test_worker_heartbeat_readiness_fails_closed_with_actual_count() -> None:
    module = _module()
    times = iter([0.0, module.WORKER_HEARTBEAT_TIMEOUT_SECONDS])

    with pytest.raises(RuntimeError, match=r"ready=3, expected=2"):
        module.wait_for_worker_heartbeats(
            snapshot_loader=lambda: _dependency_snapshot(3),
            expected=2,
            clock=lambda: next(times),
            sleeper=lambda _seconds: None,
        )


@pytest.mark.parametrize("ready_instances", [True, "2", None])
def test_worker_heartbeat_readiness_rejects_invalid_count(ready_instances: object) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="ready instance count is invalid"):
        module.wait_for_worker_heartbeats(
            snapshot_loader=lambda: _dependency_snapshot(ready_instances),
            expected=2,
        )


def test_embedding_contract_matches_bootstrap_ingest_and_worker_query() -> None:
    module = _module()
    embedding = {
        "EMBEDDING_MODE": "deterministic-fixture",
        "EMBEDDING_MODEL": "intfloat/multilingual-e5-small",
        "EMBEDDING_REVISION": "pinned-revision",
    }

    def runner(*args: str) -> str:
        if args == ("docker", "compose", "ps", "-a", "-q", "bootstrap-demo"):
            return "bootstrap-1\n"
        if args[:2] == ("docker", "inspect"):
            environment = [f"{key}={value}" for key, value in embedding.items()]
            return module.json.dumps([{"Config": {"Env": environment}}])
        raise AssertionError(args)

    module.verify_embedding_contract(
        [{"ID": "worker-1"}, {"ID": "worker-2"}], runner=runner
    )


def test_embedding_contract_rejects_ingest_query_mismatch() -> None:
    module = _module()

    def runner(*args: str) -> str:
        if args == ("docker", "compose", "ps", "-a", "-q", "bootstrap-demo"):
            return "bootstrap-1\n"
        if args[:2] == ("docker", "inspect"):
            mode = "e5" if args[2] == "bootstrap-1" else "deterministic-fixture"
            return module.json.dumps(
                [
                    {
                        "Config": {
                            "Env": [
                                f"EMBEDDING_MODE={mode}",
                                "EMBEDDING_MODEL=intfloat/multilingual-e5-small",
                                "EMBEDDING_REVISION=pinned-revision",
                            ]
                        }
                    }
                ]
            )
        raise AssertionError(args)

    with pytest.raises(RuntimeError, match="embedding contract mismatch: EMBEDDING_MODE"):
        module.verify_embedding_contract([{"ID": "worker-1"}], runner=runner)


def test_active_index_contract_is_verified_with_the_bootstrap_read_capability() -> None:
    module = _module()
    fingerprint = "a" * 64

    def runner(*args: str) -> str:
        assert args == (
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "bootstrap-demo",
            "python",
            "-m",
            "supportguard.rag.contract_check",
        )
        return module.json.dumps(
            {
                "status": "passed",
                "index_version": "index-v1",
                "pipeline_fingerprint": fingerprint,
            }
        )

    result = module.verify_active_index_contract(runner=runner)
    assert result["index_version"] == "index-v1"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "failed", "index_version": "index-v1", "pipeline_fingerprint": "a" * 64},
        {"status": "passed", "index_version": "", "pipeline_fingerprint": "a" * 64},
        {"status": "passed", "index_version": "index-v1", "pipeline_fingerprint": "short"},
    ],
)
def test_active_index_contract_rejects_unproven_state(payload: dict[str, str]) -> None:
    module = _module()
    with pytest.raises(RuntimeError):
        module.verify_active_index_contract(runner=lambda *_args: module.json.dumps(payload))
