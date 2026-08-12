from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PublicProblemCode = Literal[
    "authentication_required",
    "forbidden",
    "internal_error",
    "invalid_request",
    "rate_limited",
    "request_rejected",
    "resource_not_found",
    "service_unavailable",
    "state_conflict",
]


class ProductProblem(BaseModel):
    """Stable customer-safe API error contract."""

    model_config = ConfigDict(extra="forbid")

    public_code: PublicProblemCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
