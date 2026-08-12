"""Compatibility facade for the readiness contract owner."""

from supportguard.api.readiness import (
    HealthResponse,
    ReadinessSnapshot,
    evaluate_readiness,
    require_internal_token,
)

__all__ = [
    "HealthResponse",
    "ReadinessSnapshot",
    "evaluate_readiness",
    "require_internal_token",
]
