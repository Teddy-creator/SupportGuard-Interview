from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

from conftest import seed_business_facts
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    ConversationTurn,
    HumanDecision,
    ProposalRecord,
    ProposalWithdrawal,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.commands import CommandCoordinator
from supportguard.services.conversation_lifecycle import ConversationLifecycleCoordinator
from supportguard.services.proposal_withdrawals import ProposalWithdrawalCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict


class _PostgresLifecycleSession:
    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def scalar(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        # PostgreSQL JSON rendering legitimately trims trailing fractional zeros.
        return {
            "conversation_id": "ticket_timestamp_boundary",
            "lifecycle": "archived",
            "accepted_at": "2026-08-12T17:25:57.7+00:00",
            "reused": False,
        }


async def _accept_new(db_session):
    return await CommandCoordinator(
        db_session, provider_identity=("deterministic-fake", "fake", "native_fixture")
    ).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        idempotency_key="v15-create",
        message="余额充足，为什么并发受限？",
        trace_id="trace-v15-create",
    )


async def test_postgres_lifecycle_accepts_database_json_timestamp_precision() -> None:
    coordinator = ConversationLifecycleCoordinator(_PostgresLifecycleSession())  # type: ignore[arg-type]

    accepted = await coordinator.transition(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        principal_id="user_customer_demo",
        conversation_id="ticket_timestamp_boundary",
        lifecycle="archived",
        idempotency_key="archive-timestamp-boundary",
        trace_id="trace-timestamp-boundary",
    )

    assert accepted.accepted_at == datetime(2026, 8, 12, 17, 25, 57, 700000, tzinfo=UTC)
    assert accepted.response()["accepted_at"] == "2026-08-12T17:25:57.700000+00:00"


async def test_first_message_atomically_creates_conversation_message_and_turn(db_session):
    await seed_business_facts(db_session)
    accepted = await _accept_new(db_session)
    ticket = await db_session.get(SupportTicket, accepted.ticket_id)
    messages = (
        await db_session.scalars(
            select(TicketMessage).where(TicketMessage.ticket_id == accepted.ticket_id)
        )
    ).all()
    turns = (
        await db_session.scalars(
            select(ConversationTurn).where(ConversationTurn.ticket_id == accepted.ticket_id)
        )
    ).all()

    assert ticket is not None
    assert ticket.lifecycle == "active"
    assert ticket.automation_mode == "agent"
    assert len(messages) == 1
    assert len(turns) == 1
    assert turns[0].customer_message_id == messages[0].id
    assert turns[0].run_id == accepted.run_id
    assert messages[0].turn_id == turns[0].id
    assert messages[0].conversation_sequence == 1


async def test_waiting_external_conversation_accepts_a_new_turn(db_session):
    await seed_business_facts(db_session)
    first = await _accept_new(db_session)
    first_run = await db_session.get(AgentRun, first.run_id)
    ticket = await db_session.get(SupportTicket, first.ticket_id)
    assert first_run is not None and ticket is not None
    first_run.status = "interrupted"
    first_run.checkpoint_stage = "awaiting_approval"
    ticket.status = "awaiting_approval"
    await db_session.flush()

    second = await CommandCoordinator(
        db_session, provider_identity=("deterministic-fake", "fake", "native_fixture")
    ).accept_message(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        idempotency_key="v15-followup",
        message="退款审批期间，通常多久到账？",
        trace_id="trace-v15-followup",
    )

    assert second.run_id != first.run_id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ConversationTurn)
            .where(ConversationTurn.ticket_id == ticket.id)
        )
        == 2
    )


