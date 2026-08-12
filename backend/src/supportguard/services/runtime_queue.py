"""Compatibility imports for the pre-v2 Runtime delivery module.

Current Runtime code imports :mod:`supportguard.runtime.delivery` and
:mod:`supportguard.services.runtime_maintenance` directly. This facade preserves
historical tests and validation tooling until Phase 6 disposition without
creating a second implementation.
"""

from supportguard.runtime.delivery import (
    CONTROL_LOOP_TIMEOUT_SECONDS as CONTROL_LOOP_TIMEOUT_SECONDS,
)
from supportguard.runtime.delivery import (
    OutboxDispatcher as OutboxDispatcher,
)
from supportguard.runtime.delivery import (
    RuntimeWorker as RuntimeWorker,
)
from supportguard.runtime.delivery import (
    ServiceLoopProgress as ServiceLoopProgress,
)
from supportguard.runtime.delivery import (
    _validate_worker_finish_result as _validate_worker_finish_result,
)
from supportguard.runtime.delivery import (
    bounded_service_loop as bounded_service_loop,
)
from supportguard.runtime.delivery import (
    bounded_stream_add as bounded_stream_add,
)
from supportguard.runtime.delivery import (
    ensure_consumer_group as ensure_consumer_group,
)
from supportguard.runtime.delivery import (
    record_delivery as record_delivery,
)
from supportguard.services.runtime_maintenance import (
    MAX_DELIVERY_GENERATION as MAX_DELIVERY_GENERATION,
)
from supportguard.services.runtime_maintenance import (
    RECONCILE_OBSERVATION_SCRIPT as RECONCILE_OBSERVATION_SCRIPT,
)
from supportguard.services.runtime_maintenance import (
    RETENTION_FINALIZE_TOMBSTONE_SCRIPT as RETENTION_FINALIZE_TOMBSTONE_SCRIPT,
)
from supportguard.services.runtime_maintenance import (
    RETENTION_GROUP as RETENTION_GROUP,
)
from supportguard.services.runtime_maintenance import (
    RETENTION_GROUP_SET_HASH as RETENTION_GROUP_SET_HASH,
)
from supportguard.services.runtime_maintenance import (
    RETENTION_TRIM_SCRIPT as RETENTION_TRIM_SCRIPT,
)
from supportguard.services.runtime_maintenance import (
    RedisTrimReport as RedisTrimReport,
)
from supportguard.services.runtime_maintenance import (
    RuntimeReconciler as RuntimeReconciler,
)
from supportguard.services.runtime_maintenance import (
    _pending_message_ids as _pending_message_ids,
)
from supportguard.services.runtime_maintenance import (
    trim_terminal_deliveries as trim_terminal_deliveries,
)

__all__ = [
    "CONTROL_LOOP_TIMEOUT_SECONDS",
    "MAX_DELIVERY_GENERATION",
    "OutboxDispatcher",
    "RedisTrimReport",
    "RuntimeReconciler",
    "RuntimeWorker",
    "ServiceLoopProgress",
    "bounded_service_loop",
    "bounded_stream_add",
    "ensure_consumer_group",
    "record_delivery",
    "trim_terminal_deliveries",
]
