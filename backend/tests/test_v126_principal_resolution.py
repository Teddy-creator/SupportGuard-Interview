from __future__ import annotations

import os
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.api.auth import PrincipalResolution

pytestmark = pytest.mark.postgres


def _url(role: str | None = None) -> str:
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is required")
    if role is None:
        return raw
    return make_url(raw).set(username=role, password=role).render_as_string(hide_password=False)


async def _scope(connection: AsyncConnection, *, subject: str, tenant_id: str) -> None:
    await connection.execute(
        text("SELECT set_config('app.principal_id', :subject, true)"), {"subject": subject}
    )
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )
    await connection.execute(
        text("SELECT set_config('app.principal_role', 'oidc_candidate', true)")
    )


async def _resolve(
    connection: AsyncConnection,
    *,
    trusted_subject: str,
    trusted_tenant: str,
    argument_subject: str | None = None,
    argument_tenant: str | None = None,
) -> dict[str, object] | None:
    await _scope(connection, subject=trusted_subject, tenant_id=trusted_tenant)
    value = await connection.scalar(
        text("SELECT supportguard_api_resolve_principal(:subject, :tenant_id)"),
        {
            "subject": argument_subject or trusted_subject,
            "tenant_id": argument_tenant or trusted_tenant,
        },
    )
    assert value is None or isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_principal_resolution_positive_negative_and_privilege_matrix() -> None:
    suffix = uuid4().hex[:12]
    customer_user = f"user_customer_{suffix}"
    customer_subject = f"oidc-customer-{suffix}"
    approver_user = f"user_approver_{suffix}"
    approver_subject = f"oidc-approver-{suffix}"
    inactive_user = f"user_inactive_{suffix}"
    inactive_subject = f"oidc-inactive-{suffix}"
    unscoped_user = f"user_unscoped_{suffix}"
    unscoped_subject = f"oidc-unscoped-{suffix}"
    empty_tenant = f"tenant_empty_{suffix}"
    empty_user = f"user_empty_{suffix}"
    empty_subject = f"oidc-empty-{suffix}"
    membership_ids = [f"mem_{suffix}_{index}" for index in range(5)]

    admin = create_async_engine(_url())
    api = create_async_engine(_url("supportguard_api"))
    read_mcp = create_async_engine(_url("supportguard_read_mcp"))
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants(id,name,status,created_at,updated_at) VALUES "
                    "(:tenant,'Principal Empty Tenant','active',"
                    "clock_timestamp(),clock_timestamp())"
                ),
                {"tenant": empty_tenant},
            )
            users = [
                (customer_user, customer_subject),
                (approver_user, approver_subject),
                (inactive_user, inactive_subject),
                (unscoped_user, unscoped_subject),
                (empty_user, empty_subject),
            ]
            for user_id, subject in users:
                await connection.execute(
                    text(
                        "INSERT INTO users(id,external_subject,display_name,created_at,updated_at) "
                        "VALUES (:id,:subject,'Principal Fixture',"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {"id": user_id, "subject": subject},
                )
            memberships = [
                (membership_ids[0], "tenant_demo", customer_user, "customer_member", "active"),
                (membership_ids[1], "tenant_demo", approver_user, "support_approver", "active"),
                (membership_ids[2], "tenant_demo", inactive_user, "customer_admin", "inactive"),
                (membership_ids[3], "tenant_demo", unscoped_user, "support_approver", "active"),
                (membership_ids[4], empty_tenant, empty_user, "customer_member", "active"),
            ]
            for membership_id, tenant, user_id, role, membership_status in memberships:
                await connection.execute(
                    text(
                        "INSERT INTO memberships(id,tenant_id,user_id,role,status,"
                        "created_at,updated_at) VALUES (:id,:tenant,:user_id,:role,:status,"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {
                        "id": membership_id,
                        "tenant": tenant,
                        "user_id": user_id,
                        "role": role,
                        "status": membership_status,
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO approver_tenant_scopes(user_id,tenant_id) "
                    "VALUES (:user_id,'tenant_demo')"
                ),
                {"user_id": approver_user},
            )
            tenant_row = (
                (
                    await connection.execute(
                        text("SELECT id,name,status FROM tenants WHERE id='tenant_demo'")
                    )
                )
                .mappings()
                .one()
            )
            customer_row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,display_name,status,security_status,region,version "
                            "FROM customers WHERE id='cust_demo'"
                        )
                    )
                )
                .mappings()
                .one()
            )
            subscription_row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,plan,status,balance,currency,rpm_limit,"
                            "concurrency_limit,version FROM subscriptions WHERE id='sub_demo'"
                        )
                    )
                )
                .mappings()
                .one()
            )

        async with api.begin() as connection:
            customer = await _resolve(
                connection, trusted_subject=customer_subject, trusted_tenant="tenant_demo"
            )
            assert customer is not None
            actual_customer = PrincipalResolution.model_validate(customer).model_dump()
            expected_customer = {
                "schema_version": "principal-resolution.v2",
                "role": "customer",
                "subject_id": customer_user,
                "display_name": "Principal Fixture",
                "tenant_id": "tenant_demo",
                "tenant": dict(tenant_row),
                "customer_id": "cust_demo",
                "customer": dict(customer_row),
                "subscription": dict(subscription_row),
                "accessible_tenants": [dict(tenant_row)],
                "membership_role": "customer_member",
            }
            assert actual_customer == expected_customer

        async with api.begin() as connection:
            approver = await _resolve(
                connection, trusted_subject=approver_subject, trusted_tenant="tenant_demo"
            )
            assert approver is not None
            actual_approver = PrincipalResolution.model_validate(approver).model_dump()
            expected_approver = {
                "schema_version": "principal-resolution.v2",
                "role": "approver",
                "subject_id": approver_user,
                "display_name": "Principal Fixture",
                "tenant_id": "tenant_demo",
                "tenant": dict(tenant_row),
                "customer_id": None,
                "customer": None,
                "subscription": None,
                "accessible_tenants": [dict(tenant_row)],
                "membership_role": "support_approver",
            }
            assert actual_approver == expected_approver

        negative_cases = (
            (customer_subject, "tenant_demo", customer_subject, "tenant_other"),
            (customer_subject, "tenant_demo", "different-subject", "tenant_demo"),
            ("unknown-subject", "tenant_demo", None, None),
            (inactive_subject, "tenant_demo", None, None),
            (unscoped_subject, "tenant_demo", None, None),
            (empty_subject, empty_tenant, None, None),
        )
        for trusted_subject, trusted_tenant, argument_subject, argument_tenant in negative_cases:
            async with api.begin() as connection:
                assert (
                    await _resolve(
                        connection,
                        trusted_subject=trusted_subject,
                        trusted_tenant=trusted_tenant,
                        argument_subject=argument_subject,
                        argument_tenant=argument_tenant,
                    )
                    is None
                )

        denied_reads = (
            "SELECT count(*) FROM public.users",
            "SELECT count(*) FROM public.memberships",
            "SELECT count(*) FROM public.approver_tenant_scopes",
            "SELECT count(*) FROM public.customers",
        )
        direct_identity_sqlstates: list[str] = []
        for denied_read in denied_reads:
            async with api.connect() as connection:
                with pytest.raises(exc.DBAPIError) as denied:
                    await connection.scalar(text(denied_read))
                direct_identity_sqlstates.append(str(getattr(denied.value.orig, "sqlstate", "")))
                await connection.rollback()

        async with read_mcp.begin() as connection:
            await _scope(connection, subject=customer_subject, tenant_id="tenant_demo")
            with pytest.raises(exc.DBAPIError) as denied_function:
                await connection.scalar(
                    text("SELECT supportguard_api_resolve_principal(:subject,:tenant)"),
                    {"subject": customer_subject, "tenant": "tenant_demo"},
                )
        unauthorized_function_sqlstate = str(getattr(denied_function.value.orig, "sqlstate", ""))
        async with admin.connect() as connection:
            environment = (
                await connection.execute(
                    text(
                        "SELECT current_database(),pg_backend_pid(),"
                        "current_setting('server_version_num')::integer,"
                        "pg_current_snapshot()::text"
                    )
                )
            ).one()
            actual_function_roles = sorted(
                await connection.scalars(
                    text(
                        "SELECT role_name FROM (VALUES ('supportguard_api'),"
                        "('supportguard_dispatcher'),('supportguard_reconciler'),"
                        "('supportguard_worker'),('supportguard_read_mcp'),"
                        "('supportguard_action_mcp'),('supportguard_bootstrap'),"
                        "('supportguard_maintenance')) roles(role_name) "
                        "WHERE has_function_privilege(role_name,"
                        "'supportguard_api_resolve_principal(text,text)','EXECUTE')"
                    )
                )
            )
        expected_output_keys = sorted(expected_customer)
        pii_forbidden_keys = sorted({"external_subject", "email", "membership_id", "scope_details"})
        assert direct_identity_sqlstates == ["42501"] * len(denied_reads)
        assert unauthorized_function_sqlstate == "42501"
        assert actual_function_roles == ["supportguard_api"]
        record_predicate_operands(
            requirement_id="C6-P0-08",
            predicate_id="api_principal_resolution_exact",
            subject_kind="postgres_api_principal_resolution_boundary",
            operands={
                "database_name": environment[0],
                "backend_pid": environment[1],
                "server_version_num": environment[2],
                "transaction_snapshot": environment[3],
                "expected_customer_result": expected_customer,
                "actual_customer_result": actual_customer,
                "expected_approver_result": expected_approver,
                "actual_approver_result": actual_approver,
                "expected_output_keys": expected_output_keys,
                "customer_output_keys": sorted(actual_customer),
                "approver_output_keys": sorted(actual_approver),
                "pii_forbidden_keys": pii_forbidden_keys,
                "customer_pii_key_count": sum(key in actual_customer for key in pii_forbidden_keys),
                "approver_pii_key_count": sum(key in actual_approver for key in pii_forbidden_keys),
                "expected_function_roles": ["supportguard_api"],
                "actual_function_roles": actual_function_roles,
                "target_forbidden_sqlstate": "42501",
                "expected_direct_identity_probe_count": len(denied_reads),
                "direct_identity_sqlstates": direct_identity_sqlstates,
                "unauthorized_function_sqlstate": unauthorized_function_sqlstate,
            },
        )
    finally:
        async with admin.begin() as connection:
            await connection.execute(
                text("DELETE FROM approver_tenant_scopes WHERE user_id=:user_id"),
                {"user_id": approver_user},
            )
            for membership_id in membership_ids:
                await connection.execute(
                    text("DELETE FROM memberships WHERE id=:id"), {"id": membership_id}
                )
            for user_id in (
                customer_user,
                approver_user,
                inactive_user,
                unscoped_user,
                empty_user,
            ):
                await connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
            await connection.execute(
                text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": empty_tenant}
            )
        await read_mcp.dispose()
        await api.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_principal_resolution_rejects_unknown_and_ambiguous_memberships() -> None:
    suffix = uuid4().hex[:12]
    admin = create_async_engine(_url())
    try:
        async with admin.connect() as connection:
            transaction = await connection.begin()
            try:
                environment = (
                    await connection.execute(
                        text(
                            "SELECT current_database(),pg_backend_pid(),"
                            "current_setting('server_version_num')::integer,"
                            "pg_current_snapshot()::text"
                        )
                    )
                ).one()
                await connection.execute(
                    text(
                        "ALTER TABLE memberships DROP CONSTRAINT "
                        "ck_memberships_membership_role_valid"
                    )
                )
                await connection.execute(
                    text("ALTER TABLE memberships DROP CONSTRAINT uq_membership_tenant_user")
                )
                kinds = ("valid", "inactive", "unscoped", "empty", "unknown", "ambiguous")
                for kind in kinds:
                    await connection.execute(
                        text(
                            "INSERT INTO users(id,external_subject,display_name,"
                            "created_at,updated_at) "
                            "VALUES (:id,:subject,'Negative Principal Fixture',"
                            "clock_timestamp(),clock_timestamp())"
                        ),
                        {
                            "id": f"user_{kind}_{suffix}",
                            "subject": f"oidc-{kind}-{suffix}",
                        },
                    )
                await connection.execute(
                    text(
                        "INSERT INTO tenants(id,name,status,created_at,updated_at) VALUES "
                        "(:id,'Fail Closed Empty Tenant','active',"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {"id": f"tenant_empty_{suffix}"},
                )
                membership_rows = (
                    ("valid", "tenant_demo", "customer_member", "active"),
                    ("inactive", "tenant_demo", "customer_admin", "inactive"),
                    ("unscoped", "tenant_demo", "support_approver", "active"),
                    ("empty", f"tenant_empty_{suffix}", "customer_member", "active"),
                    ("unknown", "tenant_demo", "unknown_role", "active"),
                )
                for kind, tenant_id, role, status in membership_rows:
                    await connection.execute(
                        text(
                            "INSERT INTO memberships(id,tenant_id,user_id,role,status,"
                            "created_at,updated_at) VALUES "
                            "(:id,:tenant_id,:user_id,:role,:status,"
                            "clock_timestamp(),clock_timestamp())"
                        ),
                        {
                            "id": f"mem_{kind}_{suffix}",
                            "tenant_id": tenant_id,
                            "user_id": f"user_{kind}_{suffix}",
                            "role": role,
                            "status": status,
                        },
                    )
                for index, role in enumerate(("customer_member", "customer_admin")):
                    await connection.execute(
                        text(
                            "INSERT INTO memberships(id,tenant_id,user_id,role,status,"
                            "created_at,updated_at) VALUES "
                            "(:id,'tenant_demo',:user_id,:role,'active',"
                            "clock_timestamp(),clock_timestamp())"
                        ),
                        {
                            "id": f"mem_ambiguous_{suffix}_{index}",
                            "user_id": f"user_ambiguous_{suffix}",
                            "role": role,
                        },
                    )
                await connection.execute(text("SET LOCAL SESSION AUTHORIZATION supportguard_api"))
                valid_subject = f"oidc-valid-{suffix}"
                cases = (
                    (
                        "cross_tenant_argument",
                        valid_subject,
                        "tenant_demo",
                        valid_subject,
                        "tenant_other",
                    ),
                    (
                        "subject_argument_mismatch",
                        valid_subject,
                        "tenant_demo",
                        "different-subject",
                        "tenant_demo",
                    ),
                    ("unknown_user", f"oidc-missing-{suffix}", "tenant_demo", None, None),
                    ("inactive_membership", f"oidc-inactive-{suffix}", "tenant_demo", None, None),
                    (
                        "missing_approver_scope",
                        f"oidc-unscoped-{suffix}",
                        "tenant_demo",
                        None,
                        None,
                    ),
                    (
                        "tenant_customer_missing",
                        f"oidc-empty-{suffix}",
                        f"tenant_empty_{suffix}",
                        None,
                        None,
                    ),
                    (
                        "unknown_membership_role",
                        f"oidc-unknown-{suffix}",
                        "tenant_demo",
                        None,
                        None,
                    ),
                    ("ambiguous_membership", f"oidc-ambiguous-{suffix}", "tenant_demo", None, None),
                )
                actual_case_labels: list[str] = []
                null_result_flags: list[bool] = []
                for label, subject, tenant_id, argument_subject, argument_tenant in cases:
                    result = await _resolve(
                        connection,
                        trusted_subject=subject,
                        trusted_tenant=tenant_id,
                        argument_subject=argument_subject,
                        argument_tenant=argument_tenant,
                    )
                    actual_case_labels.append(label)
                    null_result_flags.append(result is None)
                expected_case_labels = [case[0] for case in cases]
                assert actual_case_labels == expected_case_labels
                assert all(null_result_flags)
                record_predicate_operands(
                    requirement_id="C6-P0-08",
                    predicate_id="api_principal_resolution_fail_closed",
                    subject_kind="postgres_api_principal_resolution_negative_matrix",
                    operands={
                        "database_name": environment[0],
                        "backend_pid": environment[1],
                        "server_version_num": environment[2],
                        "transaction_snapshot": environment[3],
                        "expected_case_labels": expected_case_labels,
                        "actual_case_labels": actual_case_labels,
                        "expected_case_count": len(cases),
                        "actual_case_count": len(actual_case_labels),
                        "target_fail_closed": True,
                        "null_result_flags": null_result_flags,
                        "nonnull_result_count": sum(not flag for flag in null_result_flags),
                    },
                )
            finally:
                await transaction.rollback()
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_principal_resolution_catalog_contract_is_hardened() -> None:
    admin = create_async_engine(_url())
    try:
        async with admin.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT p.prosecdef,p.provolatile,pg_get_userbyid(p.proowner),p.proconfig "
                        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' "
                        "AND p.proname='supportguard_api_resolve_principal' "
                        "AND pg_get_function_identity_arguments(p.oid)='p_external_subject text, "
                        "p_tenant_id text'"
                    )
                )
            ).one()
            volatility = row[1].decode() if isinstance(row[1], bytes) else row[1]
            assert (row[0], volatility, row[2]) == (True, "s", "supportguard_owner")
            assert list(row.proconfig or ()) == ["search_path=pg_catalog, public"]
            roles = set(
                await connection.scalars(
                    text(
                        "SELECT role_name FROM (VALUES "
                        "('supportguard_api'),('supportguard_dispatcher'),"
                        "('supportguard_reconciler'),('supportguard_worker'),"
                        "('supportguard_read_mcp'),('supportguard_action_mcp'),"
                        "('supportguard_bootstrap'),('supportguard_maintenance')) roles(role_name) "
                        "WHERE has_function_privilege(role_name,"
                        "'supportguard_api_resolve_principal(text,text)','EXECUTE')"
                    )
                )
            )
            assert roles == {"supportguard_api"}
    finally:
        await admin.dispose()


def test_principal_resolution_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PrincipalResolution.model_validate(
            {
                "schema_version": "principal-resolution.v1",
                "role": "customer",
                "subject_id": "user_demo",
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "membership_role": "customer_member",
                "external_subject": "must-not-cross-the-boundary",
            }
        )
