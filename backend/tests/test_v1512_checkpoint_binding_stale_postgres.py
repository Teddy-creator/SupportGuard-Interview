from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.agent.persistence import verify_ticket_event_chain
from supportguard.api.auth import Principal, approver_principal
from supportguard.contracts.context import RequestContext
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApprovalRequest,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    IdempotencyRequest,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.seed import seed_demo_data
from supportguard.db.session import create_scoped_session_factory
from supportguard.main import create_app
from test_postgres_finalizer_faults import _seed_pending_approval

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _database_url() -> str | None:
    return os.getenv("TEST_FINALIZER_DATABASE_URL")


def _role_url(database_url: str, role: str) -> str:
    return (
        make_url(database_url)
        .set(username=role, password=role)
        .render_as_string(hide_password=False)
    )


def _scope(
    label: str,
    *,
    tenant_id: str = "tenant_demo",
    actor_id: str = "user_approver_demo",
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        authenticated_actor_id=actor_id,
        authenticated_actor_role="support_approver",
        subject_customer_id=None,
        request_id=f"request-{label}",
        trace_id=f"trace-{label}",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


async def _insert_waiting_followup(
    factory: async_sessionmaker,
    *,
    prefix: str,
    ticket_id: str,
) -> tuple[str, str]:
    message_id = f"message_{prefix}_followup"
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                message_kind="customer",
                role="user",
                content="Please continue with my ordinary support question.",
                source_refs=[],
            )
        )
        await session.flush()
        turn = await session.scalar(
            select(ConversationTurn).where(
                ConversationTurn.tenant_id == "tenant_demo",
                ConversationTurn.customer_message_id == message_id,
            )
        )
        assert turn is not None
        assert turn.activity_state == "accepted"
        assert turn.run_id is None
        return turn.id, message_id


