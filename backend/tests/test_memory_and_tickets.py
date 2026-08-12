from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.agent.graph import AgentState
from supportguard.agent.persistence import (
    CANONICALIZATION_VERSION,
    EVENT_HASH_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    GENESIS_EVENT_HASH,
    AgentRunStore,
    verify_ticket_event_chain,
)
from supportguard.db.models import (
    AgentEvent,
    AuditEvent,
    KnowledgeDocument,
    KnowledgeIngestRun,
    SupportTicket,
    TicketMessage,
    TicketSummary,
)
from supportguard.db.session import ScopedSessionFactory
from supportguard.memory.service import MemoryHistoryLoader, MemoryService, MemoryWriter
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.tickets import TicketService


def completed_state(ticket_id: str) -> AgentState:
    return cast(
        AgentState,
        {
            "ticket_id": ticket_id,
            "customer_id": "cust_demo",
            "trace_id": f"trace_{ticket_id}",
            "classification": {"issue_type": "rate_limit", "risk": "low"},
            "evidence": [
                {
                    "chunk_id": "limits:c001",
                    "excerpt": "Pro concurrency is 40.",
                }
            ],
            "tool_observations": [
                {
                    "plan": "pro",
                    "source_refs": [
                        {
                            "source_type": "business_record",
                            "source_id": "subscription:sub_demo",
                            "observed_at": "2026-07-12T00:00:00Z",
                        }
                    ],
                }
            ],
            "candidate": {"action": "answer"},
            "final": {
                "answer": "Use the cited limits.",
                "terminal_state": "resolved",
                "knowledge_chunk_ids": ["limits:c001"],
                "business_source_ids": ["subscription:sub_demo"],
            },
        },
    )


