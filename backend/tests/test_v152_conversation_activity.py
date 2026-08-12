from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def _accept_request(*, suffix: str, ticket_id: str) -> dict[str, object]:
    return {
        "schema_version": "api-accept-ticket.v1",
        "customer_id": "cust_demo",
        "principal_id": "user_customer_demo",
        "idempotency_key": f"idem-{suffix}",
        "message": f"conversation activity {suffix}",
        "trace_id": f"trace-{suffix}",
        "idempotency_id": f"idem_{suffix}",
        "ticket_id": ticket_id,
        "message_id": f"msg_{suffix}",
        "run_id": f"run_{suffix}",
        "job_id": f"job_{suffix}",
        "outbox_id": f"outbox_{suffix}",
        "delivery_id": f"delivery_{suffix}",
        "audit_id": f"audit_{suffix}",
        "model": "deterministic-fake",
        "provider_mode": "fake",
        "tool_call_mode": "native_fixture",
        "prompt_version": "agent_decide.v3",
        "agent_schema_version": "agent.v1",
        "context_version": "context.v1",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_conversation_clock_ignores_internal_updates_and_tracks_messages() -> None:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required")
    api_url = (
        make_url(raw_url)
        .set(username="supportguard_api", password="supportguard_api")  # noqa: S106
        .render_as_string(hide_password=False)
    )
    owner_engine = create_async_engine(raw_url)
    api_engine = create_async_engine(api_url)
    suffix = uuid4().hex[:10]
    older = f"ticket_activity_old_{suffix}"
    newer = f"ticket_activity_new_{suffix}"
    try:
        async with api_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(
                text("SELECT set_config('app.principal_id','user_customer_demo',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.principal_role','customer_admin',true)")
            )
            for label, ticket_id in (("old", older), ("new", newer)):
                request = _accept_request(suffix=f"{label}_{suffix}", ticket_id=ticket_id)
                accepted = await connection.scalar(
                    text("SELECT supportguard_api_accept_ticket(CAST(:request AS jsonb))"),
                    {"request": json.dumps(request, sort_keys=True, separators=(",", ":"))},
                )
                assert isinstance(accepted, dict) and not accepted.get("error_code")
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE support_tickets SET "
                    "last_message_at=clock_timestamp()-interval '2 minutes',"
                    "updated_at=clock_timestamp()+interval '1 day' WHERE id=:ticket"
                ),
                {"ticket": older},
            )
            await connection.execute(
                text(
                    "UPDATE support_tickets SET "
                    "last_message_at=clock_timestamp()-interval '1 minute' WHERE id=:ticket"
                ),
                {"ticket": newer},
            )
        async with api_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            listing = await connection.scalar(
                text("SELECT supportguard_api_list_conversations('cust_demo',:query,NULL,30)"),
                {"query": suffix},
            )
        ids = [item["id"] for item in listing["items"]]
        assert ids.index(newer) < ids.index(older)

        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO ticket_messages("
                    "id,tenant_id,ticket_id,message_kind,role,content,source_refs,created_at,updated_at"
                    ") VALUES ("
                    ":id,'tenant_demo',:ticket,'action_update','action',:content,'[]'::jsonb,"
                    "clock_timestamp()+interval '1 minute',clock_timestamp()+interval '1 minute')"
                ),
                {
                    "id": f"msg_activity_{suffix}",
                    "ticket": older,
                    "content": "审批结果已更新。",
                },
            )
        async with api_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            listing = await connection.scalar(
                text("SELECT supportguard_api_list_conversations('cust_demo',:query,NULL,30)"),
                {"query": suffix},
            )
        ids = [item["id"] for item in listing["items"]]
        assert ids.index(older) < ids.index(newer)
    finally:
        await api_engine.dispose()
        await owner_engine.dispose()
