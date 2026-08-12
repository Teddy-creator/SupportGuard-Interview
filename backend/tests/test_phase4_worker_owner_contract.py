from __future__ import annotations

import ast
from pathlib import Path

from supportguard import worker as legacy_worker
from supportguard.runtime import delivery as runtime_delivery
from supportguard.runtime import finalizer as runtime_finalizer
from supportguard.runtime import worker as runtime_worker
from supportguard.services import runtime_maintenance
from supportguard.services import runtime_queue as legacy_delivery

RUNTIME_WORKER_PATH = Path("backend/src/supportguard/runtime/worker.py")
RUNTIME_DELIVERY_PATH = Path("backend/src/supportguard/runtime/delivery.py")
RUNTIME_FINALIZER_PATH = Path("backend/src/supportguard/runtime/finalizer.py")
LEGACY_WORKER_PATH = Path("backend/src/supportguard/worker.py")
LEGACY_DELIVERY_PATH = Path("backend/src/supportguard/services/runtime_queue.py")


def test_runtime_worker_composes_physical_delivery_and_finalizer_owners() -> None:
    assert legacy_worker.AgentJobHandler is runtime_worker.AgentJobHandler
    assert legacy_worker.worker_runtime is runtime_worker.worker_runtime
    assert legacy_worker.worker_heartbeat_snapshot is runtime_worker.worker_heartbeat_snapshot
    assert legacy_worker.finalizer_state is runtime_worker.finalizer_state

    legacy_source = LEGACY_WORKER_PATH.read_text()
    assert "class AgentJobHandler" not in legacy_source
    assert "async def worker_runtime" not in legacy_source

    assert runtime_worker.AgentJobHandler is runtime_finalizer.AgentJobHandler
    assert runtime_worker.finalizer_state is runtime_finalizer.finalizer_state
    assert legacy_delivery.RuntimeWorker is runtime_delivery.RuntimeWorker
    assert legacy_delivery.OutboxDispatcher is runtime_delivery.OutboxDispatcher
    assert legacy_delivery.RuntimeReconciler is runtime_maintenance.RuntimeReconciler

    worker_source = RUNTIME_WORKER_PATH.read_text()
    finalizer_source = RUNTIME_FINALIZER_PATH.read_text()
    delivery_source = RUNTIME_DELIVERY_PATH.read_text()
    legacy_delivery_source = LEGACY_DELIVERY_PATH.read_text()
    assert "from supportguard.runtime.delivery import RuntimeWorker" in worker_source
    assert "from supportguard.runtime.finalizer import" in worker_source
    assert "class AgentJobHandler" not in worker_source
    assert "class AgentJobHandler" in finalizer_source
    assert "class RuntimeWorker" in delivery_source
    assert "class RuntimeWorker" not in legacy_delivery_source


def test_current_runtime_importers_bypass_the_legacy_worker_facade() -> None:
    source_root = Path("backend/src/supportguard")
    importers = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path != LEGACY_WORKER_PATH and "supportguard.worker" in path.read_text(encoding="utf-8")
    ]

    assert importers == []
    assert (
        "from supportguard.runtime.worker import worker_runtime"
        in Path("backend/src/supportguard/cli.py").read_text()
    )

    delivery_importers = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path != LEGACY_DELIVERY_PATH
        and "supportguard.services.runtime_queue" in path.read_text(encoding="utf-8")
    ]
    assert delivery_importers == []


def test_worker_entry_and_one_hop_functions_respect_the_phase4_decision_budget() -> None:
    for path in (RUNTIME_WORKER_PATH, RUNTIME_DELIVERY_PATH, RUNTIME_FINALIZER_PATH):
        tree = ast.parse(path.read_text())
        sizes = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno is not None
        }

        assert sizes
        assert max(sizes.values()) < 200, path
