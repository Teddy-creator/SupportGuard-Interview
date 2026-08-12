from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.config import Settings
from supportguard.contracts.context import RequestContext
from supportguard.db.permissions import configure_local_mcp_roles
from supportguard.db.session import create_scoped_session_factory

pytestmark = pytest.mark.postgres


def _role_url(role: str):
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    return make_url(admin_url).set(username=role, password=role)


@pytest.mark.asyncio
async def test_scoped_api_session_binds_customer_identity_for_product_readers() -> None:
    engine = create_async_engine(_role_url("supportguard_api"))
    factory = create_scoped_session_factory(engine)
    context = RequestContext(
        tenant_id="tenant_demo",
        authenticated_actor_id="user_customer_demo",
        authenticated_actor_role="customer_admin",
        request_id="request-product-reader-scope",
        trace_id="trace-product-reader-scope",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        subject_customer_id="cust_demo",
    )
    try:
        async with factory.request(context) as session:
            scope = tuple(
                (
                    await session.execute(
                        text(
                            "SELECT current_setting('app.tenant_id',true),"
                            "current_setting('app.principal_id',true),"
                            "current_setting('app.principal_role',true),"
                            "current_setting('app.subject_customer_id',true)"
                        )
                    )
                ).one()
            )
            tickets = await session.scalar(
                text("SELECT supportguard_api_list_tickets(:customer_id,10)"),
                {"customer_id": "cust_demo"},
            )
        assert scope == (
            "tenant_demo",
            "user_customer_demo",
            "customer_admin",
            "cust_demo",
        )
        assert isinstance(tickets, list)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restricted_keyword_search_ranks_declared_heading_ngram_matches() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin = create_async_engine(admin_url)
    try:
        async with admin.connect() as connection:
            definition = str(
                await connection.scalar(
                    text(
                        "SELECT pg_get_functiondef("
                        "'supportguard_read_mcp_search_execute(jsonb,jsonb)'::regprocedure)"
                    )
                )
            )
        assert "SELECT count(*)::real" in definition
        assert "jsonb_array_elements_text(ep->'keyword_terms')" in definition
        assert "c.section_path" in definition
        assert (
            "row_number() OVER (PARTITION BY d.status ORDER BY "
            "(ts_rank_cd(c.search_vector" in definition
        )
        assert (
            "WHERE rank<=LEAST((ep->>'limit')::integer,20)" in definition
        )
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_mcp_role_reconfiguration_never_reopens_owner_bootstrap_surface() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    await configure_local_mcp_roles(Settings(database_url=admin_url))
    admin = create_async_engine(admin_url)
    try:
        async with admin.connect() as connection:
            bootstrap_function = await connection.scalar(
                text("SELECT to_regprocedure('public.supportguard_bootstrap_transfer_ownership()')")
            )
            migrator_can_create = bool(
                await connection.scalar(
                    text("SELECT has_schema_privilege('supportguard_migrator','public','CREATE')")
                )
            )
        assert bootstrap_function is None
        assert migrator_can_create is False
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_runtime_logins_are_non_owner_non_superuser_and_rls_isolates() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    admin = create_async_engine(admin_url)
    roles = (
        "supportguard_api",
        "supportguard_dispatcher",
        "supportguard_reconciler",
        "supportguard_worker",
        "supportguard_read_mcp",
        "supportguard_action_mcp",
        "supportguard_bootstrap",
        "supportguard_maintenance",
    )
    async with admin.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
                    "WHERE rolname = ANY(:roles) ORDER BY rolname"
                ),
                {"roles": list(roles)},
            )
        ).all()
        assert {row.rolname for row in rows} == set(roles)
        assert all(
            not row.rolsuper and not row.rolbypassrls and not row.rolcreaterole for row in rows
        )
        legacy = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_roles "
                "WHERE rolname IN ('supportguard_read','supportguard_action')"
            )
        )
        assert legacy == 0
        owner = await connection.scalar(
            text(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE relname='customers' AND relkind='r'"
            )
        )
        assert owner == "supportguard_owner"
    await admin.dispose()

    api = create_async_engine(_role_url("supportguard_api"))
    async with api.connect() as connection:
        with pytest.raises(exc.ProgrammingError):
            async with connection.begin():
                await connection.execute(text("SELECT count(*) FROM customers"))
        async with connection.begin():
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            assert await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_demo')")
            )
            assert not await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_other')")
            )
        async with connection.begin():
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_other',true)"))
            assert await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_other')")
            )
            assert not await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_demo')")
            )
    await api.dispose()


