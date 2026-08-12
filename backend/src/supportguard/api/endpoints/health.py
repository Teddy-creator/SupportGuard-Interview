from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from supportguard import __version__
from supportguard.api import readiness as health_runtime
from supportguard.api.auth import app_settings

router = APIRouter(tags=["health"])


@router.get("/health", response_model=health_runtime.HealthResponse)
async def health(request: Request) -> health_runtime.HealthResponse:
    settings = app_settings(request)
    auth_mode = settings.auth_mode
    if not hasattr(request.app.state, "runtime"):
        configured_fake = settings.demo_fake_provider or settings.app_env == "test"
        return health_runtime.HealthResponse(
            version=__version__,
            provider_mode="worker-owned-configured-fake" if configured_fake else "worker-owned",
            provider_model="deterministic-fake" if configured_fake else settings.llm_model,
            tool_call_mode="native_fixture" if configured_fake else "native-worker",
            mcp={
                "read": {"process": "worker-owned", "session": "worker-owned"},
                "action": {"process": "worker-owned", "session": "worker-owned"},
            },
            auth_mode=auth_mode,
        )
    provider = request.app.state.runtime.provider
    manager = request.app.state.runtime.mcp_manager
    return health_runtime.HealthResponse(
        version=__version__,
        provider_mode=provider.mode,
        provider_model=provider.model,
        tool_call_mode=provider.tool_call_mode,
        mcp=(
            manager.health()
            if manager is not None
            else {
                "read": {"process": "disabled", "session": "disabled", "schema": "test"},
                "action": {"process": "disabled", "session": "disabled", "schema": "test"},
            }
        ),
        auth_mode=auth_mode,
    )


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive", "service": "api", "version": __version__}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    snapshot = await health_runtime.evaluate_readiness(request)
    is_ready = snapshot.status in {"healthy", "compatible_read_only"}
    return JSONResponse(
        status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": (
                "read_only"
                if snapshot.status == "compatible_read_only"
                else "ready"
                if is_ready
                else "unavailable"
            )
        },
    )
