import os

import pytest
from sqlalchemy import UniqueConstraint, exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.db.models import ApprovalRequest, CitationBinding, ProposalRecord
from supportguard.db.role_contract import (
    BOOTSTRAP_TABLES,
    FUNCTION_GRANTS,
    INTERVIEW_RETIRED_FUNCTION_GRANTS,
    OWNER_ONLY_FUNCTIONS,
    RUNTIME_ROLES,
    TRIGGER_ONLY_FUNCTIONS,
    V15_WORKER_MUTABLE_TABLES,
    V15_WORKER_SELECT_TABLES,
    V126_RETIRED_FUNCTION_GRANTS,
    WORKER_APPEND_ONLY_TABLES,
    WORKER_APPROVAL_UPDATE_COLUMNS,
    WORKER_CHECKPOINT_TABLES,
    WORKER_MUTABLE_TABLES,
    WORKER_PROPOSAL_UPDATE_COLUMNS,
    WORKER_SELECT_TABLES,
    expected_function_grants,
    expected_worker_table_grants,
)


def _url(role: str | None = None) -> str:
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is required")
    if role is None:
        return raw
    return make_url(raw).set(username=role, password=role).render_as_string(hide_password=False)


def test_v126_history_and_v1512_current_function_denominators_are_explicit() -> None:
    grants = expected_function_grants()
    historical_signatures = {item.signature for item in FUNCTION_GRANTS}
    retired_signatures = {
        item.signature
        for item in (*V126_RETIRED_FUNCTION_GRANTS, *INTERVIEW_RETIRED_FUNCTION_GRANTS)
    }
    assert len(FUNCTION_GRANTS) == 62
    assert len(historical_signatures) == 62
    assert len(grants) == 60
    assert [item.signature for item in V126_RETIRED_FUNCTION_GRANTS] == [
        "supportguard_api_accept_message(text,jsonb)"
    ]
    assert [item.signature for item in INTERVIEW_RETIRED_FUNCTION_GRANTS] == [
        "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)"
    ]
    assert retired_signatures <= historical_signatures
    assert set(grants) == historical_signatures - retired_signatures
    assert "supportguard_api_accept_message(text,jsonb)" not in grants
    assert "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)" not in grants
    assert set().union(*(item.roles for item in FUNCTION_GRANTS)) == (
        RUNTIME_ROLES - {"supportguard_bootstrap"}
    ) | {"supportguard_migrator"}
    assert all(item.roles for item in FUNCTION_GRANTS)
    assert not TRIGGER_ONLY_FUNCTIONS & grants.keys()
    assert "supportguard_run_retention(boolean)" not in grants
    assert {signature for signature in grants if signature.startswith("supportguard_worker_")} == {
        "supportguard_worker_claim_job(text,text)",
        "supportguard_worker_heartbeat_job(text,text,bigint)",
        "supportguard_worker_finish_job(text,text,bigint,text)",
        "supportguard_worker_accept_delivery(jsonb)",
        "supportguard_worker_record_poison(jsonb)",
        "supportguard_worker_finalize(jsonb)",
        "supportguard_worker_revalidate_approver_scope(text,text,text,bigint)",
        "supportguard_worker_execute_approved_action(text,text,text,bigint)",
        "supportguard_worker_publish_conversation_message(jsonb)",
    }
    assert {signature for signature in grants if signature.startswith("supportguard_api_")} == {
        "supportguard_api_resolve_principal(text,text)",
        "supportguard_api_accept_ticket(jsonb)",
        "supportguard_api_accept_conversation_approval_decision(text,text,jsonb)",
        "supportguard_api_converge_checkpoint_binding_stale(text)",
        "supportguard_api_withdraw_proposal(text,jsonb)",
        "supportguard_api_transition_conversation(text,text,jsonb)",
        "supportguard_api_customer_exists(text)",
        "supportguard_api_list_tickets(text,integer)",
        "supportguard_api_get_ticket(text,text)",
        "supportguard_api_get_run(text,text)",
        "supportguard_api_get_run_inspector(text,text,text,text,text)",
        "supportguard_api_list_ticket_events(text,text,bigint,integer)",
        "supportguard_api_list_approvals(integer)",
        "supportguard_api_get_approval(text)",
        "supportguard_api_get_approval_source(text,bigint,text,integer)",
        "supportguard_api_runtime_snapshot()",
        "supportguard_api_accept_conversation_message(text,jsonb)",
        "supportguard_api_list_conversations(text,text,text,integer)",
        "supportguard_api_get_conversation(text,text)",
        "supportguard_api_get_run_citations(text,text)",
        "supportguard_api_get_conversation_page(text,text,integer,integer)",
    }


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_v1512_api_legacy_accept_message_capability_is_revoked_live() -> None:
    engine = create_async_engine(_url("supportguard_api"))
    try:
        async with engine.connect() as connection:
            with pytest.raises(exc.DBAPIError) as denied:
                await connection.execute(
                    text(
                        "SELECT supportguard_api_accept_message("
                        "'missing-ticket',CAST('{}' AS jsonb))"
                    )
                )
        assert str(getattr(denied.value.orig, "sqlstate", "")) == "42501"
    finally:
        await engine.dispose()