@pytest.mark.asyncio
async def test_database_rejects_cross_tenant_customer_resource_mismatch() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(admin_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(exc.IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO support_tickets "
                        "(id, tenant_id, customer_id, status, issue_type, risk, version, "
                        "next_event_sequence) VALUES "
                        "('ticket_cross_tenant_probe','tenant_other','cust_demo','open',"
                        "'unknown','low',1,0)"
                    )
                )
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_action_mcp_has_no_direct_observation_binding_table_access() -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url("supportguard_action_mcp"))
    try:
        for table in ("turn_groups", "tool_invocations", "tool_observations"):
            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(exc.ProgrammingError):
                    await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                await transaction.rollback()
        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(
                    text("UPDATE tool_observations SET status=status WHERE tenant_id='tenant_demo'")
                )
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_query_capabilities_are_tenant_scoped_and_typed() -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url("supportguard_api"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(text("SELECT set_config('app.principal_id','cust_demo',true)"))
            await connection.execute(
                text("SELECT set_config('app.principal_role','customer_member',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.subject_customer_id','cust_demo',true)")
            )
            assert await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_demo')")
            )
            assert not await connection.scalar(
                text("SELECT supportguard_api_customer_exists('cust_other')")
            )
            tickets = await connection.scalar(
                text("SELECT supportguard_api_list_tickets('cust_demo',10)")
            )
            assert isinstance(tickets, list) and len(tickets) <= 10
            invalid = await connection.begin_nested()
            with pytest.raises(exc.DBAPIError, match="api_limit_invalid"):
                await connection.execute(
                    text("SELECT supportguard_api_list_tickets('cust_demo',0)")
                )
            await invalid.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "statement"),
    [
        (
            "supportguard_read_mcp",
            "INSERT INTO proposal_records (id) VALUES ('forbidden')",
        ),
        (
            "supportguard_action_mcp",
            "SELECT count(*) FROM knowledge_chunks",
        ),
        (
            "supportguard_dispatcher",
            "SELECT count(*) FROM customers",
        ),
        (
            "supportguard_maintenance",
            "DELETE FROM agent_events WHERE false",
        ),
        (
            "supportguard_action_mcp",
            "UPDATE audit_events SET event_type=event_type WHERE false",
        ),
        (
            "supportguard_api",
            "DELETE FROM human_decisions WHERE false",
        ),
        (
            "supportguard_worker",
            "UPDATE policy_capability_results SET status=status WHERE false",
        ),
    ],
)
async def test_service_login_deny_matrix(role: str, statement: str) -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url(role))
    try:
        async with engine.begin() as connection:
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "service"),
    [
        ("supportguard_api", "api"),
        ("supportguard_dispatcher", "dispatcher"),
        ("supportguard_reconciler", "reconciler"),
        ("supportguard_worker", "worker"),
        ("supportguard_read_mcp", "read_mcp"),
        ("supportguard_action_mcp", "action_mcp"),
    ],
)
async def test_runtime_health_uses_capability_without_heartbeat_table_access(
    role: str, service: str
) -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url(role))
    instance = f"health-contract-{service}-{uuid4().hex}"
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                published = await connection.scalar(
                    text(
                        "SELECT supportguard_record_service_heartbeat("
                        ":instance,:service,'test-version')"
                    ),
                    {"instance": instance, "service": service},
                )
                assert published["healthy"] is True
                observed = await connection.scalar(
                    text(
                        "SELECT supportguard_record_service_heartbeat("
                        ":instance,:service,'__healthcheck__')"
                    ),
                    {"instance": instance, "service": service},
                )
                assert observed["healthy"] is True
            with pytest.raises(exc.ProgrammingError):
                async with connection.begin():
                    await connection.execute(text("SELECT * FROM service_instance_heartbeats"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_maintenance_login_can_only_use_fixed_retention_capability() -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    admin_url = os.getenv("TEST_DATABASE_URL")
    assert admin_url is not None
    engine = create_async_engine(
        make_url(admin_url).set(
            username="supportguard_maintenance",
            password="supportguard_maintenance",  # noqa: S106 - local role fixture
        )
    )
    try:
        async with engine.begin() as connection:
            plan = await connection.scalar(
                text("SELECT supportguard_maintenance_plan_pg_retention()")
            )
            assert plan["schema_version"] == "pg-retention-plan.v1"
            assert len(plan["plan_id"]) == 64
            applied = await connection.scalar(
                text("SELECT supportguard_maintenance_apply_pg_retention(:plan_id)"),
                {"plan_id": plan["plan_id"]},
            )
            assert applied["plan_id"] == plan["plan_id"]
            stale = await connection.begin_nested()
            with pytest.raises(exc.DBAPIError, match="retention_plan_stale"):
                await connection.execute(
                    text("SELECT supportguard_maintenance_apply_pg_retention(:plan_id)"),
                    {"plan_id": "0" * 64},
                )
            await stale.rollback()
            report = await connection.scalar(
                text("SELECT supportguard_maintenance_retention_report('missing-intent')")
            )
            assert report == {"found": False, "id": "missing-intent"}
            eligibility = await connection.scalar(
                text(
                    "SELECT supportguard_maintenance_trim_eligibility("
                    "'missing-job','missing-run','tenant_demo')"
                )
            )
            assert eligibility == {
                "eligible": False,
                "reason": "postgres_job_missing",
            }
            legacy = await connection.begin_nested()
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(
                    text(
                        "SELECT supportguard_run_retention("
                        "false, now() - interval '14 days', now() - interval '30 days')"
                    )
                )
            await legacy.rollback()
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(text("SELECT count(*) FROM runtime_jobs"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pg_retention_plan_is_content_addressed_and_protects_unlisted_facts() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    suffix = uuid4().hex[:12]
    first = f"heartbeat_retention_{suffix}_1"
    second = f"heartbeat_retention_{suffix}_2"
    audit_id = f"audit_retention_{suffix}"
    old = datetime.now(UTC) - timedelta(days=2)
    admin = create_async_engine(admin_url)
    maintenance = create_async_engine(_role_url("supportguard_maintenance"))
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO service_instance_heartbeats("
                    "id,service,capabilities,version,status,last_heartbeat_at,"
                    "timing_version,runtime_config_hash) "
                    "SELECT :id,'worker','[]'::json,'test','stopped',:old,"
                    "timing_version,config_hash FROM "
                    "supportguard_control.runtime_timing_snapshots WHERE is_active"
                ),
                {"id": first, "old": old},
            )
            await connection.execute(
                text(
                    "INSERT INTO queue_delivery_audits("
                    "id,redis_message_id,consumer_group,outcome,payload_hash,details) "
                    "VALUES (:id,:message,'maintenance-test','preserved',:hash,'{}'::json)"
                ),
                {"id": audit_id, "message": f"0-{suffix}", "hash": "a" * 64},
            )
        async with maintenance.begin() as connection:
            first_plan = await connection.scalar(
                text("SELECT supportguard_maintenance_plan_pg_retention()")
            )
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO service_instance_heartbeats("
                    "id,service,capabilities,version,status,last_heartbeat_at,"
                    "timing_version,runtime_config_hash) "
                    "SELECT :id,'worker','[]'::json,'test','stopped',:old,"
                    "timing_version,config_hash FROM "
                    "supportguard_control.runtime_timing_snapshots WHERE is_active"
                ),
                {"id": second, "old": old},
            )
        async with maintenance.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(exc.DBAPIError, match="retention_plan_stale"):
                await connection.execute(
                    text("SELECT supportguard_maintenance_apply_pg_retention(:id)"),
                    {"id": first_plan["plan_id"]},
                )
            await transaction.rollback()
            async with connection.begin():
                current = await connection.scalar(
                    text("SELECT supportguard_maintenance_plan_pg_retention()")
                )
                applied = await connection.scalar(
                    text("SELECT supportguard_maintenance_apply_pg_retention(:id)"),
                    {"id": current["plan_id"]},
                )
                assert applied["deleted"]["service_instance_heartbeats"] >= 2
        async with admin.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM service_instance_heartbeats WHERE id=ANY(:ids)"),
                    {"ids": [first, second]},
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM queue_delivery_audits WHERE id=:id"),
                    {"id": audit_id},
                )
                == 1
            )
    finally:
        async with admin.begin() as connection:
            await connection.execute(
                text("DELETE FROM queue_delivery_audits WHERE id=:id"), {"id": audit_id}
            )
            await connection.execute(
                text("DELETE FROM service_instance_heartbeats WHERE id=ANY(:ids)"),
                {"ids": [first, second]},
            )
        await maintenance.dispose()
        await admin.dispose()


