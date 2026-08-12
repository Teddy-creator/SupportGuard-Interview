from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from supportguard.config import Settings, get_settings
from supportguard.db.interview_baseline import (
    acquire_interview_baseline_lock,
    inspect_interview_baseline_admin,
    inspect_interview_template_clone_admin,
)
from supportguard.db.session import create_engine

SERVICE_ROLES = (
    "supportguard_api",
    "supportguard_dispatcher",
    "supportguard_reconciler",
    "supportguard_worker",
    "supportguard_read_mcp",
    "supportguard_action_mcp",
    "supportguard_bootstrap",
    "supportguard_maintenance",
)

INTERVIEW_DATABASE_CONNECT_ROLES = ("supportguard_migrator", *SERVICE_ROLES)
INTERVIEW_DATABASE_TEMPORARY_ROLES = ("supportguard_migrator",)


async def _quoted_password(connection: AsyncConnection, value: str) -> str:
    # Passwords are data, never SQL identifiers. PostgreSQL performs the quoting.
    result = await connection.execute(text("SELECT quote_literal(:password)"), {"password": value})
    quoted = result.scalar_one()
    return str(quoted)


async def _apply_interview_database_access_contract(connection: AsyncConnection) -> None:
    """Replace database ACLs with the one canonical Interview Edition contract."""

    database_name = str((await connection.execute(text("SELECT current_database()"))).scalar_one())
    quoted_database = str(
        (
            await connection.execute(
                text("SELECT quote_ident(:database_name)"),
                {"database_name": database_name},
            )
        ).scalar_one()
    )
    grantees = tuple(
        str(row[0])
        for row in (
            await connection.execute(
                text(
                    "SELECT DISTINCT CASE WHEN acl.grantee=0 THEN 'PUBLIC' "
                    "ELSE pg_catalog.quote_ident(role.rolname) END "
                    "FROM pg_catalog.pg_database database "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce("
                    "database.datacl,pg_catalog.acldefault('d'::\"char\",database.datdba)"
                    ")) acl LEFT JOIN pg_catalog.pg_roles role ON role.oid=acl.grantee "
                    "WHERE database.datname=pg_catalog.current_database() "
                    "AND acl.grantee<>database.datdba ORDER BY 1"
                )
            )
        ).all()
    )
    for grantee in grantees:
        await connection.execute(
            text(
                f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} "  # noqa: S608
                f"FROM {grantee}"
            )
        )
    await connection.execute(
        text(
            f"GRANT CONNECT ON DATABASE {quoted_database} TO "  # noqa: S608
            + ", ".join(INTERVIEW_DATABASE_CONNECT_ROLES)
        )
    )
    await connection.execute(
        text(
            f"GRANT TEMPORARY ON DATABASE {quoted_database} TO "  # noqa: S608
            + ", ".join(INTERVIEW_DATABASE_TEMPORARY_ROLES)
        )
    )
    await connection.execute(
        text(
            f"REVOKE CREATE ON DATABASE {quoted_database} "  # noqa: S608
            "FROM supportguard_owner"
        )
    )