def test_v129_owner_only_formatter_does_not_expand_runtime_grants() -> None:
    assert len(OWNER_ONLY_FUNCTIONS) == 1
    formatter = OWNER_ONLY_FUNCTIONS[0]
    assert formatter.signature == "supportguard_internal_format_utc_timestamp(timestamptz)"
    assert formatter.owner == "supportguard_owner"
    assert formatter.definition_sha256 == (
        "c8ebbec2b907c9ebf7ea8b2a1da3ed4f75155425396e12b7eae323b4835b8989"
    )
    assert formatter.signature not in expected_function_grants()


def test_v126_table_manifests_are_disjoint_and_include_new_contract_tables() -> None:
    worker_groups = (
        WORKER_SELECT_TABLES,
        WORKER_MUTABLE_TABLES,
        V15_WORKER_MUTABLE_TABLES,
        WORKER_APPEND_ONLY_TABLES,
        WORKER_CHECKPOINT_TABLES,
    )
    for index, left in enumerate(worker_groups):
        for right in worker_groups[index + 1 :]:
            assert left.isdisjoint(right)
    assert {"approval_snapshots", "approval_action_revisions"} <= WORKER_APPEND_ONLY_TABLES
    assert {"context_memberships", "citation_bindings"} <= WORKER_APPEND_ONLY_TABLES
    assert "api_usage_buckets" in BOOTSTRAP_TABLES
    assert "api_usage_buckets" in WORKER_SELECT_TABLES
    record_predicate_operands(
        requirement_id="C6-P0-15",
        predicate_id="revision_append_only_role_enforced",
        subject_kind="role_table_grant_manifest",
        operands={
            "revision_table": "approval_action_revisions",
            "worker_append_only_tables": sorted(WORKER_APPEND_ONLY_TABLES),
            "worker_mutable_tables": sorted(WORKER_MUTABLE_TABLES),
            "mutable_contains_revision": ("approval_action_revisions" in WORKER_MUTABLE_TABLES),
            "append_and_mutable_disjoint": WORKER_APPEND_ONLY_TABLES.isdisjoint(
                WORKER_MUTABLE_TABLES
            ),
        },
    )
    grants = expected_worker_table_grants()
    assert set(grants) == (
        WORKER_SELECT_TABLES
        | WORKER_MUTABLE_TABLES
        | V15_WORKER_MUTABLE_TABLES
        | V15_WORKER_SELECT_TABLES
        | WORKER_APPEND_ONLY_TABLES
        | WORKER_CHECKPOINT_TABLES
        | {"approval_requests", "proposal_records"}
    )
    assert {
        "status",
        "selected_revision_id",
        "selected_revision_number",
        "decided_at",
        "consumed_at",
        "status_version",
    } == WORKER_APPROVAL_UPDATE_COLUMNS
    assert {"status", "status_version"} == WORKER_PROPOSAL_UPDATE_COLUMNS


def test_proposal_cas_does_not_implicitly_write_outside_frozen_columns() -> None:
    assert ProposalRecord.__table__.c.updated_at.onupdate is None
    assert ApprovalRequest.__table__.c.updated_at.onupdate is None