async def test_human_queue_accepts_message_without_agent_run_or_job(db_session):
    await seed_business_facts(db_session)
    first = await _accept_new(db_session)
    ticket = await db_session.get(SupportTicket, first.ticket_id)
    assert ticket is not None
    ticket.automation_mode = "human_queue"
    await db_session.flush()
    run_count = await db_session.scalar(select(func.count()).select_from(AgentRun))
    job_count = await db_session.scalar(select(func.count()).select_from(RuntimeJob))

    accepted = await CommandCoordinator(
        db_session, provider_identity=("deterministic-fake", "fake", "native_fixture")
    ).accept_message(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        idempotency_key="v15-human-queue",
        message="补充一条信息",
        trace_id="trace-v15-human-queue",
    )

    assert accepted.status == "accepted"
    assert accepted.run_id is None and accepted.job_id is None
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == run_count
    assert await db_session.scalar(select(func.count()).select_from(RuntimeJob)) == job_count
    latest_turn = await db_session.scalar(
        select(ConversationTurn)
        .where(ConversationTurn.ticket_id == ticket.id)
        .order_by(ConversationTurn.ordinal.desc())
    )
    assert latest_turn is not None
    assert latest_turn.activity_state == "completed"
    assert latest_turn.result_state == "human_queue"


async def test_ordinary_turns_materialize_jobs_in_dispatch_order(db_session):
    await seed_business_facts(db_session)
    first = await _accept_new(db_session)
    ticket = await db_session.get(SupportTicket, first.ticket_id)
    assert ticket is not None

    second = await CommandCoordinator(
        db_session, provider_identity=("deterministic-fake", "fake", "native_fixture")
    ).accept_message(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        idempotency_key="v15-second-queued",
        message="这是紧接着发送的第二条消息",
        trace_id="trace-v15-second",
    )
    assert second.status == "queued"
    assert second.run_id is not None
    assert second.job_id is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ConversationTurn)
            .where(
                ConversationTurn.ticket_id == ticket.id,
                ConversationTurn.activity_state == "queued",
            )
        )
        == 2
    )
    jobs = (
        await db_session.scalars(
            select(RuntimeJob)
            .where(RuntimeJob.ticket_id == ticket.id)
            .order_by(RuntimeJob.dispatch_sequence)
        )
    ).all()
    assert [job.run_id for job in jobs] == [first.run_id, second.run_id]
    assert [job.dispatch_sequence for job in jobs] == [1, 2]
    assert [job.status for job in jobs] == ["queued", "queued"]


async def test_customer_withdrawal_is_idempotent_and_creates_no_runtime_action(db_session):
    await seed_business_facts(db_session)
    accepted = await _accept_new(db_session)
    run = await db_session.get(AgentRun, accepted.run_id)
    ticket = await db_session.get(SupportTicket, accepted.ticket_id)
    assert run is not None and ticket is not None
    proposal = ProposalRecord(
        id="proposal_v15_withdraw",
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        proposal_identity="refund:bill_demo_duplicate:v15",
        action_type="refund",
        resource_id="bill_demo_duplicate",
        resource_version=2,
        action_payload={"billing_record_id": "bill_demo_duplicate"},
        observation_binding=[],
        action_hash="a" * 64,
        status="bound",
    )
    approval = ApprovalRequest(
        id="approval_v15_withdraw",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        proposal_id=proposal.id,
        run_id=run.id,
        action_type="refund",
        resource_type="billing_record_id",
        resource_id=proposal.resource_id,
        origin_turn_id=str(run.turn_id),
        action_payload=proposal.action_payload,
        review_context={},
        action_hash=proposal.action_hash,
        business_version=2,
        status="pending",
        idempotency_key="proposal-v15-withdraw",
    )
    db_session.add_all([proposal, approval])
    await db_session.flush()
    coordinator = ProposalWithdrawalCoordinator(db_session)
    first = await coordinator.withdraw(
        tenant_id=ticket.tenant_id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        approval_id=approval.id,
        idempotency_key="withdraw-v15",
        reason="不再需要退款",
        trace_id="trace-v15-withdraw",
    )
    replay = await coordinator.withdraw(
        tenant_id=ticket.tenant_id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        approval_id=approval.id,
        idempotency_key="withdraw-v15",
        reason="不再需要退款",
        trace_id="trace-v15-withdraw-replay",
    )

    assert first.withdrawal_id == replay.withdrawal_id
    assert replay.reused is True
    assert approval.status == "withdrawn"
    assert proposal.status == "stale"
    assert await db_session.scalar(select(func.count()).select_from(ProposalWithdrawal)) == 1
    assert await db_session.scalar(select(func.count()).select_from(HumanDecision)) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RuntimeJob)
            .where(RuntimeJob.approval_id == approval.id)
        )
        == 0
    )
    action_update = await db_session.scalar(
        select(TicketMessage).where(TicketMessage.approval_id == approval.id)
    )
    assert action_update is not None and action_update.message_kind == "action_update"


