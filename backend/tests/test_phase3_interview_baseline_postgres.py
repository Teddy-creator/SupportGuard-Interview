from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.config import Settings
from supportguard.db.interview_baseline import (
    CURRENT_BASELINE_MANIFEST_SHA256,
    RUNTIME_TIMING_V1_CONFIG_HASH,
    catalog_security_manifest_sha256,
    inspect_interview_baseline_admin,
    inspect_interview_template_clone_admin,
    verify_interview_baseline_postcondition,
)
from supportguard.db.permissions import (
    INTERVIEW_DATABASE_CONNECT_ROLES,
    bootstrap_interview_database_roles,
    restore_interview_clone_database_access,
)
from supportguard.db.security_contract import (
    CRITICAL_CONSTRAINTS,
    CURRENT_INTERVIEW_DATABASE_REVISION,
)

pytestmark = pytest.mark.postgres


def _url(role: str | None = None) -> str:
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is required")
    if role is None:
        return raw
    return make_url(raw).set(username=role, password=role).render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_current_role_bootstrap_preflight_and_postcondition_are_repeatable() -> None:
    admin_url = _url()
    admin = create_async_engine(admin_url)
    try:
        async with admin.connect() as connection:
            before = await connection.run_sync(catalog_security_manifest_sha256)
            preflight = await connection.run_sync(inspect_interview_baseline_admin)
        assert preflight.classification == "current"
        assert preflight.observed_revision == CURRENT_INTERVIEW_DATABASE_REVISION
        assert before == CURRENT_BASELINE_MANIFEST_SHA256

        await bootstrap_interview_database_roles(
            Settings(_env_file=None, app_env="test", database_url=admin_url)
        )

        async with admin.connect() as connection:
            after = await connection.run_sync(catalog_security_manifest_sha256)
            await connection.run_sync(verify_interview_baseline_postcondition)
            timing = (
                await connection.execute(
                    text(
                        "SELECT timing_version,max_job_age_seconds,max_attempts,"
                        "max_delivery_generation,redelivery_grace_seconds,lease_seconds,"
                        "backlog_count_limit,oldest_backlog_seconds,config_hash,is_active "
                        "FROM supportguard_control.runtime_timing_snapshots"
                    )
                )
            ).one()
            identity = (
                await connection.execute(
                    text("SELECT database_name FROM supportguard_control.database_identity")
                )
            ).one()
        assert after == before
        assert tuple(timing) == (
            1,
            600,
            5,
            5,
            15,
            30,
            500,
            600,
            RUNTIME_TIMING_V1_CONFIG_HASH,
            True,
        )
        assert tuple(identity) == (make_url(admin_url).database,)
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_catalog_manifest_is_independent_of_installer_search_path() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL search_path = ''"))
            digest = await connection.run_sync(catalog_security_manifest_sha256)
        assert digest == CURRENT_BASELINE_MANIFEST_SHA256
    finally:
        await engine.dispose()


async def _create_template_clone(source_url: str, clone_database: str) -> str:
    source = make_url(source_url)
    assert source.database
    control = create_async_engine(source.set(database="postgres"))
    try:
        async with control.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            quoted_clone = str(
                (
                    await connection.execute(
                        text("SELECT pg_catalog.quote_ident(:value)"),
                        {"value": clone_database},
                    )
                ).scalar_one()
            )
            quoted_source = str(
                (
                    await connection.execute(
                        text("SELECT pg_catalog.quote_ident(:value)"),
                        {"value": source.database},
                    )
                ).scalar_one()
            )
            await connection.execute(
                text(f"CREATE DATABASE {quoted_clone} TEMPLATE {quoted_source}")  # noqa: S608
            )
    finally:
        await control.dispose()
    return source.set(database=clone_database).render_as_string(hide_password=False)


async def _drop_template_clone(source_url: str, clone_database: str) -> None:
    control = create_async_engine(make_url(source_url).set(database="postgres"))
    try:
        async with control.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(
                text(
                    "SELECT pg_catalog.pg_terminate_backend(pid) "
                    "FROM pg_catalog.pg_stat_activity "
                    "WHERE datname=:database AND pid<>pg_catalog.pg_backend_pid()"
                ),
                {"database": clone_database},
            )
            quoted_clone = str(
                (
                    await connection.execute(
                        text("SELECT pg_catalog.quote_ident(:value)"),
                        {"value": clone_database},
                    )
                ).scalar_one()
            )
            await connection.execute(
                text(f"DROP DATABASE IF EXISTS {quoted_clone}")  # noqa: S608
            )
    finally:
        await control.dispose()