def test_citation_binding_identity_is_membership_scoped_not_trace_scoped() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in CitationBinding.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("membership_id",) in unique_columns
    assert ("retrieval_trace_id", "selected_candidate_ordinal") not in unique_columns


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_runtime_role_negative_owner_and_public_surfaces_are_closed() -> None:
    roles = sorted(RUNTIME_ROLES)
    admin = create_async_engine(_url())
    role_engines = {role: create_async_engine(_url(role)) for role in roles}
    try:
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
            owner_rows = [
                list(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT rolname,rolbypassrls,rolsuper,rolcreaterole,rolcreatedb,"
                            "pg_has_role(rolname,'supportguard_owner','MEMBER') "
                            "FROM pg_roles WHERE rolname=ANY(:roles) ORDER BY rolname"
                        ),
                        {"roles": roles},
                    )
                ).all()
            ]
            public_counts = list(
                (
                    await connection.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                            "ON n.oid=p.pronamespace WHERE n.nspname IN "
                            "('public','supportguard_control') AND p.proname LIKE "
                            "'supportguard_%' AND has_function_privilege("
                            "'public',p.oid,'EXECUTE')),"
                            "(SELECT count(*) FROM information_schema.role_table_grants "
                            "WHERE grantee='PUBLIC' AND table_schema IN "
                            "('public','supportguard_control')),"
                            "(SELECT count(*) FROM information_schema.role_usage_grants "
                            "WHERE grantee='PUBLIC' AND object_schema IN "
                            "('public','supportguard_control')),"
                            "(SELECT count(*) FROM pg_default_acl d CROSS JOIN LATERAL "
                            "aclexplode(d.defaclacl) a WHERE a.grantee=0)"
                        )
                    )
                ).one()
            )
        direct_table_sqlstates: list[str] = []
        owner_helper_sqlstates: list[str] = []
        cross_function_sqlstates: list[str] = []
        for role, engine in role_engines.items():
            async with engine.connect() as connection:
                with pytest.raises(exc.DBAPIError) as table_denied:
                    forbidden_table = (
                        "approval_requests" if role == "supportguard_bootstrap" else "users"
                    )
                    await connection.execute(
                        text(f"SELECT count(*) FROM {forbidden_table}")  # noqa: S608
                    )
                direct_table_sqlstates.append(str(getattr(table_denied.value.orig, "sqlstate", "")))
            async with engine.connect() as connection:
                with pytest.raises(exc.DBAPIError) as helper_denied:
                    await connection.execute(
                        text("SELECT supportguard_canonical_jsonb('{}'::jsonb)")
                    )
                owner_helper_sqlstates.append(
                    str(getattr(helper_denied.value.orig, "sqlstate", ""))
                )
            cross_statement = (
                "SELECT supportguard_action_mcp_propose_refund('{}'::jsonb,'{}'::jsonb)"
                if role == "supportguard_read_mcp"
                else "SELECT supportguard_read_mcp_query_account('{}'::jsonb,'{}'::jsonb)"
            )
            async with engine.connect() as connection:
                with pytest.raises(exc.DBAPIError) as cross_denied:
                    await connection.execute(text(cross_statement))
                cross_function_sqlstates.append(
                    str(getattr(cross_denied.value.orig, "sqlstate", ""))
                )
        target_sqlstate = "42501"
        assert all(row[1:] == [False, False, False, False, False] for row in owner_rows)
        assert public_counts == [0, 0, 0, 0]
        assert direct_table_sqlstates == [target_sqlstate] * len(roles)
        assert owner_helper_sqlstates == [target_sqlstate] * len(roles)
        assert cross_function_sqlstates == [target_sqlstate] * len(roles)
        shared = {
            "database_name": environment[0],
            "backend_pid": environment[1],
            "server_version_num": environment[2],
            "transaction_snapshot": environment[3],
            "target_roles": roles,
            "target_forbidden_sqlstate": target_sqlstate,
            "expected_role_count": len(roles),
        }
        record_predicate_operands(
            requirement_id="C6-P0-08",
            predicate_id="role_negative_surface_complete",
            subject_kind="postgres_runtime_role_negative_surface",
            operands={
                **shared,
                "direct_table_sqlstates": direct_table_sqlstates,
                "owner_helper_sqlstates": owner_helper_sqlstates,
                "cross_function_sqlstates": cross_function_sqlstates,
            },
        )
        role_negative_operands = {
            **shared,
            "direct_table_sqlstates": direct_table_sqlstates,
            "owner_helper_sqlstates": owner_helper_sqlstates,
            "cross_function_sqlstates": cross_function_sqlstates,
            "expected_sqlstates": [target_sqlstate] * len(roles),
            "expected_public_counts": [0, 0, 0, 0],
            "actual_public_counts": public_counts,
        }
        record_predicate_operands(
            requirement_id="C4-P0-01d",
            predicate_id="c4_p0_01d",
            subject_kind="postgres_runtime_role_negative_surface",
            operands=role_negative_operands,
        )
        record_predicate_operands(
            requirement_id="C4-P0-08b",
            predicate_id="c4_p0_08b",
            subject_kind="postgres_runtime_role_negative_surface",
            operands=role_negative_operands,
        )
        record_predicate_operands(
            requirement_id="C6-P0-08",
            predicate_id="runtime_owner_bypass_zero",
            subject_kind="postgres_runtime_owner_bypass_matrix",
            operands={
                **shared,
                "expected_owner_rows": [
                    [role, False, False, False, False, False] for role in roles
                ],
                "actual_owner_rows": owner_rows,
                "owner_bypass_count": sum(any(row[1:]) for row in owner_rows),
            },
        )
        record_predicate_operands(
            requirement_id="C6-P0-08",
            predicate_id="public_and_default_privilege_closed",
            subject_kind="postgres_public_default_privilege_surface",
            operands={
                **shared,
                "expected_public_counts": [0, 0, 0, 0],
                "actual_public_counts": public_counts,
            },
        )
    finally:
        for engine in role_engines.values():
            await engine.dispose()
        await admin.dispose()
