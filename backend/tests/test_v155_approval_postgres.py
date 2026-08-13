from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.services.commands import CommandCoordinator


def _role_url(base: str, role: str) -> str:
    return make_url(base).set(username=role, password=role).render_as_string(hide_password=False)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_pending_approval_is_not_squeezed_out_by_recent_terminal_history() -> None:
    base = os.getenv("TEST_DATABASE_URL")
    if not base or not make_url(base).drivername.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL is not configured for PostgreSQL")
    admin = create_async_engine(base)
    api = create_async_engine(_role_url(base, "supportguard_api"))
    api_factory = async_sessionmaker(api, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    pending_id = f"approval_v155_pending_{suffix}"
    try:
        async with api_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await session.execute(
                text("SELECT set_config('app.principal_id','user_customer_demo',true)")
            )
            await session.execute(
                text("SELECT set_config('app.principal_role','customer_admin',true)")
            )
            accepted = await CommandCoordinator(
                session,
                provider_identity=("deterministic-fake", "fake", "native_fixture"),
            ).accept_new_ticket(
                customer_id="cust_demo",
                principal_id="user_customer_demo",
                idempotency_key=f"v155-approval-queue-{suffix}",
                message="请检查我的待审批请求。",
                trace_id=f"trace-v155-approval-queue-{suffix}",
            )
        assert accepted.run_id is not None

        async with admin.begin() as connection:
            await connection.execute(text("SET LOCAL app.tenant_id='tenant_demo'"))
            await connection.execute(text("SET LOCAL app.principal_id='v155-approval-fixture'"))
            await connection.execute(text("SET LOCAL app.principal_role='system_worker'"))
            customer = (
                (
                    await connection.execute(
                        text("SELECT tenant_id,id FROM customers ORDER BY created_at,id LIMIT 1")
                    )
                )
                .mappings()
                .one()
            )
            tenant_id = str(customer["tenant_id"])
            customer_id = str(customer["id"])
            ticket_id = accepted.ticket_id
            await connection.execute(
                text(
                    "INSERT INTO proposal_records("
                    "id,tenant_id,run_id,proposal_identity,action_type,resource_id,"
                    "resource_version,action_payload,observation_binding,action_hash,"
                    "status,status_version,created_at,updated_at)"
                    " SELECT 'proposal_v155_terminal_' || :suffix || '_' || g,"
                    ":tenant_id,:run_id,"
                    "'api-key:v155:terminal:' || :suffix || ':' || g,"
                    "'api_key_revocation','key_terminal_' || g,1,"
                    "jsonb_build_object('api_key_id','key_terminal_' || g),"
                    "'[]'::jsonb,repeat('a',64),'bound',1,"
                    "now() + (g || ' seconds')::interval,"
                    "now() + (g || ' seconds')::interval"
                    " FROM generate_series(1,205) AS g"
                ),
                {
                    "suffix": suffix,
                    "tenant_id": tenant_id,
                    "run_id": accepted.run_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_requests("
                    "id,tenant_id,ticket_id,customer_id,proposal_id,run_id,"
                    "action_type,action_payload,review_context,action_hash,"
                    "business_version,status,idempotency_key,"
                    "created_at,updated_at)"
                    " SELECT 'approval_v155_terminal_' || :suffix || '_' || g,"
                    ":tenant_id,:ticket_id,:customer_id,"
                    "'proposal_v155_terminal_' || :suffix || '_' || g,:run_id,"
                    "'api_key_revocation',"
                    "jsonb_build_object('api_key_id','key_terminal_' || g),"
                    "'{\"risk\":\"high\"}'::jsonb,repeat('a',64),1,'pending',"
                    "'v155-terminal-' || :suffix || '-' || g,"
                    "now() + (g || ' seconds')::interval,"
                    "now() + (g || ' seconds')::interval"
                    " FROM generate_series(1,205) AS g"
                ),
                {
                    "suffix": suffix,
                    "tenant_id": tenant_id,
                    "ticket_id": ticket_id,
                    "customer_id": customer_id,
                    "run_id": accepted.run_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE approval_requests "
                    "SET status='rejected',status_version=status_version+1 "
                    "WHERE tenant_id=:tenant_id "
                    "AND id LIKE 'approval_v155_terminal_' || :suffix || '_%'"
                ),
                {"suffix": suffix, "tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO proposal_records("
                    "id,tenant_id,run_id,proposal_identity,action_type,resource_id,"
                    "resource_version,action_payload,observation_binding,action_hash,"
                    "status,status_version,created_at,updated_at)"
                    " VALUES(:id,:tenant_id,:run_id,:identity,'api_key_revocation',"
                    "'key_pending',1,"
                    "'{\"api_key_id\":\"key_pending\"}'::jsonb,'[]'::jsonb,"
                    "repeat('b',64),'bound',1,now()-interval '1 day',"
                    "now()-interval '1 day')"
                ),
                {
                    "id": f"proposal_v155_pending_{suffix}",
                    "tenant_id": tenant_id,
                    "run_id": accepted.run_id,
                    "identity": f"refund:v155:pending:{suffix}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_requests("
                    "id,tenant_id,ticket_id,customer_id,proposal_id,run_id,"
                    "action_type,action_payload,review_context,action_hash,"
                    "business_version,status,idempotency_key,"
                    "created_at,updated_at)"
                    " VALUES(:id,:tenant_id,:ticket_id,:customer_id,:proposal_id,"
                    ":run_id,'api_key_revocation',"
                    '\'{"api_key_id":"key_pending"}\'::jsonb,'
                    "'{\"risk\":\"high\"}'::jsonb,repeat('b',64),1,'pending',"
                    ":idempotency_key,now()-interval '1 day',now()-interval '1 day')"
                ),
                {
                    "id": pending_id,
                    "tenant_id": tenant_id,
                    "ticket_id": ticket_id,
                    "customer_id": customer_id,
                    "proposal_id": f"proposal_v155_pending_{suffix}",
                    "run_id": accepted.run_id,
                    "idempotency_key": f"v155-pending-{suffix}",
                },
            )

        async with api.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id',:value,true)"),
                {"value": tenant_id},
            )
            await connection.execute(
                text("SELECT set_config('app.principal_id','approver-v155',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.principal_role','approver',true)")
            )
            payload = await connection.scalar(text("SELECT supportguard_api_list_approvals(200)"))

        assert isinstance(payload, list)
        assert len(payload) == 200
        pending = next(item for item in payload if item["id"] == pending_id)
        pending_index = payload.index(pending)
        first_terminal_index = next(
            index for index, item in enumerate(payload) if item["status"] != "pending"
        )
        assert pending_index < first_terminal_index
        assert all(item["status"] == "pending" for item in payload[:first_terminal_index])
        assert pending["status"] == "pending"
        assert pending["resource_summary"] == "key_pending"
        assert pending["source_label"] == "请检查我的待审批请求。"
        assert isinstance(pending["conversation_action_sources"], dict)
        assert set(pending) == {
            "id",
            "ticket_id",
            "source_label",
            "status",
            "action_type",
            "resource_summary",
            "risk",
            "actionable",
            "conversation_action_sources",
            "created_at",
        }
        assert all("action_payload" not in item for item in payload)
        assert all("review_context" not in item for item in payload)
    finally:
        await api.dispose()
        await admin.dispose()