def _clone_settings(clone_url: str) -> Settings:
    return Settings(_env_file=None, app_env="test", database_url=clone_url)


@pytest.mark.asyncio
async def test_template_clone_acl_is_restored_before_strict_current_preflight() -> None:
    source_url = _url()
    clone_database = f"supportguard_acl_clone_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    source_database = str(make_url(source_url).database)
    try:
        async with clone.connect() as connection:
            await connection.run_sync(
                lambda sync_connection: inspect_interview_template_clone_admin(
                    sync_connection, expected_source_database=source_database
                )
            )
            with pytest.raises(RuntimeError, match="interview_baseline_current_catalog_mismatch"):
                await connection.run_sync(inspect_interview_baseline_admin)

        await restore_interview_clone_database_access(
            _clone_settings(clone_url), source_database=source_database
        )
        async with clone.connect() as connection:
            restored = await connection.run_sync(catalog_security_manifest_sha256)
            await connection.run_sync(inspect_interview_baseline_admin)
            identity = await connection.scalar(
                text("SELECT database_name FROM supportguard_control.database_identity")
            )
            acl_rows = (
                await connection.execute(
                    text(
                        "SELECT coalesce(role.rolname,'PUBLIC'),acl.privilege_type "
                        "FROM pg_catalog.pg_database database "
                        "CROSS JOIN LATERAL pg_catalog.aclexplode(database.datacl) acl "
                        "LEFT JOIN pg_catalog.pg_roles role ON role.oid=acl.grantee "
                        "WHERE database.datname=pg_catalog.current_database()"
                    )
                )
            ).all()
        matrix: dict[str, set[str]] = {}
        for role, privilege in acl_rows:
            matrix.setdefault(str(role), set()).add(str(privilege))
        assert restored == CURRENT_BASELINE_MANIFEST_SHA256
        assert identity == clone_database
        assert "PUBLIC" not in matrix
        assert matrix["supportguard_migrator"] == {"CONNECT", "TEMPORARY"}
        for role in INTERVIEW_DATABASE_CONNECT_ROLES:
            assert "CONNECT" in matrix[role]
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.asyncio
async def test_template_clone_with_extra_grantee_fails_before_acl_mutation() -> None:
    source_url = _url()
    clone_database = f"supportguard_acl_extra_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    try:
        async with clone.begin() as connection:
            await connection.execute(
                text(
                    f'GRANT CONNECT ON DATABASE "{clone_database}" '  # noqa: S608
                    "TO supportguard_rls_client"
                )
            )
        async with clone.connect() as connection:
            before = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
        with pytest.raises(RuntimeError, match="interview_template_clone_database_acl_invalid"):
            await restore_interview_clone_database_access(
                _clone_settings(clone_url),
                source_database=str(make_url(source_url).database),
            )
        async with clone.connect() as connection:
            after = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
        assert before == after
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.asyncio
async def test_template_clone_with_non_database_drift_fails_before_acl_mutation() -> None:
    source_url = _url()
    clone_database = f"supportguard_acl_drift_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    try:
        async with clone.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE supportguard_control.database_identity "
                    "ADD COLUMN unexpected_clone_drift text"
                )
            )
        with pytest.raises(RuntimeError, match="interview_template_clone_catalog_mismatch"):
            await restore_interview_clone_database_access(
                _clone_settings(clone_url),
                source_database=str(make_url(source_url).database),
            )
        async with clone.connect() as connection:
            acl = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
            drift_remains = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute attribute "
                    "WHERE attribute.attrelid="
                    "'supportguard_control.database_identity'::pg_catalog.regclass "
                    "AND attribute.attname='unexpected_clone_drift' "
                    "AND NOT attribute.attisdropped)"
                )
            )
        assert acl is None
        assert drift_remains is True
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            "UPDATE supportguard_control.runtime_timing_snapshots "
            "SET is_active=false WHERE is_active",
            "interview_template_clone_runtime_timing_invalid",
        ),
        (
            "DELETE FROM supportguard_control.database_identity",
            "interview_template_clone_database_identity_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_template_clone_with_missing_operational_bootstrap_fails_before_acl_mutation(
    mutation: str, error: str
) -> None:
    source_url = _url()
    clone_database = f"supportguard_data_drift_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    try:
        async with clone.begin() as connection:
            await connection.execute(text(mutation))
        with pytest.raises(RuntimeError, match=error):
            await restore_interview_clone_database_access(
                _clone_settings(clone_url),
                source_database=str(make_url(source_url).database),
            )
        async with clone.connect() as connection:
            acl = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
        assert acl is None
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            "UPDATE supportguard_control.runtime_timing_snapshots "
            "SET is_active=false WHERE is_active",
            "interview_baseline_current_runtime_timing_invalid",
        ),
        (
            "DELETE FROM supportguard_control.database_identity",
            "interview_baseline_current_database_identity_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_current_operational_bootstrap_drift_fails_before_role_ddl(
    mutation: str, error: str
) -> None:
    source_url = _url()
    clone_database = f"supportguard_current_drift_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    source_database = str(make_url(source_url).database)
    try:
        await restore_interview_clone_database_access(
            _clone_settings(clone_url), source_database=source_database
        )
        async with clone.begin() as connection:
            await connection.execute(text(mutation))
        async with clone.connect() as connection:
            before_catalog = await connection.run_sync(catalog_security_manifest_sha256)
            before_acl = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
        with pytest.raises(RuntimeError, match=error):
            await bootstrap_interview_database_roles(_clone_settings(clone_url))
        async with clone.connect() as connection:
            after_catalog = await connection.run_sync(catalog_security_manifest_sha256)
            after_acl = await connection.scalar(
                text(
                    "SELECT datacl::text FROM pg_catalog.pg_database "
                    "WHERE datname=pg_catalog.current_database()"
                )
            )
        assert (after_catalog, after_acl) == (before_catalog, before_acl)
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.asyncio
async def test_current_preflight_accepts_a_versioned_active_runtime_timing() -> None:
    source_url = _url()
    clone_database = f"supportguard_timing_v2_{uuid4().hex[:10]}"
    clone_url = await _create_template_clone(source_url, clone_database)
    clone = create_async_engine(clone_url)
    source_database = str(make_url(source_url).database)
    try:
        await restore_interview_clone_database_access(
            _clone_settings(clone_url), source_database=source_database
        )
        async with clone.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE supportguard_control.runtime_timing_snapshots "
                    "SET is_active=false WHERE is_active"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO supportguard_control.runtime_timing_snapshots("
                    "timing_version,max_job_age_seconds,max_attempts,"
                    "max_delivery_generation,redelivery_grace_seconds,lease_seconds,"
                    "backlog_count_limit,oldest_backlog_seconds,config_hash,is_active) "
                    "VALUES (2,720,6,6,20,40,600,720,:config_hash,true)"
                ),
                {"config_hash": "2" * 64},
            )
        await bootstrap_interview_database_roles(_clone_settings(clone_url))
        async with clone.connect() as connection:
            await connection.run_sync(inspect_interview_baseline_admin)
            await connection.run_sync(verify_interview_baseline_postcondition)
            active_version = await connection.scalar(
                text(
                    "SELECT timing_version FROM "
                    "supportguard_control.runtime_timing_snapshots WHERE is_active"
                )
            )
        assert active_version == 2
    finally:
        await clone.dispose()
        await _drop_template_clone(source_url, clone_database)


