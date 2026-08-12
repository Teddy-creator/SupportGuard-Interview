"""Compatibility facade for the Interview Edition Runtime Worker owner."""

from supportguard.runtime.worker import (
    AgentJobHandler as AgentJobHandler,
)
from supportguard.runtime.worker import (
    finalizer_state as finalizer_state,
)
from supportguard.runtime.worker import (
    worker_heartbeat_snapshot as worker_heartbeat_snapshot,
)
from supportguard.runtime.worker import (
    worker_runtime as worker_runtime,
)

__all__ = (
    "AgentJobHandler",
    "finalizer_state",
    "worker_heartbeat_snapshot",
    "worker_runtime",
)
