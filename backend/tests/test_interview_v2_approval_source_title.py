from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import exc, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import seed_business_facts
from supportguard.api.projections import _sqlite_approval_source
from supportguard.contracts.context import RequestContext
from supportguard.db.models import (
    ApprovalRequest,
    ConversationTurn,
    SupportTicket,
)
from supportguard.db.session import create_scoped_session_factory
from supportguard.services.commands import CommandCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict


def _coordinator(db_session):
    return CommandCoordinator(
        db_session,
        provider_identity=("deterministic-fake", "fake", "native_fixture"),
    )


def _scope(prefix: str, *, approver: bool = False, tenant_id: str = "tenant_demo"):
    return RequestContext(
        tenant_id=tenant_id,
        authenticated_actor_id=("user_approver_demo" if approver else "cust_demo"),
        authenticated_actor_role=("support_approver" if approver else "customer_member"),
        subject_customer_id=None if approver else "cust_demo",
        request_id=f"request-{prefix}",
        trace_id=f"trace-{prefix}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


async def _accept_turn(db_session, ticket_id: str, key: str, message: str):
    return await _coordinator(db_session).accept_message(
        ticket_id=ticket_id,
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key=key,
        message=message,
        trace_id=f"trace-{key}",
    )


async def test_sqlite_approval_source_is_origin_bounded_and_keyset_paginated(db_session):
    await seed_business_facts(db_session)
    first = await _coordinator(db_session).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="source-first",
        message="第一条支持问题",
        trace_id="trace-source-first",
    )
    second = await _accept_turn(db_session, first.ticket_id, "source-second", "第二条支持问题")
    third = await _accept_turn(db_session, first.ticket_id, "source-origin", "需要审批的第三条")
    origin_turn = await db_session.scalar(
        select(ConversationTurn).where(ConversationTurn.run_id == third.run_id)
    )
    assert origin_turn is not None and third.run_id is not None
    approval = ApprovalRequest(
        id="approval_source_window",
        tenant_id="tenant_demo",
        ticket_id=first.ticket_id,
        customer_id="cust_demo",
        run_id=third.run_id,
        origin_turn_id=origin_turn.id,
        action_type="refund",
        resource_type="billing_record_id",
        resource_id="bill_demo_duplicate",
        action_payload={"billing_record_id": "bill_demo_duplicate"},
        review_context={},
        action_hash="a" * 64,
        business_version=2,
        status="pending",
        idempotency_key="approval-source-window",
    )
    db_session.add(approval)
    await db_session.flush()
    await _accept_turn(db_session, first.ticket_id, "source-after", "审批之后的消息")

    initial = await _sqlite_approval_source(db_session, approval, limit=2)
    assert initial is not None
    assert initial["origin_turn_id"] == origin_turn.id
    assert initial["returned"] == 2
    assert initial["has_more"] is True
    assert any(item["is_origin_turn"] for item in initial["messages"])
    assert "审批之后的消息" not in {item["content"] for item in initial["messages"]}
    assert initial["next_before_sequence"] is not None
    assert initial["next_before_message_id"] is not None

    older = await _sqlite_approval_source(
        db_session,
        approval,
        before_sequence=initial["next_before_sequence"],
        before_message_id=initial["next_before_message_id"],
        limit=2,
    )
    assert older is not None
    assert older["messages"]
    assert max(item["sequence"] for item in older["messages"]) < min(
        item["sequence"] for item in initial["messages"]
    )
    assert not (
        {item["id"] for item in initial["messages"]} & {item["id"] for item in older["messages"]}
    )
    assert second.run_id is not None


async def test_sqlite_approval_source_rejects_unbound_cursor(db_session):
    await seed_business_facts(db_session)
    accepted = await _coordinator(db_session).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="cursor-first",
        message="需要支持",
        trace_id="trace-cursor-first",
    )
    turn = await db_session.scalar(
        select(ConversationTurn).where(ConversationTurn.run_id == accepted.run_id)
    )
    assert turn is not None and accepted.run_id is not None
    approval = ApprovalRequest(
        id="approval_cursor_conflict",
        tenant_id="tenant_demo",
        ticket_id=accepted.ticket_id,
        customer_id="cust_demo",
        run_id=accepted.run_id,
        origin_turn_id=turn.id,
        action_type="refund",
        resource_type="billing_record_id",
        resource_id="bill_demo_duplicate",
        action_payload={"billing_record_id": "bill_demo_duplicate"},
        review_context={},
        action_hash="b" * 64,
        business_version=2,
        status="pending",
        idempotency_key="approval-cursor-conflict",
    )
    db_session.add(approval)
    await db_session.flush()

    with pytest.raises(RuntimeConflict, match="approval_source_cursor_conflict"):
        await _sqlite_approval_source(
            db_session,
            approval,
            before_sequence=999,
            before_message_id="msg_missing",
        )