async def _bootstrap_database_roles(settings: Settings | None) -> None:
    resolved = settings or get_settings()
    engine = create_engine(resolved)
    passwords = {
        "supportguard_migrator": resolved.migrator_database_password.get_secret_value(),
        "supportguard_api": resolved.api_database_password.get_secret_value(),
        "supportguard_dispatcher": resolved.dispatcher_database_password.get_secret_value(),
        "supportguard_reconciler": resolved.reconciler_database_password.get_secret_value(),
        "supportguard_worker": resolved.worker_database_password.get_secret_value(),
        "supportguard_read_mcp": resolved.read_mcp_database_password.get_secret_value(),
        "supportguard_action_mcp": resolved.action_mcp_database_password.get_secret_value(),
        "supportguard_bootstrap": resolved.bootstrap_database_password.get_secret_value(),
        "supportguard_maintenance": resolved.maintenance_database_password.get_secret_value(),
    }
    try:
        async with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("database role bootstrap requires PostgreSQL")
            # The shared transaction lock and read-only preflight are the first
            # target-database operations. The current product path rejects
            # legacy, unknown, or partial databases before any role/extension DDL.
            await connection.run_sync(acquire_interview_baseline_lock)
            await connection.run_sync(inspect_interview_baseline_admin)
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE supportguard_owner NOLOGIN NOINHERIT; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(
                text(
                    "ALTER ROLE supportguard_owner WITH NOLOGIN NOINHERIT NOSUPERUSER "
                    "NOBYPASSRLS NOREPLICATION NOCREATEDB NOCREATEROLE"
                )
            )
            await connection.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE supportguard_rls_client NOLOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            for role, password in passwords.items():
                await connection.execute(
                    text(
                        f"DO $$ BEGIN CREATE ROLE {role} LOGIN; "  # noqa: S608
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                    )
                )
                quoted = await _quoted_password(connection, password)
                await connection.execute(
                    text(
                        f"ALTER ROLE {role} WITH LOGIN PASSWORD {quoted} "  # noqa: S608
                        "NOINHERIT NOSUPERUSER NOBYPASSRLS NOREPLICATION "
                        "NOCREATEDB NOCREATEROLE"
                    )
                )
            for statement in (
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE "
                "rolname='supportguard_read') THEN EXECUTE 'DROP OWNED BY supportguard_read'; "
                "EXECUTE 'DROP ROLE supportguard_read'; END IF; END $$",
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE "
                "rolname='supportguard_action') THEN EXECUTE 'DROP OWNED BY supportguard_action'; "
                "EXECUTE 'DROP ROLE supportguard_action'; END IF; END $$",
            ):
                await connection.execute(text(statement))
            owner_boundary_active = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT to_regclass("
                            "'supportguard_control.runtime_timing_snapshots') IS NOT NULL"
                        )
                    )
                ).scalar_one()
            )
            # The role bootstrap intentionally revokes database CREATE from the
            # NOLOGIN owner below.  Create the fixed control schema first so a
            # truly empty Compose database can enter the owner-only migrations
            # without temporarily broadening either runtime or migrator roles.
            await connection.execute(
                text(
                    "CREATE SCHEMA IF NOT EXISTS supportguard_control "
                    "AUTHORIZATION supportguard_owner"
                )
            )
            await _apply_interview_database_access_contract(connection)
            await connection.execute(text("GRANT supportguard_owner TO supportguard_migrator"))
            if owner_boundary_active:
                # A role/password maintenance rerun must never reopen the one-shot
                # ownership-transfer capability after b126c0a1d001 closed it.
                await connection.execute(
                    text(
                        "DROP FUNCTION IF EXISTS public.supportguard_bootstrap_transfer_ownership()"
                    )
                )
            else:
                await connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION "
                        "public.supportguard_bootstrap_transfer_ownership() "
                        "RETURNS void LANGUAGE plpgsql SECURITY DEFINER "
                        "SET search_path=pg_catalog,public AS $$ BEGIN "
                        "IF session_user <> 'supportguard_migrator' THEN "
                        "RAISE EXCEPTION USING ERRCODE='42501', "
                        "MESSAGE='ownership transfer caller is not authorized'; END IF; "
                        "REASSIGN OWNED BY supportguard_migrator TO supportguard_owner; "
                        "ALTER FUNCTION public.supportguard_bootstrap_transfer_ownership() "
                        "OWNER TO supportguard_migrator; END $$"
                    )
                )
                await connection.execute(
                    text(
                        "REVOKE ALL ON FUNCTION "
                        "public.supportguard_bootstrap_transfer_ownership() FROM PUBLIC"
                    )
                )
                await connection.execute(
                    text(
                        "GRANT EXECUTE ON FUNCTION "
                        "public.supportguard_bootstrap_transfer_ownership() "
                        "TO supportguard_migrator"
                    )
                )
            await connection.execute(text("ALTER SCHEMA public OWNER TO supportguard_owner"))
            await connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
            await connection.execute(
                text("GRANT USAGE, CREATE ON SCHEMA public TO supportguard_owner")
            )
            if owner_boundary_active:
                await connection.execute(
                    text("GRANT USAGE ON SCHEMA public TO supportguard_migrator")
                )
                await connection.execute(
                    text("REVOKE CREATE ON SCHEMA public FROM supportguard_migrator")
                )
            else:
                # Alembic must create its version table and pre-owner schema objects
                # before the owner-transfer migration closes this bootstrap grant.
                await connection.execute(
                    text("GRANT USAGE, CREATE ON SCHEMA public TO supportguard_migrator")
                )
            await connection.execute(
                text(
                    "GRANT USAGE ON SCHEMA public TO supportguard_api, supportguard_dispatcher, "
                    "supportguard_reconciler, supportguard_worker, supportguard_read_mcp, "
                    "supportguard_action_mcp, supportguard_bootstrap, supportguard_maintenance"
                )
            )
            # Existing developer volumes predate the owner role. Transfer only application
            # objects in public; the administrator remains database/extension owner.
            await connection.execute(
                text(
                    "DO $$ DECLARE item record; BEGIN "
                    "FOR item IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP "
                    "EXECUTE format('ALTER TABLE public.%I OWNER TO supportguard_owner', "
                    "item.tablename); END LOOP; "
                    "FOR item IN SELECT sequencename FROM pg_sequences WHERE schemaname='public' "
                    "LOOP EXECUTE format('ALTER SEQUENCE public.%I OWNER TO "
                    "supportguard_owner', "
                    "item.sequencename); END LOOP; END $$"
                )
            )
    finally:
        await engine.dispose()


