from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from redis.asyncio import Redis

from supportguard.agent.checkpoints import postgres_checkpointer
from supportguard.agent.contracts import (
    CONTEXT_VERSION,
    canonical_runtime_manifest,
    runtime_provenance,
    validate_candidate_code_version,
    validate_contract_bundle,
)
from supportguard.config import Settings
from supportguard.db.models import ProviderRuntimeEvent
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.db.session import create_engine, create_session_factory
from supportguard.main import build_provider
from supportguard.mcp.runtime import MCPManager
from supportguard.providers.base import StructuredProvider
from supportguard.providers.deepseek import DeepSeekProvider, ProviderError
from supportguard.providers.limiter import RedisProviderLimiter
from supportguard.rag.embeddings import MCPOnlyEmbedding
from supportguard.runtime.app import AppRuntime
from supportguard.runtime.delivery import RuntimeWorker
from supportguard.runtime.finalizer import AgentJobHandler, finalizer_state
from supportguard.services.heartbeats import ServiceHeartbeatSnapshot
from supportguard.services.runtime_timing import RuntimeTiming
from supportguard.services.schema_rollout import require_current_writer_contract

__all__ = [
    "AgentJobHandler",
    "finalizer_state",
    "worker_heartbeat_snapshot",
    "worker_runtime",
]


def worker_heartbeat_snapshot(
    *,
    settings: Settings,
    provider: StructuredProvider,
    manager: MCPManager,
    worker: RuntimeWorker,
) -> ServiceHeartbeatSnapshot:
    """Project only bounded, non-secret facts used by the readiness contract."""

    manifest = canonical_runtime_manifest(
        settings=settings,
        model=provider.model,
        provider_mode=provider.mode,
        tool_call_mode=provider.tool_call_mode,
    )
    mcp = manager.health()
    read = mcp["read"]
    action = mcp["action"]
    last_activity_at = max(
        worker.last_progress_at,
        getattr(worker, "last_lease_heartbeat_at", None) or worker.last_progress_at,
    )
    progress_age = max(
        0.0,
        (datetime.now(UTC) - last_activity_at).total_seconds(),
    )
    progress_limit = max(
        30.0,
        (worker.timing.heartbeat_interval.total_seconds() * 3 if worker.timing else 30.0),
    )
    limiter_ready = not isinstance(provider, DeepSeekProvider) or provider.limiter is not None
    provider_ready = (
        provider.model == settings.llm_model
        and provider.mode == "production"
        and provider.tool_call_mode == "native"
        and limiter_ready
        if settings.app_env == "production"
        else provider.tool_call_mode in {"native", "native_fixture"}
    )

    def mcp_ready(value: Mapping[str, object]) -> bool:
        generation = value.get("generation")
        return (
            value.get("state") == "ready"
            and value.get("process") == "running"
            and value.get("session") == "ready"
            and value.get("schema") == "verified"
            and isinstance(value.get("schema_hash"), str)
            and isinstance(generation, int)
            and generation >= 1
        )

    ready = (
        provider_ready and mcp_ready(read) and mcp_ready(action) and progress_age <= progress_limit
    )
    capabilities = (
        "agent",
        f"runtime_manifest:{manifest.content_hash}",
        f"code_commit:{manifest.code_commit}",
        f"prompt:{manifest.prompt_version}:{manifest.prompt_hash}",
        f"schema:{manifest.schema_version}:{manifest.schema_hash}",
        f"provider:{provider.mode}:{provider.model}:{provider.tool_call_mode}",
        f"provider_limiter:{'ready' if limiter_ready else 'unavailable'}",
        f"redis_consumer:{'recent' if progress_age <= progress_limit else 'stale'}",
        f"read_mcp:{read.get('state')}:{read.get('generation')}:{read.get('schema_hash')}",
        (
            "action_mcp:"
            f"{action.get('state')}:{action.get('generation')}:{action.get('schema_hash')}"
        ),
        f"consumer_progress_age_ms:{int(progress_age * 1000)}",
        f"migration_head:{CURRENT_PRODUCT_DATABASE_HEAD}",
        "database_identity:interview_baseline",
    )
    return ServiceHeartbeatSnapshot(
        status="ready" if ready else "degraded",
        capabilities=capabilities,
        migration_head=CURRENT_PRODUCT_DATABASE_HEAD,
    )


@asynccontextmanager
async def worker_runtime(settings: Settings) -> AsyncIterator[RuntimeWorker]:
    """Own the Worker process lifecycle and compose delivery with finalization."""

    validate_contract_bundle()
    validate_candidate_code_version(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine, settings=settings)
    try:
        await require_current_writer_contract(factory, service="worker")
    except BaseException:
        await engine.dispose()
        raise
    manager = MCPManager()
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=10,
    )
    try:
        provider = build_provider(settings, testing=False)
    except ProviderError as exc:
        async with factory() as session, session.begin():
            session.add(
                ProviderRuntimeEvent(
                    service_instance_id=settings.service_instance_id,
                    status="initialization_failed",
                    model=settings.llm_model,
                    provider_mode="production",
                    tool_call_mode="native",
                    error_code=type(exc).__name__,
                    runtime_provenance=runtime_provenance(
                        model=settings.llm_model,
                        provider_mode="production",
                        tool_call_mode="native",
                        context_version=CONTEXT_VERSION,
                        code_version=settings.code_version,
                        settings=settings,
                    ),
                )
            )
        await redis.aclose()
        await engine.dispose()
        raise
    async with factory() as session, session.begin():
        session.add(
            ProviderRuntimeEvent(
                service_instance_id=settings.service_instance_id,
                status="initialized",
                model=provider.model,
                provider_mode=provider.mode,
                tool_call_mode=provider.tool_call_mode,
                error_code=None,
                runtime_provenance=runtime_provenance(
                    model=provider.model,
                    provider_mode=provider.mode,
                    tool_call_mode=provider.tool_call_mode,
                    context_version=CONTEXT_VERSION,
                    code_version=settings.code_version,
                    settings=settings,
                ),
            )
        )
    if isinstance(provider, DeepSeekProvider):
        provider.limiter = RedisProviderLimiter(
            redis,
            max_inflight=settings.provider_max_inflight,
            rpm_capacity=settings.provider_rpm_capacity,
        )
    await manager.start()
    try:
        timing = await RuntimeTiming.from_database(factory, settings)
        async with postgres_checkpointer(settings.database_url) as checkpointer:
            runtime = AppRuntime(
                engine=engine,
                factory=factory,
                checkpointer=checkpointer,
                provider=provider,
                embedding=MCPOnlyEmbedding(),
                mcp_manager=manager,
                settings=settings,
            )
            worker = RuntimeWorker(
                factory,
                redis,
                stream=settings.redis_stream,
                group=settings.redis_consumer_group,
                consumer=settings.service_instance_id,
                handler=AgentJobHandler(factory, runtime),
                timing=timing,
            )
            worker.heartbeat_snapshot_provider = lambda: worker_heartbeat_snapshot(
                settings=settings,
                provider=provider,
                manager=manager,
                worker=worker,
            )
            yield worker
    finally:
        await manager.stop()
        if isinstance(provider, DeepSeekProvider):
            await provider.aclose()
        await redis.aclose()
        await engine.dispose()