async def _post_approval(
    *,
    app,
    approval_id: str,
    idempotency_key: str,
    reason: str = "The persisted evidence was independently reviewed.",
) -> tuple[int, dict[str, object]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://supportguard.test") as client:
        response = await client.post(
            f"/api/approvals/{approval_id}/approve",
            headers={"Idempotency-Key": idempotency_key},
            json={"reason": reason},
        )
    return response.status_code, response.json()


async def test_checkpoint_binding_conflict_api_converges_atomically_and_replays_409() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_binding_stale_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(database_url, "supportguard_api"))
    api_unscoped_factory = async_sessionmaker(api, expire_on_commit=False)
    api_factory = create_scoped_session_factory(api)
    try:
        async with factory() as session, session.begin():
            await seed_demo_data(session)
        approval_id, ticket_id = await _seed_pending_approval(factory, prefix)
        followup_turn_id, followup_message_id = await _insert_waiting_followup(
            factory,
            prefix=prefix,
            ticket_id=ticket_id,
        )
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            approval = await session.get(
                ApprovalRequest,
                approval_id,
                with_for_update=True,
            )
            assert approval is not None
            original_run_id = str(approval.run_id)
            original_turn_id = approval.origin_turn_id
            original_approval_head = approval.expected_ticket_head_event_id
            original_approval_hash = approval.expected_ticket_event_hash
            run = await session.get(AgentRun, original_run_id, with_for_update=True)
            assert run is not None
            run.canonical_checkpoint_hash = "f" * 64

        app = create_app(testing=False)
        app.state.testing = True
        # The HTTP rollout probe is intentionally API-capability-owned; domain
        # routes below use the separately scoped API factory.
        app.state.factory = api_unscoped_factory
        app.state.scoped_factory = api_factory

        async def approver_identity() -> Principal:
            return Principal(
                role="approver",
                subject_id="user_approver_demo",
                tenant_id="tenant_demo",
                membership_role="support_approver",
            )

        app.dependency_overrides[approver_principal] = approver_identity
        first_status, first = await _post_approval(
            app=app,
            approval_id=approval_id,
            idempotency_key=f"approve-{prefix}",
        )
        replay_status, replay = await _post_approval(
            app=app,
            approval_id=approval_id,
            idempotency_key=f"approve-{prefix}",
        )
        assert first_status == replay_status == 409
        assert first["public_code"] == replay["public_code"] == "state_conflict"
        assert (
            set(first)
            == set(replay)
            == {
                "public_code",
                "message",
                "retryable",
                "request_id",
            }
        )
        assert "checkpoint_binding_conflict" not in str(first)

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            proposal = await session.get(ProposalRecord, approval.proposal_id)
            original_run = await session.get(AgentRun, original_run_id)
            original_turn = await session.get(ConversationTurn, original_turn_id or "")
            followup_turn = await session.get(ConversationTurn, followup_turn_id)
            followup_message = await session.get(TicketMessage, followup_message_id)
            ticket = await session.get(SupportTicket, ticket_id)
            transition_events = (
                await session.scalars(
                    select(AgentEvent).where(
                        AgentEvent.tenant_id == "tenant_demo",
                        AgentEvent.ticket_id == ticket_id,
                        AgentEvent.run_id == original_run_id,
                        AgentEvent.event_type == "runtime_action_reconciliation",
                        AgentEvent.payload["approval_id"].as_string() == approval_id,
                    )
                )
            ).all()
            action_updates = (
                await session.scalars(
                    select(TicketMessage).where(
                        TicketMessage.tenant_id == "tenant_demo",
                        TicketMessage.ticket_id == ticket_id,
                        TicketMessage.approval_id == approval_id,
                        TicketMessage.publication_key
                        == f"approval:{approval_id}:stale:checkpoint-binding",
                    )
                )
            ).all()
            decisions = int(
                await session.scalar(
                    select(func.count(HumanDecision.id)).where(
                        HumanDecision.approval_id == approval_id
                    )
                )
                or 0
            )
            effects = int(
                await session.scalar(
                    select(func.count(BusinessAction.id)).where(
                        BusinessAction.approval_id == approval_id
                    )
                )
                or 0
            )
            approval_jobs = (
                await session.scalars(
                    select(RuntimeJob).where(RuntimeJob.approval_id == approval_id)
                )
            ).all()
            followup_jobs = (
                await session.scalars(
                    select(RuntimeJob).where(
                        RuntimeJob.tenant_id == "tenant_demo",
                        RuntimeJob.run_id == followup_turn.run_id,
                        RuntimeJob.kind == "agent_start",
                    )
                )
            ).all()
            stale_idempotency = int(
                await session.scalar(
                    select(func.count(IdempotencyRequest.id)).where(
                        IdempotencyRequest.tenant_id == "tenant_demo",
                        IdempotencyRequest.principal_id == "user_approver_demo",
                        IdempotencyRequest.idempotency_key == f"approve-{prefix}",
                    )
                )
                or 0
            )

            assert approval.status == "stale"
            assert approval.decision_reason is None
            assert proposal is not None and proposal.status == "stale"
            assert original_run is not None
            assert original_run.status == "completed"
            assert original_run.checkpoint_stage == "completed"
            assert original_run.agent_finish_reason == "binding_stale"
            assert original_turn is not None
            assert original_turn.activity_state == "completed"
            assert original_turn.result_state == "stale"
            assert followup_turn is not None
            assert followup_turn.activity_state == "queued"
            assert followup_turn.run_id is not None
            assert followup_message is not None
            assert followup_message.turn_id == followup_turn.id
            assert ticket is not None and ticket.status == "queued"
            assert len(transition_events) == 1
            assert transition_events[0].payload == {
                "approval_id": approval_id,
                "proposal_id": approval.proposal_id,
                "reason": "checkpoint_binding_conflict",
            }
            assert approval.expected_ticket_head_event_id == original_approval_head
            assert approval.expected_ticket_event_hash == original_approval_hash
            assert transition_events[0].causation_id == original_approval_head
            assert len(action_updates) == 1
            assert action_updates[0].conversation_sequence is not None
            assert decisions == effects == stale_idempotency == 0
            assert approval_jobs == []
            assert len(followup_jobs) == 1
            await verify_ticket_event_chain(session, ticket_id)
    finally:
        await api.dispose()
        await admin.dispose()


