from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from supportguard.config import Settings, get_settings
from supportguard.contracts.context import RequestContext, WorkerExecutionContext
from supportguard.db.scope import ScopeContext, database_scope

RUNTIME_CODE_VERSION_INFO_KEY = "supportguard_runtime_code_version"
DEFAULT_RUNTIME_CODE_VERSION = "development"


def runtime_code_version(session: AsyncSession) -> str:
    """Read provenance from the factory that owns this database session."""

    value = session.info.get(RUNTIME_CODE_VERSION_INFO_KEY)
    return value if isinstance(value, str) and value else DEFAULT_RUNTIME_CODE_VERSION


def _runtime_session_info(settings: Settings | None) -> dict[str, str]:
    return {
        RUNTIME_CODE_VERSION_INFO_KEY: (
            settings.code_version if settings is not None else DEFAULT_RUNTIME_CODE_VERSION
        )
    }


class _ScopedSyncSession(Session):
    pass


class ScopedAsyncSession(AsyncSession):
    sync_session_class = _ScopedSyncSession

    def __init__(self, *args: Any, trusted_scope: ScopeContext, **kwargs: Any) -> None:
        self.trusted_scope = trusted_scope
        super().__init__(*args, **kwargs)
        self.sync_session.info["supportguard_trusted_scope"] = trusted_scope


@event.listens_for(_ScopedSyncSession, "after_begin")
def _bind_root_transaction_scope(
    session: Session, transaction: Any, connection: Any
) -> None:
    if transaction.nested:
        return
    scope = session.info.get("supportguard_trusted_scope")
    if not isinstance(scope, (RequestContext, WorkerExecutionContext)):
        raise RuntimeError("business transaction started without trusted scope")
    tenant_id, principal_id, principal_role = database_scope(scope)
    if connection.dialect.name != "postgresql":
        session.info["supportguard_scope_bound"] = (
            tenant_id,
            principal_id,
            principal_role,
        )
        return
    subject_customer_id = (
        scope.subject_customer_id
        if isinstance(scope, RequestContext)
        else scope.customer_id
    )
    expected = (
        tenant_id,
        principal_id,
        principal_role,
        subject_customer_id or "",
    )
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"),
        {"tenant": tenant_id},
    )
    connection.execute(
        text("SELECT set_config('app.principal_id', :principal, true)"),
        {"principal": principal_id},
    )
    connection.execute(
        text("SELECT set_config('app.principal_role', :role, true)"),
        {"role": principal_role},
    )
    connection.execute(
        text("SELECT set_config('app.subject_customer_id', :customer_id, true)"),
        {"customer_id": subject_customer_id or ""},
    )
    actual = tuple(
        connection.execute(
            text(
                "SELECT current_setting('app.tenant_id', true), "
                "current_setting('app.principal_id', true), "
                "current_setting('app.principal_role', true), "
                "current_setting('app.subject_customer_id', true)"
            )
        ).one()
    )
    if actual != expected:
        raise RuntimeError("database transaction scope verification failed")
    session.info["supportguard_scope_bound"] = expected


class ScopedSessionFactory:
    """Only business-session entry point; a scope is mandatory and immutable per session."""

    def __init__(self, engine: AsyncEngine, *, settings: Settings | None = None) -> None:
        self._factory = async_sessionmaker(
            engine,
            class_=ScopedAsyncSession,
            expire_on_commit=False,
            info=_runtime_session_info(settings),
        )

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("use ScopedSessionFactory.request() or .worker()")

    @asynccontextmanager
    async def request(self, context: RequestContext) -> AsyncIterator[ScopedAsyncSession]:
        async with self._factory(trusted_scope=context) as session:
            yield session

    @asynccontextmanager
    async def worker(
        self, context: WorkerExecutionContext
    ) -> AsyncIterator[ScopedAsyncSession]:
        async with self._factory(trusted_scope=context) as session:
            yield session


def create_scoped_session_factory(
    engine: AsyncEngine, *, settings: Settings | None = None
) -> ScopedSessionFactory:
    return ScopedSessionFactory(engine, settings=settings)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    resolved = settings or get_settings()
    return create_async_engine(resolved.database_url, pool_pre_ping=True)


def create_session_factory(
    engine: AsyncEngine, *, settings: Settings | None = None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        info=_runtime_session_info(settings),
    )


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