@pytest.mark.asyncio
async def test_worker_can_revalidate_tenant_retrieval_trace_but_cannot_mutate_it() -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url("supportguard_worker"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(text("SELECT count(*) FROM retrieval_traces"))
            for statement in (
                "INSERT INTO retrieval_traces (id) VALUES ('forbidden')",
                "UPDATE retrieval_traces SET query_hash=query_hash WHERE false",
                "DELETE FROM retrieval_traces WHERE false",
            ):
                savepoint = await connection.begin_nested()
                with pytest.raises(exc.ProgrammingError):
                    await connection.execute(text(statement))
                await savepoint.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (
            "supportguard_dispatcher",
            "SELECT count(*) FROM supportguard_dispatcher_claim_outbox(1)",
            "SELECT count(*) FROM outbox_events",
        ),
        (
            "supportguard_reconciler",
            "SELECT count(*) FROM supportguard_reconciler_candidates(1)",
            "SELECT count(*) FROM runtime_jobs",
        ),
    ],
)
async def test_cross_tenant_control_plane_roles_only_receive_fixed_capabilities(
    role: str, allowed: str, denied: str
) -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url(role))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text(allowed))
            await transaction.rollback()
        async with engine.begin() as connection:
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(text(denied))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["supportguard_read_mcp", "supportguard_action_mcp"])
async def test_mcp_roles_cannot_call_internal_fence_or_mutate_runtime_tables(role: str) -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(_role_url(role))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(exc.ProgrammingError):
                await connection.scalar(
                    text(
                        "SELECT supportguard_mcp_verify_fence("
                        "'tenant_demo','missing_run','missing_job','missing_segment',1,1)"
                    )
                )
            await transaction.rollback()
        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(exc.ProgrammingError):
                await connection.execute(text("UPDATE runtime_jobs SET status=status WHERE false"))
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_rejects_cross_tenant_runtime_ledger_reference() -> None:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(admin_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(
                text(
                    "INSERT INTO support_tickets "
                    "(id,tenant_id,customer_id,status,issue_type,risk,version,"
                    "next_event_sequence) VALUES "
                    "('ticket_cross_tenant_ledger_v124','tenant_demo','cust_demo',"
                    "'open','test','low',1,0)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO ticket_messages "
                    "(id,tenant_id,ticket_id,role,content,source_refs) VALUES "
                    "('message_cross_tenant_ledger_v124','tenant_demo',"
                    "'ticket_cross_tenant_ledger_v124','user','fixture','[]'::json)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_runs "
                    "(id,tenant_id,ticket_id,customer_id,message_id,status,status_version,"
                    "next_run_sequence,canonical_checkpoint_version,checkpoint_stage,"
                    "step_index,tool_rounds,tool_attempts,llm_calls,model,provider_mode,"
                    "tool_call_mode,prompt_version,schema_version,context_version) VALUES "
                    "('run_cross_tenant_ledger_v124','tenant_demo',"
                    "'ticket_cross_tenant_ledger_v124','cust_demo',"
                    "'message_cross_tenant_ledger_v124','running',1,0,0,"
                    "'request_created',0,0,0,0,'fake','fake','native',"
                    "'agent_decide.v1','agent.v1','context.v1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO runtime_jobs "
                    "(id,tenant_id,run_id,kind,status,attempt,fencing_token) VALUES "
                    "('job_cross_tenant_ledger_v124','tenant_demo',"
                    "'run_cross_tenant_ledger_v124','agent_start','queued',0,0)"
                )
            )
            with pytest.raises(exc.IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO agent_call_attempts "
                        "(id,tenant_id,run_id,job_id,fencing_token,call_kind,ordinal,status) "
                        "VALUES ('attempt_cross_tenant_v124','tenant_other',:run_id,:job_id,"
                        "1,'llm',999,'started')"
                    ),
                    {
                        "run_id": "run_cross_tenant_ledger_v124",
                        "job_id": "job_cross_tenant_ledger_v124",
                    },
                )
        finally:
            await transaction.rollback()
    await engine.dispose()