async def test_blank_approve_reason_matches_public_http_and_postgres_contract() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_blank_approve_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(database_url, "supportguard_api"))
    api_unscoped_factory = async_sessionmaker(api, expire_on_commit=False)
    api_factory = create_scoped_session_factory(api)
    try:
        async with factory() as session, session.begin():
            await seed_demo_data(session)
        approval_id, _ = await _seed_pending_approval(factory, prefix)

        app = create_app(testing=False)
        app.state.testing = True
        app.state.factory = api_unscoped_factory
        app.state.scoped_factory = api_factory

        async def approver_identity() -> Principal:
            return Principal(
                role="approver",
                subject_id="user_approver_demo",
                tenant_id="tenant_demo",
                membership_role="support_approver",
            )

        app.dependency_overrides[approver_principal] = approver_identity
        idempotency_key = f"approve-{prefix}"
        first_status, first = await _post_approval(
            app=app,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            reason="",
        )
        replay_status, replay = await _post_approval(
            app=app,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            reason="",
        )

        assert first_status == replay_status == 202
        assert first["decision"] == replay["decision"] == "approve"
        assert first["reused"] is False
        assert replay == {**first, "reused": True}
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            decision_count = await session.scalar(
                select(func.count(HumanDecision.id)).where(
                    HumanDecision.approval_id == approval_id
                )
            )
            job_count = await session.scalar(
                select(func.count(RuntimeJob.id)).where(
                    RuntimeJob.approval_id == approval_id
                )
            )
        assert approval is not None and approval.status == "approved"
        assert int(decision_count or 0) == 1
        assert int(job_count or 0) == 1
    finally:
        await api.dispose()
        await admin.dispose()


async def test_checkpoint_binding_stale_capability_is_narrow_scoped_and_non_abusable() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("TEST_FINALIZER_DATABASE_URL is required")
    prefix = f"v1512_binding_acl_{uuid4().hex[:10]}"
    admin = create_async_engine(database_url)
    factory = async_sessionmaker(admin, expire_on_commit=False)
    api = create_async_engine(_role_url(database_url, "supportguard_api"))
    api_factory = create_scoped_session_factory(api)
    try:
        async with factory() as session, session.begin():
            await seed_demo_data(session)
        approval_id, _ = await _seed_pending_approval(factory, prefix)

        async with api_factory.request(_scope(prefix)) as session:
            valid = await session.scalar(
                text("SELECT supportguard_api_converge_checkpoint_binding_stale(:approval_id)"),
                {"approval_id": approval_id},
            )
            await session.commit()
        assert valid == {"error_code": "checkpoint_binding_valid"}

        async with api_factory.request(
            _scope(f"{prefix}-other", tenant_id="tenant_other")
        ) as session:
            hidden = await session.scalar(
                text("SELECT supportguard_api_converge_checkpoint_binding_stale(:approval_id)"),
                {"approval_id": approval_id},
            )
            await session.commit()
        assert hidden == {"error_code": "approval_not_found"}

        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status == "pending"

        signature = "public.supportguard_api_converge_checkpoint_binding_stale(text)"
        async with admin.connect() as connection:
            execute_acl = {
                role: bool(
                    await connection.scalar(
                        text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                        {"role": role, "signature": signature},
                    )
                )
                for role in (
                    "supportguard_api",
                    "supportguard_worker",
                    "supportguard_read_mcp",
                    "supportguard_action_mcp",
                )
            }
            table_acl_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name,privilege,
                               has_table_privilege(
                                 'supportguard_api',
                                 'public.'||table_name,
                                 privilege
                               )
                        FROM unnest(ARRAY[
                          'approval_requests','proposal_records','agent_runs',
                          'conversation_turns','support_tickets','agent_events',
                          'ticket_messages','human_decisions','business_actions',
                          'runtime_jobs'
                        ]) table_name
                        CROSS JOIN unnest(ARRAY[
                          'SELECT','INSERT','UPDATE','DELETE'
                        ]) privilege
                        ORDER BY table_name,privilege
                        """
                    )
                )
            ).all()
        assert execute_acl == {
            "supportguard_api": True,
            "supportguard_worker": False,
            "supportguard_read_mcp": False,
            "supportguard_action_mcp": False,
        }
        assert table_acl_rows
        assert all(not bool(row[2]) for row in table_acl_rows)
    finally:
        await api.dispose()
        await admin.dispose()