async def canonical_state(
    session: AsyncSession, ticket_id: str, state: AgentState | None = None
) -> AgentState:
    ticket = await session.get(SupportTicket, ticket_id)
    assert ticket is not None
    message = TicketMessage(
        id=f"message_memory_{ticket_id}",
        tenant_id=ticket.tenant_id,
        ticket_id=ticket_id,
        role="user",
        content="Build a canonical memory summary.",
    )
    session.add(message)
    await session.flush()
    run = await AgentRunStore(session).create(
        ticket_id=ticket_id,
        customer_id=ticket.customer_id,
        message_id=message.id,
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    run.status = "completed"
    run.canonical_checkpoint_hash = "c" * 64
    await AgentRunStore(session).append_event(
        run, event_type="final_outcome", payload={"terminal_state": "resolved"}
    )
    output = deepcopy(state or completed_state(ticket_id))
    output["run_id"] = run.id
    return output


@pytest.mark.asyncio
async def test_terminal_summary_contains_only_sourced_facts_and_scoped_history(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.issue_type = "rate_limit"
    summary = await MemoryService(db_session).persist_summary(
        await canonical_state(db_session, ticket.id)
    )
    assert len(summary.confirmed_facts) == 2
    assert all(item["source_id"] for item in summary.confirmed_facts)

    history = await MemoryService(db_session).load_relevant_history(
        customer_id="cust_demo", issue_type="rate_limit"
    )
    other_customer = await MemoryService(db_session).load_relevant_history(
        customer_id="cust_other", issue_type="rate_limit"
    )
    assert [item.ticket_id for item in history] == ["ticket_demo"]
    assert other_customer == []


@pytest.mark.asyncio
async def test_summary_replay_at_same_watermark_is_a_zero_mutation(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    state = await canonical_state(db_session, "ticket_demo")
    service = MemoryService(db_session)
    first = await service.persist_summary(state)
    await db_session.flush()
    facts_before = deepcopy(first.confirmed_facts)
    audits_before = await db_session.scalar(select(func.count()).select_from(AuditEvent))
    summaries_before = await db_session.scalar(select(func.count()).select_from(TicketSummary))

    replay = await service.persist_summary(state)
    await db_session.flush()

    assert replay.id == first.id
    assert replay.confirmed_facts == facts_before
    assert await db_session.scalar(select(func.count()).select_from(AuditEvent)) == audits_before
    assert (
        await db_session.scalar(select(func.count()).select_from(TicketSummary)) == summaries_before
    )


@pytest.mark.asyncio
async def test_knowledge_memory_is_superseded_when_its_index_is_no_longer_active(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ingest = KnowledgeIngestRun(
        id="ingest-memory-v1",
        status="succeeded",
        index_version="index-memory-v1",
        document_count=1,
        chunk_count=1,
        is_active=True,
    )
    effective_from = datetime.now(UTC)
    document = KnowledgeDocument(
        ingest_run_id=ingest.id,
        document_key="limits",
        document_family_key="limits",
        title="Limits",
        document_type="product_policy",
        version="1.0",
        status="active",
        effective_at=effective_from,
        effective_from=effective_from,
        effective_until=None,
        applicability_scope_hash="c" * 64,
        temporal_manifest_hash="d" * 64,
        authority_level=3,
        content_hash="a" * 64,
        canonical_blob=b"Pro concurrency is 40.",
        source_path="limits.md",
        index_version=ingest.index_version,
    )
    db_session.add_all([ingest, document])
    state = completed_state("ticket_demo")
    state["evidence"][0].update(
        {
            "document_id": "limits",
            "version": "1.0",
            "index_version": ingest.index_version,
            "source_locator": {"locator_hash": "b" * 64},
        }
    )
    service = MemoryService(db_session)
    summary = await service.persist_summary(await canonical_state(db_session, "ticket_demo", state))
    await db_session.flush()

    current = await service.load_relevant_history(customer_id="cust_demo", issue_type="rate_limit")
    assert current[0].confirmed_facts[0]["status"] == "active"

    ingest.is_active = False
    await db_session.flush()
    stale = await service.load_relevant_history(customer_id="cust_demo", issue_type="rate_limit")
    assert stale[0].id == summary.id
    assert stale[0].confirmed_facts[0]["status"] == "superseded"


@pytest.mark.asyncio
async def test_memory_adapters_fail_closed_without_worker_context() -> None:
    factory = cast(ScopedSessionFactory, object())
    with pytest.raises(RuntimeError, match="required trusted context is not bound"):
        await MemoryWriter(factory).persist(completed_state("ticket_demo"))
    with pytest.raises(RuntimeError, match="required trusted context is not bound"):
        await MemoryHistoryLoader(factory).load(customer_id="cust_demo", issue_type="rate_limit")


@pytest.mark.asyncio
async def test_history_is_deterministically_limited_to_three(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    service = MemoryService(db_session)
    for index in range(5):
        ticket = SupportTicket(
            id=f"ticket_history_{index}",
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            status="open",
            issue_type="rate_limit",
        )
        db_session.add(ticket)
        await db_session.flush()
        await service.persist_summary(await canonical_state(db_session, ticket.id))
    history = await service.load_relevant_history(customer_id="cust_demo", issue_type="rate_limit")
    assert len(history) == 3


@pytest.mark.asyncio
async def test_awaiting_approval_rejects_new_message_without_mutation(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert ticket is not None
    ticket.status = "awaiting_approval"
    await db_session.flush()
    before = await db_session.scalar(select(func.count()).select_from(TicketMessage))
    with pytest.raises(DomainError) as error:
        await TicketService(db_session).append_message(
            ticket_id=ticket.id,
            customer_id="cust_demo",
            message="Change the refund target while approval is pending.",
        )
    after = await db_session.scalar(select(func.count()).select_from(TicketMessage))
    assert error.value.code is ErrorCode.TICKET_STATE_CONFLICT
    assert before == after
    assert ticket.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_open_ticket_accepts_followup_and_moves_to_running(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    message = await TicketService(db_session).append_message(
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        message="The same 429 happened again after one minute.",
    )
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert message.ticket_id == "ticket_demo"
    assert ticket is not None and ticket.status == "running"


@pytest.mark.asyncio
async def test_one_message_creates_one_stable_run_with_monotonic_events(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket = SupportTicket(
        id="ticket_run",
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="running",
    )
    message = TicketMessage(
        id="message_run",
        tenant_id="tenant_demo",
        ticket_id=ticket.id,
        role="user",
        content="Check current usage.",
    )
    db_session.add_all([ticket, message])
    await db_session.flush()
    store = AgentRunStore(db_session)
    run = await store.create(
        ticket_id=ticket.id,
        customer_id="cust_demo",
        message_id=message.id,
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    same = await store.create(
        ticket_id=ticket.id,
        customer_id="cust_demo",
        message_id=message.id,
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    await store.append_event(
        run,
        event_type="agent_decision",
        visibility="customer",
        payload={"decision_type": "tool_calls"},
    )
    events = (
        await db_session.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)
        )
    ).all()

    assert same.id == run.id
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].parent_event_hash == GENESIS_EVENT_HASH
    assert events[0].previous_event_id is None
    assert events[1].previous_event_id == events[0].id
    assert events[1].parent_event_hash == events[0].event_hash
    assert events[1].event_schema_version == EVENT_SCHEMA_VERSION
    assert events[1].canonicalization_version == CANONICALIZATION_VERSION
    assert events[1].event_hash_schema_version == EVENT_HASH_SCHEMA_VERSION
    assert await verify_ticket_event_chain(db_session, ticket.id) == events[-1].event_hash
    events[0].payload = {"tampered": True}
    await db_session.flush()
    with pytest.raises(RuntimeError, match="ticket_event_chain_invalid"):
        await verify_ticket_event_chain(db_session, ticket.id)


@pytest.mark.asyncio
async def test_event_redaction_preserves_typed_ids_without_exempting_free_text(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    ticket = SupportTicket(
        id="ticket_structural_redaction",
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="running",
    )
    message = TicketMessage(
        id="message_structural_redaction",
        tenant_id="tenant_demo",
        ticket_id=ticket.id,
        role="user",
        content="Create a structural-redaction fixture.",
    )
    db_session.add_all([ticket, message])
    await db_session.flush()
    store = AgentRunStore(db_session)
    run = await store.create(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        message_id=message.id,
        model="fake",
        provider_mode="fake",
        tool_call_mode="native_fixture",
        context_version="context.v1",
    )
    approval_id = "approval_c44d1f2c0068427484884f6f4cb0f509"
    event = await store.append_event(
        run,
        event_type="runtime_action_reconciliation",
        payload={
            "approval_id": approval_id,
            "related_approval_ids": [approval_id],
            "action_hash": "1" * 64,
            "note": "Contact owner@example.test about card 4242424242424242.",
        },
    )
    assert event.payload["approval_id"] == approval_id
    assert event.payload["related_approval_ids"] == [approval_id]
    assert event.payload["action_hash"] == "1" * 64
    assert event.payload["note"] == (
        "Contact [REDACTED_EMAIL] about card [REDACTED_PAYMENT_NUMBER]."
    )
    assert await verify_ticket_event_chain(db_session, ticket.id) == event.event_hash


@pytest.mark.asyncio
async def test_new_observation_supersedes_prior_fact_and_preserves_freshness(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    service = MemoryService(db_session)
    first_state = completed_state("ticket_demo")
    observed = datetime.now(UTC).isoformat()
    first_state["tool_observations"][0].update(
        {
            "tool_name": "query_account",
            "observed_at": observed,
            "resource_version": "3",
            "data": {"plan": "pro", "version": 3},
        }
    )
    first = await service.persist_summary(
        await canonical_state(db_session, "ticket_demo", first_state)
    )
    ticket = SupportTicket(
        id="ticket_memory_new",
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="open",
        issue_type="rate_limit",
    )
    db_session.add(ticket)
    await db_session.flush()
    second_state = deepcopy(completed_state(ticket.id))
    second_state["tool_observations"][0].update(
        {
            "tool_name": "query_account",
            "observed_at": observed,
            "resource_version": "4",
            "data": {"plan": "pro", "version": 4},
        }
    )
    second = await service.persist_summary(
        await canonical_state(db_session, ticket.id, second_state)
    )

    previous = next(item for item in first.confirmed_facts if item["fact_type"] == "query_account")
    current = next(item for item in second.confirmed_facts if item["fact_type"] == "query_account")
    assert previous["status"] == "superseded"
    assert current["status"] == "active"
    assert current["freshness_policy"] == "account_subscription_15m"
    assert current["valid_until"] is not None
    assert current["resource_version"] == "4"
    assert current["supersedes_fact_id"] == previous["fact_id"]
