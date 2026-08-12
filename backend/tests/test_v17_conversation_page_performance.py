from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.api.endpoints import conversations


def test_conversation_detail_uses_one_database_capability_round_trip() -> None:
    source = inspect.getsource(conversations.get_conversation)
    assert source.count("await session.scalar(") == 2
    # One scalar belongs to the PostgreSQL capability path and one to the
    # SQLite-only ticket lookup.  Citation projection is part of the page
    # capability and must not scale with the number of returned turns.
    postgres_branch = source.split('dialect.name == "postgresql"', 1)[1].split(
        "        ticket = await session.scalar(", 1
    )[0]
    assert postgres_branch.count("await session.scalar(") == 1
    assert "supportguard_api_get_run_citations" not in postgres_branch


@pytest.mark.postgres
async def test_postgres_page_capability_bounds_turns_and_preserves_cursor() -> None:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required")
    setup_url = make_url(raw_url).set(
        username="supportguard_migrator",
        password="supportguard_migrator",  # noqa: S106
    )
    api_url = make_url(raw_url).set(
        username="supportguard_api",
        password="supportguard_api",  # noqa: S106
    )
    setup_engine = create_async_engine(setup_url)
    api_engine = create_async_engine(api_url)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_v17_{suffix}"
    try:
        # The fixture uses the migration owner so this read-path contract is
        # independent of queue capacity, Worker heartbeats, and Provider state.
        async with setup_engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE supportguard_owner"))
            await connection.execute(text("SET LOCAL app.tenant_id='tenant_demo'"))
            await connection.execute(
                text(
                    "INSERT INTO support_tickets("
                    "id,customer_id,status,issue_type,risk,version,tenant_id,"
                    "next_event_sequence,lifecycle,automation_mode,title,"
                    "next_message_sequence,last_message_at,next_dispatch_sequence"
                    ") VALUES ("
                    ":ticket,'cust_demo','open','general','low',1,'tenant_demo',"
                    "1,'active','human_queue','v1.7 page fixture',0,now(),0)"
                ),
                {"ticket": ticket_id},
            )
            for ordinal in (1, 2, 3):
                await connection.execute(
                    text(
                        "INSERT INTO ticket_messages("
                        "id,ticket_id,role,content,source_refs,tenant_id,message_kind"
                        ") VALUES ("
                        ":message_id,:ticket,'user',:content,"
                        "CAST('[]' AS json),'tenant_demo','customer')"
                    ),
                    {
                        "message_id": f"msg_v17_{suffix}_{ordinal}",
                        "ticket": ticket_id,
                        "content": f"conversation page fixture {ordinal}",
                    },
                )

        async with api_engine.begin() as connection:
            await connection.execute(text("SET LOCAL app.tenant_id='tenant_demo'"))
            await connection.execute(text("SET LOCAL app.principal_id='user_customer_demo'"))
            await connection.execute(text("SET LOCAL app.principal_role='customer_admin'"))
            newest = await connection.scalar(
                text("SELECT supportguard_api_get_conversation_page(:customer,:ticket,NULL,2)"),
                {"customer": "cust_demo", "ticket": ticket_id},
            )
            older = await connection.scalar(
                text("SELECT supportguard_api_get_conversation_page(:customer,:ticket,2,2)"),
                {"customer": "cust_demo", "ticket": ticket_id},
            )

        assert isinstance(newest, dict) and isinstance(older, dict)
        assert [turn["ordinal"] for turn in newest["turns"]] == [2, 3]
        assert newest["turn_pagination"] == {
            "limit": 2,
            "returned": 2,
            "has_more": True,
            "next_before_ordinal": 2,
        }
        assert [turn["ordinal"] for turn in older["turns"]] == [1]
        assert older["turn_pagination"] == {
            "limit": 2,
            "returned": 1,
            "has_more": False,
            "next_before_ordinal": None,
        }
        assert all(isinstance(turn["citations"], list) for turn in newest["turns"])
        assert all(isinstance(turn["citations"], list) for turn in older["turns"])
    finally:
        await setup_engine.dispose()
        await api_engine.dispose()