@pytest.mark.asyncio
async def test_interview_baseline_preserves_critical_constraints_and_role_boundaries() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.connect() as connection:
            missing: list[str] = []
            for item in CRITICAL_CONSTRAINTS:
                if item.category == "index":
                    found = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_indexes "
                            "WHERE schemaname=:schema AND tablename=:relation "
                            "AND indexname=:name)"
                        ),
                        {
                            "schema": item.schema,
                            "relation": item.relation,
                            "name": item.catalog_name,
                        },
                    )
                else:
                    found = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k "
                            "JOIN pg_catalog.pg_class c ON c.oid=k.conrelid "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=:schema AND c.relname=:relation "
                            "AND k.conname=:name AND k.convalidated)"
                        ),
                        {
                            "schema": item.schema,
                            "relation": item.relation,
                            "name": item.catalog_name,
                        },
                    )
                if not found:
                    missing.append(item.requirement)
            role_rows = (
                await connection.execute(
                    text(
                        "SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolreplication,"
                        "rolbypassrls FROM pg_catalog.pg_roles "
                        "WHERE rolname LIKE 'supportguard_%' ORDER BY rolname"
                    )
                )
            ).all()
        assert not missing
        assert role_rows
        assert all(not any(bool(flag) for flag in row[1:]) for row in role_rows)
    finally:
        await engine.dispose()