async def bootstrap_interview_database_roles(settings: Settings | None = None) -> None:
    """Bootstrap roles only after the current baseline's read-only preflight passes."""

    await _bootstrap_database_roles(settings)


async def restore_interview_clone_database_access(
    settings: Settings | None = None, *, source_database: str
) -> None:
    """Finalize one exact template clone and prove its complete current identity."""

    resolved = settings or get_settings()
    engine = create_engine(resolved)
    try:
        async with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("database access restoration requires PostgreSQL")
            await connection.run_sync(acquire_interview_baseline_lock)
            await connection.run_sync(
                lambda sync_connection: inspect_interview_template_clone_admin(
                    sync_connection, expected_source_database=source_database
                )
            )
            await _apply_interview_database_access_contract(connection)
            rebound = (
                await connection.execute(
                    text(
                        "UPDATE supportguard_control.database_identity "
                        "SET database_uuid=gen_random_uuid(),database_name=current_database() "
                        "WHERE database_name=:source_database RETURNING database_name"
                    ),
                    {"source_database": source_database},
                )
            ).all()
            current_database = str(
                (await connection.execute(text("SELECT current_database()"))).scalar_one()
            )
            if tuple(str(row[0]) for row in rebound) != (current_database,):
                raise RuntimeError("interview_clone_database_identity_rebind_failed")
            await connection.run_sync(inspect_interview_baseline_admin)
    finally:
        await engine.dispose()


async def configure_local_mcp_roles(settings: Settings | None = None) -> None:
    """Rotate MCP login credentials without reopening bootstrap-time capabilities."""
    resolved = settings or get_settings()
    engine = create_engine(resolved)
    passwords = {
        "supportguard_read_mcp": resolved.read_mcp_database_password.get_secret_value(),
        "supportguard_action_mcp": resolved.action_mcp_database_password.get_secret_value(),
    }
    try:
        async with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("MCP role configuration requires PostgreSQL")
            boundary_active = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT to_regclass("
                            "'supportguard_control.runtime_timing_snapshots') IS NOT NULL"
                        )
                    )
                ).scalar_one()
            )
            if not boundary_active:
                raise RuntimeError("MCP role configuration requires completed migrations")
            existing = {
                str(row[0])
                for row in (
                    await connection.execute(
                        text("SELECT rolname FROM pg_roles WHERE rolname=ANY(:roles)"),
                        {"roles": sorted(passwords)},
                    )
                ).all()
            }
            missing = sorted(set(passwords) - existing)
            if missing:
                raise RuntimeError(f"MCP roles missing; run bootstrap-roles first: {missing}")
            for role, password in passwords.items():
                quoted = await _quoted_password(connection, password)
                await connection.execute(
                    text(
                        f"ALTER ROLE {role} WITH LOGIN PASSWORD {quoted} "  # noqa: S608
                        "NOINHERIT NOSUPERUSER NOBYPASSRLS NOREPLICATION "
                        "NOCREATEDB NOCREATEROLE"
                    )
                )
            # Repair databases touched by the historical alias implementation and
            # make every rerun monotonic: it may close bootstrap power, never reopen it.
            await connection.execute(
                text("DROP FUNCTION IF EXISTS public.supportguard_bootstrap_transfer_ownership()")
            )
            await connection.execute(
                text("REVOKE CREATE ON SCHEMA public FROM supportguard_migrator")
            )
    finally:
        await engine.dispose()
