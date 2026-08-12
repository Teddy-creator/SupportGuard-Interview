from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import AgentRunStore
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import ProposalCausalDecisionV2
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    PolicyCapabilityAttempt,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    RuntimeJob,
    ToolTransportAttempt,
)
from supportguard.services.attempts import AttemptLedger
from supportguard.services.capability_ledger import (
    PolicyCapabilityLedger,
    capability_payload_hash,
)
from supportguard.services.runtime_jobs import RuntimeConflict, RuntimeJobRepository
from supportguard.services.tool_ledger import InvocationSpec, ToolLedger


def _refund_decision(
    binding: list[dict[str, str]], *, revision: int = 1
) -> ProposalCausalDecisionV2:
    return ProposalCausalDecisionV2(
        capability_name="propose_refund",
        action_type="refund",
        resource_id=f"bill_fixture_{revision}",
        resource_version=revision,
        model_arguments={"billing_record_id": f"bill_fixture_{revision}"},
        observation_binding_hash=canonical_json_hash(binding),
        policy_version="supportguard-policy-gate.v1",
    )


@pytest.mark.asyncio
async def test_attempt_is_persisted_and_budget_consumed_before_result(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    ledger = AttemptLedger(db_session)
    reserved = await ledger.reserve(lease, kind="llm")
    assert run.llm_calls == 1
    stored = await db_session.get(AgentCallAttempt, reserved.id)
    assert stored is not None and stored.status == "started"
    assert stored.runtime_provenance["worker_execution"] == {
        "lease_owner": "worker-a",
        "fencing_token": lease.fencing_token,
        "job_id": lease.job_id,
    }
    await ledger.finish(
        lease,
        reserved,
        status="succeeded",
        prompt_tokens=10,
        completion_tokens=2,
        provider_transport_attempts=2,
    )
    assert stored.status == "succeeded" and stored.prompt_tokens == 10
    assert stored.runtime_provenance["provider_transport_attempts"] == 2
    assert stored.runtime_provenance["provider_retry_count"] == 1
    assert run.llm_calls == 1
    record_predicate_operands(
        requirement_id="C4-P0-02a",
        predicate_id="c4_p0_02a",
        subject_kind="agent_attempt_budget_ledger",
        operands={
            "run_llm_calls": run.llm_calls,
            "reserved_attempt_id": reserved.id,
            "stored_attempt_status": stored.status,
            "prompt_tokens": stored.prompt_tokens,
            "completion_tokens": stored.completion_tokens,
        },
    )


@pytest.mark.asyncio
async def test_failed_provider_attempt_persists_only_bounded_structured_error_paths(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-schema", now=datetime.now(UTC))
    ledger = AttemptLedger(db_session)
    reserved = await ledger.reserve(lease, kind="llm")

    await ledger.finish(
        lease,
        reserved,
        status="failed",
        error_code="provider_terminal_schema_invalid",
        structured_error_paths=(
            "candidate.material_claims.0.text:missing",
            "candidate.action:extra_forbidden",
        ),
    )

    stored = await db_session.get(AgentCallAttempt, reserved.id)
    assert stored is not None
    assert stored.runtime_provenance["structured_error_paths"] == [
        "candidate.material_claims.0.text:missing",
        "candidate.action:extra_forbidden",
    ]
    assert "provider_payload" not in stored.runtime_provenance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paths",
    [
        (),
        tuple(f"field_{index}:missing" for index in range(13)),
        ("answer:\nsecret",),
        ("x" * 201,),
    ],
)
async def test_attempt_ledger_rejects_unsafe_structured_error_paths(
    db_session: AsyncSession,
    paths: tuple[str, ...],
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-schema", now=datetime.now(UTC))
    reserved = await AttemptLedger(db_session).reserve(lease, kind="llm")

    with pytest.raises(ValueError, match="structured error path"):
        await AttemptLedger(db_session).finish(
            lease,
            reserved,
            status="failed",
            structured_error_paths=paths,
        )


@pytest.mark.asyncio
async def test_successful_attempt_cannot_claim_structured_schema_errors(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-schema", now=datetime.now(UTC))
    reserved = await AttemptLedger(db_session).reserve(lease, kind="llm")

    with pytest.raises(ValueError, match="require a failed Provider attempt"):
        await AttemptLedger(db_session).finish(
            lease,
            reserved,
            status="succeeded",
            structured_error_paths=("candidate:missing",),
        )


@pytest.mark.asyncio
async def test_structure_repair_is_a_distinct_linked_attempt_in_the_shared_llm_budget(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-repair", now=datetime.now(UTC))
    ledger = AttemptLedger(db_session)
    original = await ledger.reserve(lease, kind="llm")
    await ledger.finish(
        lease,
        original,
        status="failed",
        error_code="provider_structured_output_invalid",
    )
    repair = await ledger.reserve(
        lease,
        kind="structure_repair",
        repair_of_attempt_id=original.id,
    )
    stored = await db_session.get(AgentCallAttempt, repair.id)
    assert stored is not None
    assert stored.call_kind == "structure_repair"
    assert stored.runtime_provenance["repair_of_attempt_id"] == original.id
    assert stored.runtime_provenance["repair_contract"] == "strict-structure-repair.v1"
    assert run.llm_calls == 2
    with pytest.raises(RuntimeConflict, match="structure_repair_already_used"):
        await ledger.reserve(
            lease,
            kind="structure_repair",
            repair_of_attempt_id=original.id,
        )
    with pytest.raises(ValueError, match="requires repair_of_attempt_id"):
        await ledger.reserve(lease, kind="structure_repair")


@pytest.mark.asyncio
async def test_unknown_attempts_remain_consumed_and_budget_is_bounded(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a", now=datetime.now(UTC))
    ledger = AttemptLedger(db_session)
    _, invocations = await ToolLedger(db_session).open_turn(
        lease,
        segment_id="segment_attempt_budget",
        tool_round=1,
        decision={"decision_type": "tool_calls"},
        context_manifest={},
        calls=[
            InvocationSpec(f"provider_{ordinal}", "query_account", {}, ordinal)
            for ordinal in range(6)
        ],
    )
    for invocation in invocations:
        reserved = await ledger.reserve(
            lease,
            kind="read_mcp",
            logical_invocation_id=invocation.id,
            transport_ordinal=1,
        )
        await ledger.finish(lease, reserved, status="unknown", error_code="worker_lost")
    with pytest.raises(RuntimeConflict, match="tool_budget_exhausted") as budget_error:
        await ledger.reserve(
            lease,
            kind="read_mcp",
            logical_invocation_id=invocations[0].id,
            transport_ordinal=2,
        )
    attempt_count = int(
        await db_session.scalar(select(func.count()).select_from(AgentCallAttempt)) or 0
    )
    transport_count = int(
        await db_session.scalar(select(func.count()).select_from(ToolTransportAttempt)) or 0
    )
    assert attempt_count == 6
    assert transport_count == 6
    record_predicate_operands(
        requirement_id="C6-P0-11",
        predicate_id="transport_retry_budget_consumed",
        subject_kind="tool_attempt_budget_ledger",
        operands={
            "logical_invocation_count": len(invocations),
            "agent_call_attempt_count": attempt_count,
            "transport_attempt_count": transport_count,
            "consumed_tool_attempts": run.tool_attempts,
            "budget_error": str(budget_error.value),
        },
    )


@pytest.mark.asyncio
async def test_read_transport_lifecycle_is_persisted_with_worker_lease_snapshot(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-lifecycle", now=datetime.now(UTC))
    _, invocations = await ToolLedger(db_session).open_turn(
        lease,
        segment_id="segment_lifecycle",
        tool_round=1,
        decision={"decision_type": "tool_calls"},
        context_manifest={},
        calls=[InvocationSpec("provider_lifecycle", "query_account", {}, 1)],
    )
    reserved = await AttemptLedger(db_session).reserve(
        lease,
        kind="read_mcp",
        logical_invocation_id=invocations[0].id,
        transport_ordinal=1,
    )
    lifecycle: dict[str, object] = {
        "schema_version": "mcp-transport-lifecycle.v1",
        "server": "read",
        "tool_name": "query_account",
        "arguments_hash": "a" * 64,
        "phase_sequence": ["initialize", "discovery", "schema_verified", "call", "terminal"],
        "outcome": "failed",
        "error_family": "stdio_closed",
        "duration_ms": 42,
        "session_generation": 2,
        "pid": 123,
        "process_group": 123,
        "process_birth_identity": {
            "platform": "linux",
            "boot_identity": "fixture-boot",
            "pid": 123,
            "start_value": "456",
        },
    }

    async def trusted_job_snapshot(
        _repository: RuntimeJobRepository,
        _lease: object,
    ) -> RuntimeJob:
        return job

    original_get = db_session.get

    async def get_without_runtime_job(entity: object, ident: object, **kwargs: object) -> object:
        if entity is RuntimeJob:
            raise AssertionError("finish must reuse the capability-returned job snapshot")
        return await original_get(entity, ident, **kwargs)

    monkeypatch.setattr(RuntimeJobRepository, "assert_fence", trusted_job_snapshot)
    monkeypatch.setattr(db_session, "get", get_without_runtime_job)
    await AttemptLedger(db_session).finish(
        lease,
        reserved,
        status="failed",
        error_code="tool_unavailable",
        transport_lifecycle=lifecycle,
    )
    stored = await db_session.get(AgentCallAttempt, reserved.id)
    assert stored is not None
    persisted = stored.runtime_provenance["mcp_transport_lifecycle"]
    assert persisted["error_family"] == "stdio_closed"
    assert persisted["worker_snapshot"]["lease_owner"] == "worker-lifecycle"
    assert persisted["worker_snapshot"]["fencing_token"] == lease.fencing_token
    assert "payload" not in persisted

    second = await AttemptLedger(db_session).reserve(
        lease,
        kind="read_mcp",
        logical_invocation_id=invocations[0].id,
        transport_ordinal=2,
    )
    with pytest.raises(RuntimeConflict, match="lifecycle_identity_mismatch"):
        await AttemptLedger(db_session).finish(
            lease,
            second,
            status="failed",
            transport_lifecycle={
                **lifecycle,
                "run_id": "run_other",
                "transport_attempt_id": second.transport_attempt_id,
            },
        )


@pytest.mark.asyncio
async def test_checkpoint_resume_cannot_decrease_consumed_budget(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.tool_rounds = 2
    run.tool_attempts = 5
    run.llm_calls = 4
    await AgentRunStore(db_session).transition(
        run,
        status="completed",
        checkpoint_stage="completed",
        tool_rounds=1,
        tool_attempts=2,
        llm_calls=3,
    )
    assert (run.tool_rounds, run.tool_attempts, run.llm_calls) == (2, 5, 4)
    record_predicate_operands(
        requirement_id="C4-P0-02d",
        predicate_id="c4_p0_02d",
        subject_kind="agent_budget_monotonicity",
        operands={
            "requested_projection": [1, 2, 3],
            "persisted_projection": [run.tool_rounds, run.tool_attempts, run.llm_calls],
            "minimum_projection": [2, 5, 4],
        },
    )


@pytest.mark.asyncio
async def test_policy_capability_has_its_own_ledger_and_no_agent_tool_budget(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    with pytest.raises(ValueError, match="unknown attempt kind"):
        await AttemptLedger(db_session).reserve(lease, kind="action_mcp")
    binding_1 = [{"observation_id": "observation_1"}]
    reserved = await PolicyCapabilityLedger(db_session).reserve(
        lease,
        segment_id="segment_policy",
        capability_name="propose_refund",
        causal_decision=_refund_decision(binding_1),
        observation_binding=binding_1,
    )
    await PolicyCapabilityLedger(db_session).finish(lease, reserved, status="succeeded")
    assert run.tool_attempts == 0
    stored = await db_session.get(PolicyCapabilityInvocation, reserved.id)
    assert stored is not None and stored.status == "succeeded"
    assert await db_session.scalar(select(func.count()).select_from(PolicyCapabilityAttempt)) == 1
    assert await db_session.scalar(select(func.count()).select_from(PolicyCapabilityResult)) == 1

    binding_2 = [{"observation_id": "observation_2"}]
    unknown = await PolicyCapabilityLedger(db_session).reserve(
        lease,
        segment_id="segment_policy",
        capability_name="propose_refund",
        causal_decision=_refund_decision(binding_2, revision=2),
        observation_binding=binding_2,
    )
    await PolicyCapabilityLedger(db_session).finish(
        lease,
        unknown,
        status="unknown",
        error_code="connection_lost_after_commit",
    )
    assert await db_session.scalar(select(func.count()).select_from(PolicyCapabilityResult)) == 1
    reconciled = await PolicyCapabilityLedger(db_session).reconcile_unknown(
        lease,
        invocation_id=unknown.id,
        status="succeeded",
        payload={"proposal_id": "proposal_reconciled"},
    )
    assert reconciled.reconciled_at is not None
    unknown_attempt = await db_session.get(PolicyCapabilityAttempt, unknown.attempt_id)
    assert unknown_attempt is not None and unknown_attempt.status == "succeeded"
    with pytest.raises(RuntimeConflict, match="policy_capability_already_reserved"):
        await PolicyCapabilityLedger(db_session).reserve(
            lease,
            segment_id="segment_policy",
            capability_name="propose_refund",
            causal_decision=_refund_decision(binding_2, revision=2),
            observation_binding=binding_2,
        )
    assert run.tool_attempts == 0
    record_predicate_operands(
        requirement_id="C4-P0-04a",
        predicate_id="c4_p0_04a",
        subject_kind="policy_capability_ledger_isolation",
        operands={
            "agent_tool_attempts": run.tool_attempts,
            "capability_invocation_status": stored.status,
            "capability_attempt_count": int(
                await db_session.scalar(select(func.count()).select_from(PolicyCapabilityAttempt))
                or 0
            ),
            "capability_result_count": int(
                await db_session.scalar(select(func.count()).select_from(PolicyCapabilityResult))
                or 0
            ),
            "unknown_attempt_status": unknown_attempt.status,
            "reconciled_at": reconciled.reconciled_at.isoformat(),
        },
    )


@pytest.mark.asyncio
async def test_capability_effect_receipt_is_authoritative_after_lost_response(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    run = await db_session.get(AgentRun, "run_demo")
    assert run is not None
    run.status = "queued"
    jobs = RuntimeJobRepository(db_session)
    job = await jobs.create(tenant_id="tenant_demo", run_id=run.id, kind="agent_start")
    lease = await jobs.claim(job_id=job.id, owner="worker-a")
    ledger = PolicyCapabilityLedger(db_session)
    receipt_binding = [{"observation_id": "observation_receipt"}]
    reserved = await ledger.reserve(
        lease,
        segment_id="segment_receipt",
        capability_name="propose_refund",
        causal_decision=_refund_decision(receipt_binding),
        observation_binding=receipt_binding,
    )
    invocation = await db_session.get(PolicyCapabilityInvocation, reserved.id)
    attempt = await db_session.get(PolicyCapabilityAttempt, reserved.attempt_id)
    assert invocation is not None and attempt is not None
    invocation.status = "executing"
    attempt.status = "executing"
    payload = {"proposal_id": "proposal_committed_once"}
    db_session.add(
        PolicyCapabilityResult(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            invocation_id=reserved.id,
            effect_identity=reserved.effect_identity,
            status="succeeded",
            payload_hash=capability_payload_hash(payload),
            payload=payload,
        )
    )
    await db_session.flush()

    settled = await ledger.finish(
        lease,
        reserved,
        status="unknown",
        error_code="connection_lost_after_commit",
        payload={"error_code": "timeout"},
    )
    assert settled is not None and settled.payload == payload
    assert invocation.status == "succeeded"
    assert attempt.status == "succeeded"
