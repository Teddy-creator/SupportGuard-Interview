from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer
from langgraph.checkpoint.memory import InMemorySaver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard import __version__
from supportguard.agent.checkpoints import postgres_checkpointer
from supportguard.agent.contracts import (
    validate_candidate_code_version,
    validate_contract_bundle,
)
from supportguard.api.auth import build_oidc_authenticator
from supportguard.api.endpoints.health import router as health_router
from supportguard.api.endpoints.internal import router as internal_router
from supportguard.api.problems import ProductProblem, PublicProblemCode
from supportguard.api.routes import router as api_router
from supportguard.api.sse import router as sse_router
from supportguard.config import Settings, get_settings
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.db.base import Base
from supportguard.db.seed import seed_demo_data
from supportguard.db.session import (
    create_engine,
    create_scoped_session_factory,
    create_session_factory,
)
from supportguard.mcp.runtime import MCPManager
from supportguard.mcp.test_transport import InProcessTestToolTransport
from supportguard.observability.context import (
    RequestContext,
    bind_request_context,
    current_request_context,
    reset_request_context,
)
from supportguard.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS
from supportguard.observability.tracing import (
    HTTP_SERVER,
    extracted_context,
    inject_trace_context,
    tracer,
)
from supportguard.providers.base import StructuredProvider
from supportguard.providers.deepseek import DeepSeekProvider, ProviderError
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.rag.embeddings import build_embedding_provider
from supportguard.rag.ingest import ingest_corpus
from supportguard.runtime import AppRuntime
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.schema_rollout import (
    classify_schema_probe_failure,
    inspect_schema_rollout,
    require_current_runtime_schema,
)

logger = logging.getLogger(__name__)


def build_provider(settings: Settings, *, testing: bool) -> StructuredProvider:
    if settings.app_env == "production" and settings.demo_fake_provider:
        raise ProviderError("production runtime cannot enable the fake provider")
    if testing or settings.app_env == "test" or settings.demo_fake_provider:
        return DeterministicFakeProvider(
            delay_seconds=settings.demo_fake_provider_delay_seconds,
            max_input_tokens=settings.provider_max_input_tokens,
        )
    return DeepSeekProvider(settings)


