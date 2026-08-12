from __future__ import annotations

from asyncio import run
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, event, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from supportguard.config import get_settings
from supportguard.db.interview_baseline import (
    acquire_interview_baseline_lock,
    inspect_interview_baseline,
    verify_interview_migration_postcondition,
)
from supportguard.db.security_contract import BASELINE_IDENTITY

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    raise RuntimeError("interview_baseline_offline_mode_forbidden")


def _escape_parameterless_driver_percent(
    connection: Connection,
    _cursor: object,
    statement: str,
    parameters: object,
    execution_context: object,
    _executemany: bool,
) -> tuple[str, object]:
    if (
        getattr(execution_context, "compiled", None) is None
        and not parameters
        and connection.dialect.paramstyle in {"format", "pyformat"}
    ):
        return statement.replace("%", "%%"), parameters
    return statement, parameters


def do_run_migrations(connection: Connection) -> None:
    event.listen(
        connection,
        "before_cursor_execute",
        _escape_parameterless_driver_percent,
        retval=True,
    )
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table=BASELINE_IDENTITY.version_table,
        version_table_schema=BASELINE_IDENTITY.version_table_schema,
        compare_type=True,
        include_schemas=True,
    )
    with context.begin_transaction():
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '120s'")
        acquire_interview_baseline_lock(connection)
        inspect_interview_baseline(connection)
        context.run_migrations()
        verify_interview_migration_postcondition(connection)


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_migrations_online())
