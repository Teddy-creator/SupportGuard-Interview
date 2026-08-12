from __future__ import annotations

import asyncio
import inspect

import pytest

from supportguard.runtime.worker import worker_runtime
from supportguard.services.runtime_queue import (
    ServiceLoopProgress,
    bounded_service_loop,
)


@pytest.mark.asyncio
async def test_bounded_control_loop_fails_closed_on_hung_iteration() -> None:
    never = asyncio.Event()
    progress = ServiceLoopProgress(service="dispatcher")

    async def hung_operation() -> None:
        await never.wait()

    with pytest.raises(RuntimeError, match="dispatcher_operation_timeout"):
        await bounded_service_loop(
            hung_operation,
            interval_seconds=0,
            operation_timeout_seconds=0.01,
            progress=progress,
        )

    assert progress.active_since is not None
    assert progress.completed_iterations == 0


def test_worker_runtime_redis_transport_is_bounded() -> None:
    source = inspect.getsource(worker_runtime)

    assert "socket_connect_timeout=5" in source
    assert "socket_timeout=5" in source
    assert "health_check_interval=10" in source
