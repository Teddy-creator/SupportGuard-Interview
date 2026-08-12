from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.contracts.tools import ObservationEnvelope
from supportguard.db.models import AgentRun, ToolInvocation, ToolObservation
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger
from supportguard.tools.capabilities import CAPABILITIES, MODEL_VISIBLE_READ_TOOLS


def test_capability_registry_separates_model_policy_and_runtime_surfaces() -> None:
    assert len(MODEL_VISIBLE_READ_TOOLS) == 9
    assert all(CAPABILITIES[name].effect_class == "read" for name in MODEL_VISIBLE_READ_TOOLS)
    assert not CAPABILITIES["propose_refund"].model_visible
    assert CAPABILITIES["propose_refund"].allowed_callers == ("deterministic_policy",)
    assert CAPABILITIES["execute_refund"].allowed_callers == ("runtime_finalizer",)


@pytest.mark.asyncio
async def test_turn_group_requires_one_terminal_observation_per_invocation(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    job = await RuntimeJobRepository(db_session).create(
        tenant_id="tenant_demo", run_id=run.id, kind="agent_start"
    )
    lease = await RuntimeJobRepository(db_session).claim(job_id=job.id, owner="worker-a")
    ledger = ToolLedger(db_session)
    turn, invocations = await ledger.open_turn(
        lease,
        segment_id="segment_ledger",
        tool_round=1,
        decision={"decision_type": "tool_calls"},
        context_manifest={
            "manifest_hash": "context",
            "injected_tool_names": [
                "query_account",
                "search_knowledge",
            ],
            "injected_tool_schema_hash": "a" * 64,
        },
        calls=[
            InvocationSpec("provider_call_1", "query_account", {}, 0),
            InvocationSpec("provider_call_2", "search_knowledge", {"query": "429"}, 1),
        ],
    )
    with pytest.raises(RuntimeConflict, match="turn_group_incomplete"):
        await ledger.close_turn(lease, turn.id)
    for invocation in invocations:
        await ledger.mark_executing(lease, invocation.id)
        observation = ObservationEnvelope(
            tool_name=invocation.tool_name,
            tool_call_id=invocation.provider_tool_call_id,
            ticket_id=run.ticket_id,
            run_id=run.id,
            attempt_index=1,
            status="ok",
            retryable=False,
            observed_at=datetime.now(UTC),
            duration_ms=1,
            data={"ordinal": invocation.ordinal},
        )
        first = await ledger.terminalize(
            lease, invocation.id, outcome="succeeded", observation=observation
        )
        repeated = await ledger.terminalize(
            lease, invocation.id, outcome="succeeded", observation=observation
        )
        assert repeated.id == first.id
    closed = await ledger.close_turn(lease, turn.id)
    assert closed.status == "closed"
    invocation_count = int(
        await db_session.scalar(
            select(func.count(ToolInvocation.id)).where(ToolInvocation.turn_group_id == turn.id)
        )
        or 0
    )
    observation_count = int(
        await db_session.scalar(
            select(func.count(ToolObservation.id))
            .join(ToolInvocation, ToolInvocation.id == ToolObservation.invocation_id)
            .where(ToolInvocation.turn_group_id == turn.id)
        )
        or 0
    )
    assert invocation_count == 2 and observation_count == 2
    assert closed.tool_schema_hash == "a" * 64
    operands = {
        "turn_status": closed.status,
        "invocation_count": invocation_count,
        "observation_count": observation_count,
        "terminal_lifecycle_count": sum(item.lifecycle == "terminal" for item in invocations),
        "duplicate_terminal_reuse_count": len(invocations),
        "tool_round": turn.tool_round,
    }
    for predicate_id in (
        "ordinal_terminal_unique",
        "round_exhaustion_zero_transport",
        "no_progress_full_identity",
        "budget_projection_equal",
    ):
        record_predicate_operands(
            requirement_id="C5-P0-06",
            predicate_id=predicate_id,
            subject_kind="tool_turn_terminal_ledger",
            operands=operands,
        )
    record_predicate_operands(
        requirement_id="C4-P0-03a",
        predicate_id="c4_p0_03a",
        subject_kind="tool_turn_terminal_ledger",
        operands=operands,
    )


@pytest.mark.asyncio
async def test_duplicate_provider_ids_are_all_recorded_by_ordinal(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    job = await RuntimeJobRepository(db_session).create(
        tenant_id="tenant_demo", run_id=run.id, kind="agent_start"
    )
    lease = await RuntimeJobRepository(db_session).claim(job_id=job.id, owner="worker-a")
    turn, invocations = await ToolLedger(db_session).open_turn(
        lease,
        segment_id="segment_duplicate",
        tool_round=1,
        decision={"decision_type": "tool_calls"},
        context_manifest={},
        calls=[
            InvocationSpec("duplicate", "query_account", {}, 0),
            InvocationSpec("duplicate", "query_subscription", {}, 1),
        ],
    )
    assert turn.status == "open"
    assert [item.ordinal for item in invocations] == [0, 1]
    assert {item.provider_tool_call_id for item in invocations} == {"duplicate"}