async def test_first_substantive_message_persists_over_greeting_title(db_session):
    await seed_business_facts(db_session)
    accepted = await _coordinator(db_session).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="title-greeting",
        message="'Hi!'",
        trace_id="trace-title-greeting",
    )
    ticket = await db_session.get(SupportTicket, accepted.ticket_id)
    assert ticket is not None and ticket.title == "'Hi!'"

    question = "余额充足但 atlas-chat 返回 429，为什么？"
    await _accept_turn(db_session, accepted.ticket_id, "title-question", question)
    assert ticket.title == question

    await _accept_turn(db_session, accepted.ticket_id, "title-later", "后续补充不应改标题")
    assert ticket.title == question


@pytest.mark.postgres
async def test_postgres_approval_source_and_greeting_title_match_sqlite_contract() -> None:
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
    setup = create_async_engine(setup_url)
    api = create_async_engine(api_url)
    api_factory = create_scoped_session_factory(api)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_v20_source_{suffix}"
    greeting_ticket_id = f"ticket_v20_title_{suffix}"
    origin_message_id = f"msg_v20_origin_{suffix}"
    run_id = f"run_v20_source_{suffix}"
    proposal_id = f"proposal_v20_source_{suffix}"
    approval_id = f"approval_v20_source_{suffix}"
    resource_id = f"bill_v20_source_{suffix}"
    try:
        async with setup.begin() as connection:
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
                    "0,'active','human_queue','审批来源测试',0,now(),0), ("
                    ":greeting_ticket,'cust_demo','open','general','low',1,'tenant_demo',"
                    "0,'active','human_queue',:greeting_title,0,now(),0)"
                ),
                {
                    "ticket": ticket_id,
                    "greeting_ticket": greeting_ticket_id,
                    "greeting_title": "'Hi!'",
                },
            )
            source_messages = [
                (f"msg_v20_old_{index:03d}_{suffix}", f"更早的问题 {index:03d}")
                for index in range(103)
            ]
            source_messages.append((origin_message_id, "需要审批的原始问题"))
            for message_id, content in source_messages:
                await connection.execute(
                    text(
                        "INSERT INTO ticket_messages("
                        "id,ticket_id,role,content,source_refs,tenant_id,message_kind"
                        ") VALUES ("
                        ":message_id,:ticket,'user',:content,"
                        "CAST('[]' AS json),'tenant_demo','customer')"
                    ),
                    {"message_id": message_id, "ticket": ticket_id, "content": content},
                )
            origin_turn_id = await connection.scalar(
                text(
                    "SELECT id FROM conversation_turns "
                    "WHERE tenant_id='tenant_demo' AND customer_message_id=:message"
                ),
                {"message": origin_message_id},
            )
            assert isinstance(origin_turn_id, str)
            await connection.execute(
                text(
                    "INSERT INTO agent_runs("
                    "id,tenant_id,ticket_id,customer_id,message_id,status,status_version,"
                    "next_run_sequence,canonical_checkpoint_version,checkpoint_stage,"
                    "step_index,tool_rounds,tool_attempts,llm_calls,model,provider_mode,"
                    "tool_call_mode,prompt_version,schema_version,context_version"
                    ") VALUES ("
                    ":run,'tenant_demo',:ticket,'cust_demo',:message,'interrupted',1,"
                    "0,0,'awaiting_approval',0,0,0,0,'deterministic-fake','fake',"
                    "'native_fixture','prompt-v20','schema-v20','context-v20')"
                ),
                {"run": run_id, "ticket": ticket_id, "message": origin_message_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO proposal_records("
                    "id,tenant_id,run_id,proposal_identity,action_type,resource_id,"
                    "resource_version,action_payload,observation_binding,action_hash,"
                    "status,status_version"
                    ") VALUES ("
                    ":proposal,'tenant_demo',:run,:identity,'refund',"
                    "CAST(:resource AS text),2,"
                    "pg_catalog.jsonb_build_object("
                    "'billing_record_id',CAST(:resource AS text))::json,"
                    "CAST('[]' AS json),:action_hash,'bound',1)"
                ),
                {
                    "proposal": proposal_id,
                    "run": run_id,
                    "identity": f"proposal-v20-{suffix}",
                    "resource": resource_id,
                    "action_hash": "c" * 64,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_requests("
                    "id,tenant_id,ticket_id,customer_id,proposal_id,run_id,action_type,resource_type,"
                    "resource_id,origin_turn_id,action_payload,review_context,action_hash,"
                    "business_version,status,idempotency_key,status_version,checkpoint_version"
                    ") VALUES ("
                    ":approval,'tenant_demo',:ticket,'cust_demo',:proposal,:run,'refund',"
                    "'billing_record_id',CAST(:resource AS text),:turn,"
                    "pg_catalog.jsonb_build_object("
                    "'billing_record_id',CAST(:resource AS text))::json,"
                    "CAST('{}' AS json),:action_hash,2,'pending',:idempotency,1,0)"
                ),
                {
                    "approval": approval_id,
                    "ticket": ticket_id,
                    "proposal": proposal_id,
                    "run": run_id,
                    "resource": resource_id,
                    "turn": origin_turn_id,
                    "action_hash": "c" * 64,
                    "idempotency": f"approval-v20-{suffix}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO ticket_messages("
                    "id,ticket_id,role,content,source_refs,tenant_id,message_kind"
                    ") VALUES ("
                    ":message_id,:ticket,'user','审批之后的消息',"
                    "CAST('[]' AS json),'tenant_demo','customer')"
                ),
                {"message_id": f"msg_v20_after_{suffix}", "ticket": ticket_id},
            )

        async with api_factory.request(_scope(f"source-{suffix}", approver=True)) as session:
            initial = await session.scalar(
                text("SELECT supportguard_api_get_approval_source(:approval,NULL,NULL,2)"),
                {"approval": approval_id},
            )
            assert isinstance(initial, dict)
            older = await session.scalar(
                text(
                    "SELECT supportguard_api_get_approval_source(:approval,:sequence,:message_id,2)"
                ),
                {
                    "approval": approval_id,
                    "sequence": initial["next_before_sequence"],
                    "message_id": initial["next_before_message_id"],
                },
            )
        assert isinstance(older, dict)
        assert any(item["is_origin_turn"] for item in initial["messages"])
        assert "审批之后的消息" not in {item["content"] for item in initial["messages"]}
        assert older["messages"]

        async with api_factory.request(_scope(f"large-source-{suffix}", approver=True)) as session:
            page_one = await session.scalar(
                text("SELECT supportguard_api_get_approval_source(:approval,NULL,NULL,100)"),
                {"approval": approval_id},
            )
            assert isinstance(page_one, dict)
            page_two = await session.scalar(
                text(
                    "SELECT supportguard_api_get_approval_source("
                    ":approval,:sequence,:message_id,100)"
                ),
                {
                    "approval": approval_id,
                    "sequence": page_one["next_before_sequence"],
                    "message_id": page_one["next_before_message_id"],
                },
            )
        assert isinstance(page_two, dict)
        assert page_one["returned"] == 100 and page_one["has_more"] is True
        assert page_two["returned"] == 4 and page_two["has_more"] is False
        assert page_two["next_before_sequence"] is None
        assert page_two["next_before_message_id"] is None
        page_one_ids = {item["id"] for item in page_one["messages"]}
        page_two_ids = {item["id"] for item in page_two["messages"]}
        assert page_one_ids.isdisjoint(page_two_ids)
        assert len(page_one_ids | page_two_ids) == 104
        for page in (page_one, page_two):
            ordering = [(item["sequence"], item["id"]) for item in page["messages"]]
            assert ordering == sorted(ordering)

        async with api_factory.request(_scope(f"stale-cursor-{suffix}", approver=True)) as session:
            stale = await session.scalar(
                text(
                    "SELECT supportguard_api_get_approval_source("
                    ":approval,:sequence,'msg_not_in_source_window',100)"
                ),
                {
                    "approval": approval_id,
                    "sequence": page_one["next_before_sequence"],
                },
            )
        assert stale == {"error_code": "approval_source_cursor_conflict"}

        async with api.connect() as connection:
            with pytest.raises(exc.DBAPIError) as denied:
                await connection.execute(
                    text("SELECT supportguard_api_get_approval_source(:approval)"),
                    {"approval": approval_id},
                )
        assert str(getattr(denied.value.orig, "sqlstate", "")) == "42501"

        async with api_factory.request(_scope(f"customer-source-{suffix}")) as session:
            with pytest.raises(exc.DBAPIError) as customer_denied:
                await session.execute(
                    text("SELECT supportguard_api_get_approval_source(:approval,NULL,NULL,100)"),
                    {"approval": approval_id},
                )
        assert str(getattr(customer_denied.value.orig, "sqlstate", "")) == "42501"

        async with api_factory.request(
            _scope(f"cross-{suffix}", approver=True, tenant_id="tenant_other")
        ) as session:
            foreign = await session.scalar(
                text("SELECT supportguard_api_get_approval_source(:approval,NULL,NULL,2)"),
                {"approval": approval_id},
            )
        assert foreign is None

        question = "余额充足但 atlas-chat 返回 429，为什么？"
        async with api_factory.request(_scope(f"title-{suffix}")) as session, session.begin():
            await CommandCoordinator(
                session,
                provider_identity=("deterministic-fake", "fake", "native_fixture"),
            ).accept_message(
                ticket_id=greeting_ticket_id,
                customer_id="cust_demo",
                principal_id="cust_demo",
                idempotency_key=f"title-v20-{suffix}",
                message=question,
                trace_id=f"trace-title-{suffix}",
            )
        async with setup.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE supportguard_owner"))
            await connection.execute(text("SET LOCAL app.tenant_id='tenant_demo'"))
            persisted_title = await connection.scalar(
                text("SELECT title FROM support_tickets WHERE id=:ticket"),
                {"ticket": greeting_ticket_id},
            )
        assert persisted_title == question
    finally:
        await setup.dispose()
        await api.dispose()
