from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.context import RequestContext, WorkerExecutionContext

ScopeContext = RequestContext | WorkerExecutionContext


def database_scope(context: ScopeContext) -> tuple[str, str, str]:
    if isinstance(context, RequestContext):
        return (
            context.tenant_id,
            context.authenticated_actor_id,
            context.authenticated_actor_role,
        )
    return (
        context.tenant_id,
        context.actor_principal_id,
        "system_worker",
    )


async def set_local_scope(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_id: str,
    principal_role: str,
) -> None:
    """Bind trusted request/job scope to the current PostgreSQL transaction."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config('app.tenant_id', :value, true)"), {"value": tenant_id}
    )
    await session.execute(
        text("SELECT set_config('app.principal_id', :value, true)"),
        {"value": principal_id},
    )
    await session.execute(
        text("SELECT set_config('app.principal_role', :value, true)"),
        {"value": principal_role},
    )
    values = (
        await session.execute(
            text(
                "SELECT current_setting('app.tenant_id', true), "
                "current_setting('app.principal_id', true), "
                "current_setting('app.principal_role', true)"
            )
        )
    ).one()
    if tuple(values) != (tenant_id, principal_id, principal_role):
        raise RuntimeError("database transaction scope verification failed")
