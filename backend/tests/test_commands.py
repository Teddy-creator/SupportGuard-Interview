import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.db.models import (
    AgentRun,
    IdempotencyRequest,
    OutboxEvent,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.services.commands import CommandCoordinator
from supportguard.services.runtime_jobs import RuntimeConflict


@pytest.mark.asyncio
async def test_command_atomically_creates_domain_job_outbox_and_idempotent_response(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    coordinator = CommandCoordinator(db_session)
    first = await coordinator.accept_new_ticket(
        customer_id="cust_demo",
        principal_id="cust_demo",
        idempotency_key="command-key-001",
        message="Please diagnose request req_demo_429",
        trace_id="trace-command",
    )
    second = await coordinator.accept_new_ticket(
        customer_id="cust_demo",
        principal_id="cust_demo",
        idempotency_key="command-key-001",
        message="Please diagnose request req_demo_429",
        trace_id="trace-retry",
    )
    assert first.ticket_id == second.ticket_id
    assert first.run_id == second.run_id and first.job_id == second.job_id
    assert second.reused is True
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 3
    assert await db_session.scalar(select(func.count()).select_from(RuntimeJob)) == 1
    assert await db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    run = await db_session.get(AgentRun, first.run_id)
    assert run is not None and run.status == "queued"
    request = await db_session.scalar(
        select(IdempotencyRequest).where(IdempotencyRequest.idempotency_key == "command-key-001")
    )
    assert request is not None
    assert request.response_snapshot == first.response()


@pytest.mark.asyncio
async def test_command_same_key_different_body_conflicts(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    coordinator = CommandCoordinator(db_session)
    arguments = dict(
        customer_id="cust_demo",
        principal_id="cust_demo",
        idempotency_key="command-key-002",
        trace_id="trace-command",
    )
    await coordinator.accept_new_ticket(message="first body", **arguments)
    with pytest.raises(RuntimeConflict, match="idempotency_conflict"):
        await coordinator.accept_new_ticket(message="different body", **arguments)


@pytest.mark.asyncio
async def test_append_message_creates_a_new_run_but_reuses_http_retry(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.status = "resolved"
    coordinator = CommandCoordinator(db_session)
    arguments = dict(
        ticket_id=ticket.id,
        customer_id="cust_demo",
        principal_id="cust_demo",
        idempotency_key="append-key-001",
        message="Follow-up with a fresh request id",
        trace_id="trace-append",
    )
    first = await coordinator.accept_message(**arguments)
    retry = await coordinator.accept_message(**arguments)
    assert first.run_id == retry.run_id and retry.reused is True
    assert first.run_id != "run_demo"


@pytest.mark.asyncio
async def test_key_like_secret_is_irreversibly_replaced_before_message_insert(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    secret = "sk-thisIsASecretValue123456"  # noqa: S105 - synthetic redaction fixture
    accepted = await CommandCoordinator(db_session).accept_new_ticket(
        customer_id="cust_demo",
        principal_id="cust_demo",
        idempotency_key="secret-ingress-001",
        message=f"I leaked {secret}, revoke it",
        trace_id="trace-secret",
    )
    message = await db_session.scalar(
        select(TicketMessage).where(TicketMessage.ticket_id == accepted.ticket_id)
    )
    assert message is not None
    assert secret not in message.content
    assert "[REDACTED_API_KEY]" in message.content
    assert message.source_refs[0]["kind"] == "ingress_redaction_receipt"
    assert message.source_refs[0]["count"] == 1
    assert message.source_refs[0]["rule_ids"] == ["secret.api_key.v1"]
    assert message.source_refs[0]["secret_fingerprints"]
    assert message.source_refs[0]["secret_fingerprints"][0] not in message.content
