from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from supportguard.api import readiness as health_runtime

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/health/dependencies")
async def internal_dependencies(
    request: Request,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> health_runtime.ReadinessSnapshot:
    health_runtime.require_internal_token(request, x_internal_token)
    return await health_runtime.evaluate_readiness(request)


@router.get("/metrics", include_in_schema=False)
async def internal_metrics(
    request: Request,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> Response:
    health_runtime.require_internal_token(request, x_internal_token)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