@dataclass(frozen=True, slots=True)
class TestAppEnvironment:
    """Typed ownership boundary for one deterministic test application."""

    settings: Settings
    temporary_directory: tempfile.TemporaryDirectory[str]

    @classmethod
    def create(cls, base_settings: Settings) -> TestAppEnvironment:
        directory = tempfile.TemporaryDirectory(prefix="supportguard-test-")
        database_url = f"sqlite+aiosqlite:///{directory.name}/supportguard.db"
        settings = base_settings.model_copy(
            update={
                "app_env": "test",
                "async_runtime_enabled": False,
                "database_url": database_url,
                "mcp_read_database_url": None,
                "mcp_action_database_url": None,
                "redis_url": "redis://test-owner.invalid:1/15",
                "demo_fake_provider": False,
            }
        )
        return cls(settings=settings, temporary_directory=directory)

    def close(self) -> None:
        self.temporary_directory.cleanup()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    testing = bool(getattr(app.state, "testing", False))
    test_environment = TestAppEnvironment.create(get_settings()) if testing else None
    settings = test_environment.settings if test_environment is not None else get_settings()
    validate_contract_bundle()
    validate_candidate_code_version(settings)
    app.state.settings = settings
    engine = create_async_engine(settings.database_url) if testing else create_engine(settings)
    factory = create_session_factory(engine, settings=settings)
    app.state.factory = factory
    app.state.scoped_factory = create_scoped_session_factory(engine, settings=settings)
    app.state.oidc_authenticator = build_oidc_authenticator(settings)
    if settings.app_env == "production" and settings.auth_mode != "production":
        raise RuntimeError("production runtime requires production auth")
    if settings.app_env == "production" and not settings.async_runtime_enabled:
        raise RuntimeError("production runtime requires the asynchronous worker path")
    if (
        settings.app_env == "production"
        and settings.app_secret_key.get_secret_value() == "local-development-only-change-me"
    ):
        raise RuntimeError("production auth cannot use the default application secret")
    if (
        settings.app_env == "production"
        and settings.internal_api_token.get_secret_value() == "local-internal-health-token"
    ):
        raise RuntimeError("production auth cannot use the default internal API token")
    if not testing:
        try:
            await require_current_runtime_schema(factory, service="api")
        except BaseException:
            await engine.dispose()
            raise
    if not testing and settings.async_runtime_enabled:
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            app.state.factory = factory
            app.state.redis = redis
            app.state.session_serializer = URLSafeTimedSerializer(
                settings.app_secret_key.get_secret_value(), salt="supportguard-demo-session"
            )
            yield
        finally:
            await redis.aclose()
            await engine.dispose()
        return
    provider = build_provider(settings, testing=testing)
    embedding = build_embedding_provider(settings, testing=testing)
    if testing:
        capability = issue_test_runtime_capability(testing=True)
        transport = InProcessTestToolTransport(
            app.state.scoped_factory,
            embedding,
            capability,
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await require_current_runtime_schema(
                factory,
                service="api",
                current_metadata_fixture=True,
            )
            async with factory() as session:
                await seed_demo_data(session)
                await ingest_corpus(
                    session,
                    root=Path.cwd(),
                    manifest_path=Path("knowledge/manifests/documents.json"),
                    embedding=embedding,
                )
                await session.commit()
            app.state.runtime = AppRuntime(
                engine=engine,
                factory=factory,
                checkpointer=InMemorySaver(),
                provider=provider,
                embedding=embedding,
                mcp_manager=transport,
                test_capability=capability,
                settings=settings,
            )
            app.state.tool_transport = transport
            app.state.session_serializer = URLSafeTimedSerializer(
                settings.app_secret_key.get_secret_value(), salt="supportguard-demo-session"
            )
            yield
        finally:
            if isinstance(provider, DeepSeekProvider):
                await provider.aclose()
            await engine.dispose()
            if test_environment is not None:
                test_environment.close()
        return
    manager = MCPManager()
    try:
        await manager.start()
        async with postgres_checkpointer(settings.database_url) as checkpointer:
            app.state.runtime = AppRuntime(
                engine=engine,
                factory=factory,
                checkpointer=checkpointer,
                provider=provider,
                embedding=embedding,
                mcp_manager=manager,
                settings=settings,
            )
            app.state.session_serializer = URLSafeTimedSerializer(
                settings.app_secret_key.get_secret_value(), salt="supportguard-demo-session"
            )
            yield
    finally:
        await manager.stop()
        if isinstance(provider, DeepSeekProvider):
            await provider.aclose()
        await engine.dispose()


def create_app(*, testing: bool = False) -> FastAPI:
    app = FastAPI(
        title="SupportGuard API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.testing = testing
    app.include_router(health_router, prefix="/api")
    app.include_router(internal_router)
    app.include_router(api_router, prefix="/api")
    app.include_router(sse_router, prefix="/api")

    def api_problem(
        request: Request,
        *,
        status_code: int,
        public_code: PublicProblemCode,
        message: str,
        retryable: bool,
    ) -> JSONResponse:
        context = current_request_context.get()
        request_id = context.request_id if context is not None else "unavailable"
        problem = ProductProblem(
            public_code=public_code,
            message=message,
            retryable=retryable,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status_code,
            content=problem.model_dump(mode="json"),
        )

    def public_problem_code(status_code: int) -> PublicProblemCode:
        if status_code == 401:
            return "authentication_required"
        if status_code == 403:
            return "forbidden"
        if status_code == 404:
            return "resource_not_found"
        if status_code == 409:
            return "state_conflict"
        if status_code == 422:
            return "invalid_request"
        if status_code == 429:
            return "rate_limited"
        if status_code in {502, 503, 504}:
            return "service_unavailable"
        if status_code >= 500:
            return "internal_error"
        return "request_rejected"

    def public_problem_message(status_code: int) -> str:
        messages = {
            401: "登录状态已失效，请重新登录。",
            403: "当前身份无权执行此操作。",
            404: "请求的资源不存在或当前身份无权访问。",
            409: "数据已发生变化，请刷新后重试。",
            422: "提交内容不符合要求，请检查后重试。",
            429: "请求过于频繁，请稍后重试。",
            502: "服务暂时不可用，请稍后重试。",
            503: "服务暂时不可用，请稍后重试。",
            504: "服务暂时不可用，请稍后重试。",
        }
        if status_code >= 500:
            return messages.get(status_code, "服务暂时遇到问题，请稍后重试。")
        return messages.get(status_code, "请求未能完成。")

    def log_api_rejection(
        request: Request,
        *,
        status_code: int,
        internal_reason: str,
        error_type: str,
    ) -> None:
        context = current_request_context.get()
        logger.log(
            logging.ERROR if status_code >= 500 else logging.WARNING,
            "api_request_rejected",
            extra={
                "request_id": context.request_id if context is not None else "unavailable",
                "request_path": request.url.path,
                "request_method": request.method,
                "status_code": status_code,
                "internal_reason": internal_reason[:256],
                "error_type": error_type,
            },
        )

    @app.exception_handler(HTTPException)
    async def api_http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if not request.url.path.startswith("/api/"):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        internal_reason = exc.detail if isinstance(exc.detail, str) else type(exc.detail).__name__
        log_api_rejection(
            request,
            status_code=exc.status_code,
            internal_reason=internal_reason,
            error_type=type(exc).__name__,
        )
        return api_problem(
            request,
            status_code=exc.status_code,
            public_code=public_problem_code(exc.status_code),
            message=public_problem_message(exc.status_code),
            retryable=exc.status_code in {429, 502, 503, 504},
        )

    @app.exception_handler(RequestValidationError)
    async def api_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not request.url.path.startswith("/api/"):
            return JSONResponse(status_code=422, content={"detail": exc.errors()})
        log_api_rejection(
            request,
            status_code=422,
            internal_reason=f"request_validation_error:{len(exc.errors())}",
            error_type=type(exc).__name__,
        )
        return api_problem(
            request,
            status_code=422,
            public_code="invalid_request",
            message="提交内容不符合要求，请检查后重试。",
            retryable=False,
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        if exc.code in {ErrorCode.TICKET_NOT_FOUND, ErrorCode.APPROVAL_NOT_FOUND}:
            status_code = 404
        elif exc.code in {
            ErrorCode.TICKET_STATE_CONFLICT,
            ErrorCode.APPROVAL_STATE_CONFLICT,
            ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
            ErrorCode.APPROVAL_BINDING_INVALID,
            ErrorCode.CHECKPOINT_NOT_INTERRUPTED,
            ErrorCode.APPROVAL_STALE,
            ErrorCode.IDEMPOTENCY_CONFLICT,
        }:
            status_code = 409
        else:
            status_code = 422
        log_api_rejection(
            request,
            status_code=status_code,
            internal_reason=exc.code.value,
            error_type=type(exc).__name__,
        )
        return api_problem(
            request,
            status_code=status_code,
            public_code=public_problem_code(status_code),
            message=(
                "请求的资源不存在或当前身份无权访问。"
                if status_code == 404
                else "数据已发生变化，请刷新后重试。"
                if status_code == 409
                else "提交内容不符合业务规则，请检查后重试。"
            ),
            retryable=False,
        )

    @app.middleware("http")
    async def require_current_writer_contract(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path.startswith("/api/")
            and request.url.path != "/api/demo-sessions"
        ):
            try:
                async with request.app.state.factory() as session:
                    rollout = await inspect_schema_rollout(session)
            except Exception as exc:
                failure = classify_schema_probe_failure(exc)
                if failure is None:
                    raise
                logger.warning(
                    "schema_rollout_probe_unavailable",
                    extra={
                        "error_type": type(exc).__name__,
                        "failure_class": failure,
                        "request_method": request.method,
                    },
                )
                return api_problem(
                    request,
                    status_code=503,
                    public_code="service_unavailable",
                    message="服务暂时不可用，请稍后重试。",
                    retryable=failure == "transient",
                )
            if not rollout.current_writer_compatible:
                logger.warning(
                    "writer_contract_unavailable",
                    extra={
                        "request_method": request.method,
                        "database_head": rollout.database_head,
                    },
                )
                return api_problem(
                    request,
                    status_code=503,
                    public_code="service_unavailable",
                    message="服务暂时不可用，请稍后重试。",
                    retryable=True,
                )
        return await call_next(request)

    @app.middleware("http")
    async def correlate(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = f"request_{uuid4().hex}"
        trace_id = request.headers.get("X-Trace-ID", f"trace_{uuid4().hex}")
        started = perf_counter()
        with tracer().start_as_current_span(
            f"HTTP {request.method}",
            context=extracted_context(request.headers.get("traceparent")),
            kind=HTTP_SERVER,
            attributes={"http.request.method": request.method},
        ):
            carrier: dict[str, str] = {}
            inject_trace_context(carrier)
            token = bind_request_context(
                RequestContext(request_id, trace_id, carrier.get("traceparent"))
            )
            try:
                try:
                    response = await call_next(request)
                except Exception:
                    if not request.url.path.startswith("/api/"):
                        raise
                    logger.exception("unhandled_api_error")
                    response = api_problem(
                        request,
                        status_code=500,
                        public_code="internal_error",
                        message="服务暂时遇到问题，请稍后重试。",
                        retryable=True,
                    )
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_id
                route = request.scope.get("route")
                route_template = str(getattr(route, "path", "unmatched"))
                HTTP_REQUESTS.labels(request.method, route_template, response.status_code).inc()
                return response
            finally:
                route = request.scope.get("route")
                route_template = str(getattr(route, "path", "unmatched"))
                HTTP_LATENCY.labels(route_template).observe(perf_counter() - started)
                reset_request_context(token)

    return app


app = create_app()