async def test_archive_blocks_messages_until_idempotent_restore(db_session):
    await seed_business_facts(db_session)
    accepted = await _accept_new(db_session)
    ticket = await db_session.get(SupportTicket, accepted.ticket_id)
    assert ticket is not None
    coordinator = ConversationLifecycleCoordinator(db_session)
    archived = await coordinator.transition(
        tenant_id=ticket.tenant_id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        conversation_id=ticket.id,
        lifecycle="archived",
        idempotency_key="archive-v15",
        trace_id="trace-archive-v15",
    )
    assert archived.lifecycle == "archived"
    with pytest.raises(RuntimeConflict, match="conversation_archived"):
        async with db_session.begin_nested():
            await CommandCoordinator(db_session).accept_message(
                ticket_id=ticket.id,
                customer_id=ticket.customer_id,
                principal_id="user_customer_demo",
                idempotency_key="message-while-archived",
                message="归档后不应接受",
                trace_id="trace-message-archived",
            )
    restored = await ConversationLifecycleCoordinator(db_session).transition(
        tenant_id=ticket.tenant_id,
        customer_id=ticket.customer_id,
        principal_id="user_customer_demo",
        conversation_id=ticket.id,
        lifecycle="active",
        idempotency_key="restore-v15",
        trace_id="trace-restore-v15",
    )
    assert restored.lifecycle == "active"


@pytest.mark.postgres
async def test_postgres_conversation_queries_are_customer_and_tenant_scoped():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required")
    url = make_url(raw_url).set(
        username="supportguard_api",
        password="supportguard_api",  # noqa: S106
    )
    engine = create_async_engine(url)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_v15_{suffix}"
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(
                text("SELECT set_config('app.principal_id','user_customer_demo',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.principal_role','customer_admin',true)")
            )
            accepted = await connection.scalar(
                text("SELECT supportguard_api_accept_ticket(CAST(:request AS jsonb))"),
                {
                    "request": json.dumps(
                        {
                            "schema_version": "api-accept-ticket.v1",
                            "customer_id": "cust_demo",
                            "principal_id": "user_customer_demo",
                            "idempotency_key": f"idem-{suffix}",
                            "message": "PostgreSQL conversation contract",
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
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                },
            )
            assert isinstance(accepted, dict) and not accepted.get("error_code")
            own = await connection.scalar(
                text("SELECT supportguard_api_list_conversations(:customer,NULL,NULL,30)"),
                {"customer": "cust_demo"},
            )
            foreign = await connection.scalar(
                text("SELECT supportguard_api_list_conversations(:customer,NULL,NULL,30)"),
                {"customer": "cust_other"},
            )
            detail = await connection.scalar(
                text("SELECT supportguard_api_get_conversation_page(:customer,:ticket,NULL,50)"),
                {"customer": "cust_demo", "ticket": ticket_id},
            )
            citations = await connection.scalar(
                text("SELECT supportguard_api_get_run_citations(:customer,:run)"),
                {"customer": "cust_demo", "run": f"run_{suffix}"},
            )
        assert isinstance(own, dict) and own["items"]
        assert foreign == {"items": [], "next_cursor": None}
        assert isinstance(detail, dict)
        assert detail["id"] == ticket_id
        assert detail["title"] == "PostgreSQL conversation contract"
        assert detail["turns"]
        assert detail["turn_pagination"] == {
            "limit": 50,
            "returned": 1,
            "has_more": False,
            "next_before_ordinal": None,
        }
        assert citations == []
    finally:
        await engine.dispose()
