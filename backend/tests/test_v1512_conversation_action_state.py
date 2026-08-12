from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.api.contracts import ConversationDetailResponse
from supportguard.api.projections import _apply_conversation_action_projection
from supportguard.db.models import (
    AgentRun,
    ApprovalRequest,
    BusinessAction,
    ConversationTurn,
    HumanDecision,
    ProposalRecord,
    ProposalWithdrawal,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
)
from supportguard.db.role_contract import expected_worker_table_grants
from supportguard.services.conversation_action_state import (
    ConversationActionOrmSources,
    ConversationActionSources,
    ConversationActionStateProjectionError,
    ConversationActionStateProjector,
    ConversationActionStateV1,
    conversation_action_sources_from_mapping,
    conversation_action_sources_from_orm,
    project_conversation_action_state,
)

NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
EVENT_HASH = "a" * 64
DECISION_HASH = "b" * 64
ACTION_EVENT_HASH = "c" * 64


def _approval(
    status: str,
    *,
    approval_id: str = "approval_state",
    updated_at: datetime = NOW,
) -> ApprovalRequest:
    return ApprovalRequest(
        id=approval_id,
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        proposal_id=f"proposal_{approval_id}",
        run_id="run_demo",
        checkpoint_id="checkpoint_demo",
        action_type="refund",
        resource_type="billing_record_id",
        resource_id="bill_duplicate",
        origin_turn_id="turn_demo",
        # Deliberately untrusted and contradictory: projection identity must
        # come from the explicit canonical columns above.
        action_payload={"billing_record_id": "bill_payload_must_not_win"},
        review_context={"approver_note": "must not leave the aggregate"},
        action_hash="d" * 64,
        business_version=2,
        status=status,
        idempotency_key=f"idem_{approval_id}",
        status_version=3,
        expected_ticket_head_event_id="event_interrupt",
        expected_ticket_event_hash=EVENT_HASH,
        created_at=NOW - timedelta(minutes=5),
        updated_at=updated_at,
    )


def _proposal(
    approval: ApprovalRequest,
    *,
    updated_at: datetime = NOW,
) -> ProposalRecord:
    return ProposalRecord(
        id=str(approval.proposal_id),
        tenant_id=approval.tenant_id,
        run_id=str(approval.run_id),
        proposal_identity=f"identity_{approval.id}",
        action_type=approval.action_type,
        resource_id=approval.resource_id,
        resource_version=approval.business_version,
        action_payload={"billing_record_id": approval.resource_id},
        observation_binding=[],
        action_hash=approval.action_hash,
        status="bound" if approval.status in {"pending", "approved"} else "stale",
        status_version=1,
        created_at=NOW - timedelta(minutes=4),
        updated_at=updated_at,
    )


def _decision(
    approval: ApprovalRequest,
    decision: str,
    *,
    updated_at: datetime = NOW + timedelta(minutes=1),
) -> HumanDecision:
    return HumanDecision(
        id=f"decision_{approval.id}",
        tenant_id=approval.tenant_id,
        approval_id=approval.id,
        actor_id="approver_demo",
        decision=decision,
        reason="raw private reason must not be projected",
        action_hash=approval.action_hash,
        decision_hash=DECISION_HASH,
        canonical_event_id=f"event_decision_{approval.id}",
        canonical_event_hash=DECISION_HASH,
        audit_metadata={"approver_note": "private"},
        created_at=updated_at,
        updated_at=updated_at,
    )


def _job(
    approval: ApprovalRequest,
    *,
    status: str,
    outcome: str | None = None,
    delivery_hold_reason: str | None = None,
    updated_at: datetime = NOW + timedelta(minutes=2),
) -> RuntimeJob:
    return RuntimeJob(
        id=f"job_{approval.id}",
        tenant_id=approval.tenant_id,
        ticket_id=approval.ticket_id,
        run_id=str(approval.run_id),
        dispatch_sequence=1,
        approval_id=approval.id,
        kind="approval_resume",
        status=status,
        status_version=1,
        attempt=0,
        fencing_token=0,
        outcome=outcome,
        delivery_hold_reason=delivery_hold_reason,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _business_action(
    approval: ApprovalRequest,
    *,
    status: str = "succeeded",
    updated_at: datetime = NOW + timedelta(minutes=3),
) -> BusinessAction:
    return BusinessAction(
        id=f"action_{approval.id}",
        tenant_id=approval.tenant_id,
        ticket_id=approval.ticket_id,
        customer_id=approval.customer_id,
        action_type=approval.action_type,
        resource_id=approval.resource_id,
        resource_version=approval.business_version,
        action_hash=approval.action_hash,
        approval_id=approval.id,
        decision_hash=DECISION_HASH,
        effect_identity="e" * 64,
        canonical_event_id=f"event_action_{approval.id}",
        canonical_event_hash=ACTION_EVENT_HASH,
        status=status,
        idempotency_key=f"effect_{approval.id}",
        result={"raw_provider_payload": "must not be projected"},
        created_at=updated_at,
        updated_at=updated_at,
    )


def _withdrawal(
    approval: ApprovalRequest,
    *,
    updated_at: datetime = NOW + timedelta(minutes=2),
) -> ProposalWithdrawal:
    return ProposalWithdrawal(
        id=f"withdrawal_{approval.id}",
        tenant_id=approval.tenant_id,
        ticket_id=approval.ticket_id,
        customer_id=approval.customer_id,
        approval_id=approval.id,
        proposal_id=str(approval.proposal_id),
        actor_id="cust_demo",
        reason="raw withdrawal reason must not be projected",
        idempotency_key=f"withdraw_{approval.id}",
        created_at=updated_at,
        updated_at=updated_at,
    )


def _sources(case: str) -> ConversationActionSources:
    status = {
        "pending": "pending",
        "approved": "approved",
        "executing": "approved",
        "verification_pending": "approved",
        "executed": "executed",
        "rejected": "rejected",
        "stale": "stale",
        "withdrawn": "withdrawn",
        "failed": "failed",
        "manual_takeover_legacy": "manual_takeover",
    }[case]
    approval = _approval(status, approval_id=f"approval_{case}")
    decision = None
    job = None
    action = None
    withdrawal = None
    if case in {"approved", "executing", "verification_pending", "executed", "stale", "failed"}:
        decision = _decision(approval, "approve")
    elif case == "rejected":
        decision = _decision(approval, "reject")
    elif case == "manual_takeover_legacy":
        decision = _decision(approval, "manual_takeover")
    if case == "approved":
        job = _job(approval, status="queued")
    elif case == "executing":
        job = _job(approval, status="leased")
    elif case == "verification_pending":
        job = _job(approval, status="succeeded", outcome="verification_pending")
    elif case == "executed":
        action = _business_action(approval)
        job = _job(approval, status="succeeded", outcome="completed")
    elif case == "withdrawn":
        withdrawal = _withdrawal(approval)
    elif case == "failed":
        job = _job(approval, status="dead", outcome="failed")
    sources = conversation_action_sources_from_orm(
        ConversationActionOrmSources(
            approval=approval,
            proposal=_proposal(approval),
            decision=decision,
            business_action=action,
            withdrawal=withdrawal,
            runtime_job=job,
        )
    )
    transition = {
        "stale": ("event_stale", "runtime_action_reconciliation"),
        "withdrawn": ("event_withdrawn", "proposal_withdrawn"),
        "failed": ("event_failed", "runtime_failed"),
    }.get(case)
    if transition is not None:
        event_id, event_type = transition
        sources = sources.model_copy(
            update={
                "approval": sources.approval.model_copy(
                    update={
                        "transition_event_id": event_id,
                        "transition_event_hash": "f" * 64,
                        "transition_event_type": event_type,
                    }
                )
            }
        )
    return sources


def test_customer_action_projection_discards_raw_action_and_internal_state() -> None:
    state = project_conversation_action_state(_sources("pending"))
    raw = {
        "id": "ticket_demo",
        "title": "对话",
        "lifecycle": "active",
        "automation_mode": "agent",
        "activity_label": "等待审批",
        "allowed_actions": ["append_message"],
        "turns": [
            {
                "id": state.origin_turn_id,
                "messages": [
                    {
                        "approval_id": state.approval_id,
                    }
                ],
            }
        ],
        "pending_actions": [
            {
                "id": state.approval_id,
                "turn_id": "turn_poison",
                "action_payload": {
                    "billing_record_id": "payload_identity_must_not_win",
                    "amount": "49.00",
                    "currency": "USD",
                    "prompt": "Bearer customer-action-secret",
                    "raw_result": "<script>customer-action-poison</script>",
                },
                "business_action_id": "internal_action_id",
                "source_event_hash": "f" * 64,
                "created_at": "private-created-at",
            }
        ],
        "turn_pagination": {
            "limit": 100,
            "returned": 1,
            "has_more": False,
            "next_before_ordinal": None,
        },
        "created_at": NOW,
        "updated_at": NOW,
    }

    projected = _apply_conversation_action_projection(raw, (state,))
    validated = ConversationDetailResponse.model_validate(projected).model_dump(mode="json")
    action = validated["pending_actions"][0]

    assert action["action_payload"] == {
        "billing_record_id": "bill_duplicate",
        "api_key_id": None,
        "subscription_id": None,
        "amount": "49.00",
        "currency": "USD",
        "target": None,
    }
    assert action["turn_id"] == state.origin_turn_id
    assert action["created_at"] == state.created_at.isoformat().replace("+00:00", "Z")
    serialized = str(action)
    for poison in (
        "Bearer customer-action-secret",
        "<script>customer-action-poison</script>",
        "internal_action_id",
        "private-created-at",
        "payload_identity_must_not_win",
    ):
        assert poison not in serialized


@pytest.mark.parametrize(
    ("case", "execution_state", "decision_class", "actionable"),
    [
        ("pending", "not_started", "none", True),
        ("approved", "queued", "approve", False),
        ("executing", "in_progress", "approve", False),
        ("verification_pending", "verification_pending", "approve", False),
        ("executed", "succeeded", "approve", False),
        ("rejected", "not_executed", "reject", False),
        ("stale", "not_executed", "approve", False),
        ("withdrawn", "not_executed", "customer_withdrawal", False),
        ("failed", "failed", "approve", False),
        ("manual_takeover_legacy", "legacy_stopped", "legacy_manual_takeover", False),
    ],
)
def test_projector_maps_every_frozen_customer_status(
    case: str,
    execution_state: str,
    decision_class: str,
    actionable: bool,
) -> None:
    state = project_conversation_action_state(_sources(case))

    assert state.schema_version == "conversation-action-state.v1"
    assert state.projection_status == case
    assert state.execution_state == execution_state
    assert state.decision_class == decision_class
    assert state.actionable is actionable
    assert state.allowed_customer_actions == (("withdraw",) if actionable else ())
    assert state.grants_action_authority is False


def test_projector_uses_canonical_identity_and_safe_fields_only() -> None:
    sources = _sources("executed")
    state = project_conversation_action_state(sources)
    payload = state.model_dump(mode="json")

    assert state.approval_status == "executed"
    assert state.projection_status == "executed"
    assert state.resource_id == "bill_duplicate"
    assert state.resource_version == 2
    assert state.business_action_id == f"action_{sources.approval.id}"
    assert state.source_event_id == f"event_action_{sources.approval.id}"
    assert state.source_event_hash == ACTION_EVENT_HASH
    assert state.updated_at == NOW + timedelta(minutes=3)
    forbidden = {
        "action_payload",
        "review_context",
        "reason",
        "approver_note",
        "audit_metadata",
        "result",
        "last_error",
    }
    assert forbidden.isdisjoint(payload)
    assert "bill_payload_must_not_win" not in str(payload)
    assert "private" not in str(payload)
    assert "raw_provider_payload" not in str(payload)

    with pytest.raises(ValidationError):
        ConversationActionStateV1.model_validate({**payload, "grants_action_authority": True})


def test_legacy_checkpoint_projection_uses_updated_at_as_created_at_fallback() -> None:
    payload = project_conversation_action_state(_sources("pending")).model_dump(
        mode="json"
    )
    expected_updated_at = payload["updated_at"]
    del payload["created_at"]

    restored = ConversationActionStateV1.model_validate(payload)

    assert restored.created_at == restored.updated_at
    assert restored.model_dump(mode="json")["created_at"] == expected_updated_at


def test_projector_fails_closed_on_cross_aggregate_identity_conflict() -> None:
    sources = _sources("executed")
    assert sources.business_action is not None
    conflicting_action = sources.business_action.model_copy(update={"resource_version": 99})
    conflicting_sources = sources.model_copy(update={"business_action": conflicting_action})

    with pytest.raises(
        ConversationActionStateProjectionError,
        match="business action identity conflicts",
    ):
        project_conversation_action_state(conflicting_sources)


def test_safe_mapping_adapter_round_trips_without_orm_and_rejects_raw_fields() -> None:
    payload = _sources("verification_pending").model_dump(mode="json")

    sources = conversation_action_sources_from_mapping(payload)
    state = project_conversation_action_state(sources)

    assert sources.schema_version == "conversation-action-source-bundle.v1"
    assert state.projection_status == "verification_pending"
    assert state.source_event_id == f"event_decision_{sources.approval.id}"
    assert sources.runtime_job is not None

    unsafe_payload = sources.model_dump(mode="json")
    assert isinstance(unsafe_payload["runtime_job"], dict)
    unsafe_payload["runtime_job"]["last_error"] = "private provider error"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        conversation_action_sources_from_mapping(unsafe_payload)


def test_terminal_transition_event_precedes_legacy_lineage_anchor() -> None:
    sources = _sources("withdrawn")
    transition_hash = "f" * 64
    approval = sources.approval.model_copy(
        update={
            "transition_event_id": "event_withdrawn",
            "transition_event_hash": transition_hash,
            "transition_event_type": "proposal_withdrawn",
        }
    )

    state = project_conversation_action_state(
        sources.model_copy(update={"approval": approval})
    )

    assert state.source_event_id == "event_withdrawn"
    assert state.source_event_hash == transition_hash


def test_transition_event_shape_and_terminal_status_mismatch_fail_closed() -> None:
    sources = _sources("withdrawn")
    partial = sources.model_dump(mode="json")
    partial["approval"]["transition_event_id"] = "event_partial"
    partial["approval"]["transition_event_hash"] = None
    with pytest.raises(ValidationError, match="complete or absent"):
        conversation_action_sources_from_mapping(partial)

    mismatched = sources.approval.model_copy(
        update={
            "transition_event_id": "event_failed",
            "transition_event_hash": "f" * 64,
            "transition_event_type": "runtime_failed",
        }
    )
    with pytest.raises(
        ConversationActionStateProjectionError,
        match="transition event conflicts",
    ):
        project_conversation_action_state(
            sources.model_copy(update={"approval": mismatched})
        )


def test_pure_kernel_does_not_require_worker_runtime_jobs_table_grant() -> None:
    grants = expected_worker_table_grants()

    assert "runtime_jobs" not in grants
    assert grants["approval_requests"] == frozenset({"SELECT", "INSERT"})
    state = project_conversation_action_state(_sources("verification_pending"))
    assert state.projection_status == "verification_pending"


@pytest.mark.asyncio
async def test_query_service_is_scoped_bounded_and_read_only(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    approval = _approval("executed", approval_id="approval_query")
    proposal = _proposal(approval)
    decision = _decision(approval, "approve")
    action = _business_action(approval)
    action.human_decision_id = decision.id
    job = _job(approval, status="succeeded", outcome="completed")
    db_session.add_all([proposal, approval])
    await db_session.flush()
    db_session.add_all([decision, action, job])
    await db_session.commit()

    models = (
        ApprovalRequest,
        ProposalRecord,
        HumanDecision,
        BusinessAction,
        ProposalWithdrawal,
        RuntimeJob,
    )
    before = {
        model.__tablename__: int(
            await db_session.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in models
    }
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted

    projector = ConversationActionStateProjector(db_session)
    states = await projector.list_for_ticket(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        ticket_id="ticket_demo",
        limit=10,
    )
    by_id = {state.approval_id: state for state in states}
    state = by_id[approval.id]
    assert state.projection_status == "executed"
    assert state.source_event_id == action.canonical_event_id
    assert state.source_event_hash == action.canonical_event_hash
    assert state.business_action_id == action.id
    assert (
        await projector.get_for_approval(
            tenant_id="tenant_other",
            customer_id="cust_other",
            approval_id=approval.id,
        )
        is None
    )
    with pytest.raises(ValueError, match="outside bound"):
        await projector.list_for_ticket(
            tenant_id="tenant_demo",
            customer_id="cust_demo",
            ticket_id="ticket_demo",
            limit=101,
        )

    after = {
        model.__tablename__: int(
            await db_session.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in models
    }
    assert after == before
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


@pytest.mark.asyncio
async def test_query_service_unions_ticket_owned_and_message_aliased_approvals(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    alias_ticket = SupportTicket(
        id="ticket_alias",
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        status="open",
        issue_type="billing",
        next_message_sequence=1,
    )
    alias_customer_message = TicketMessage(
        id="message_alias_customer",
        tenant_id="tenant_demo",
        ticket_id=alias_ticket.id,
        role="user",
        message_kind="customer",
        conversation_sequence=1,
        content="Create an approval owned by this other conversation.",
    )
    db_session.add_all([alias_ticket, alias_customer_message])
    await db_session.flush()
    alias_run = AgentRun(
        id="run_alias",
        tenant_id="tenant_demo",
        ticket_id=alias_ticket.id,
        customer_id="cust_demo",
        message_id=alias_customer_message.id,
        status="interrupted",
        checkpoint_stage="awaiting_approval",
        checkpoint_id="checkpoint_alias",
        model="fake",
        provider_mode="fake",
        tool_call_mode="native",
        prompt_version="v1.1",
        schema_version="agent.v1",
        context_version="context.v1",
    )
    db_session.add(alias_run)
    await db_session.flush()
    alias_turn = ConversationTurn(
        id="turn_alias",
        tenant_id="tenant_demo",
        ticket_id=alias_ticket.id,
        customer_message_id=alias_customer_message.id,
        run_id=alias_run.id,
        ordinal=1,
        activity_state="waiting_external",
        automation_mode="agent",
        model=alias_run.model,
        provider_mode=alias_run.provider_mode,
        tool_call_mode=alias_run.tool_call_mode,
        context_version=alias_run.context_version,
    )
    db_session.add(alias_turn)
    alias_run.turn_id = alias_turn.id
    alias_customer_message.turn_id = alias_turn.id
    await db_session.flush()

    older = _approval(
        "rejected",
        approval_id="approval_owned_older",
        updated_at=NOW,
    )
    newer = _approval(
        "rejected",
        approval_id="approval_alias_newer",
        updated_at=NOW + timedelta(minutes=1),
    )
    newer.ticket_id = alias_ticket.id
    newer.run_id = alias_run.id
    newer.origin_turn_id = alias_turn.id
    older_decision = _decision(older, "reject")
    newer_decision = _decision(newer, "reject")
    db_session.add_all(
        [
            _proposal(older),
            _proposal(newer),
            older,
            newer,
            older_decision,
            newer_decision,
        ]
    )
    await db_session.flush()

    owner_ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert owner_ticket is not None
    owner_ticket.next_message_sequence = 3
    db_session.add_all(
        [
            TicketMessage(
                id="message_alias_newer",
                tenant_id="tenant_demo",
                ticket_id=owner_ticket.id,
                approval_id=newer.id,
                role="assistant",
                message_kind="action_update",
                conversation_sequence=2,
                content="Canonical approval alias.",
            ),
            TicketMessage(
                id="message_alias_newer_duplicate",
                tenant_id="tenant_demo",
                ticket_id=owner_ticket.id,
                approval_id=newer.id,
                role="assistant",
                message_kind="action_update",
                conversation_sequence=3,
                content="Repeated alias must not duplicate action state.",
            ),
        ]
    )
    await db_session.commit()

    projector = ConversationActionStateProjector(db_session)
    states = await projector.list_for_ticket(
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        ticket_id=owner_ticket.id,
    )

    assert [state.approval_id for state in states] == [newer.id, older.id]
    assert all(state.projection_status == "rejected" for state in states)
    assert (
        await projector.list_for_ticket(
            tenant_id="tenant_demo",
            customer_id="cust_other",
            ticket_id=owner_ticket.id,
        )
        == ()
    )


def test_unknown_effect_and_dead_job_fail_closed_to_customer_safe_states() -> None:
    verification_orm = _approval("approved", approval_id="approval_unknown")
    verification_job = _job(
        verification_orm,
        status="succeeded",
        delivery_hold_reason="state_unknown",
    )
    pending_effect = _business_action(verification_orm, status="unknown")
    projected = project_conversation_action_state(
        conversation_action_sources_from_orm(
            ConversationActionOrmSources(
                approval=verification_orm,
                proposal=_proposal(verification_orm),
                decision=_decision(verification_orm, "approve"),
                business_action=pending_effect,
                runtime_job=verification_job,
            )
        )
    )
    assert projected.projection_status == "verification_pending"
    assert projected.customer_safe_reason_code == ("action_execution_verification_pending")
    assert projected.allowed_customer_actions == ()

    failed_orm = _approval("failed", approval_id="approval_dead")
    failed_sources = conversation_action_sources_from_orm(
        ConversationActionOrmSources(
            approval=failed_orm,
            proposal=_proposal(failed_orm),
            decision=_decision(failed_orm, "approve"),
            runtime_job=_job(failed_orm, status="dead", outcome="failed"),
        )
    )
    failed_sources = failed_sources.model_copy(
        update={
            "approval": failed_sources.approval.model_copy(
                update={
                    "transition_event_id": "event_failed",
                    "transition_event_hash": "f" * 64,
                    "transition_event_type": "runtime_failed",
                }
            )
        }
    )
    projected_failed = project_conversation_action_state(
        failed_sources
    )
    assert projected_failed.projection_status == "failed"
    assert projected_failed.customer_safe_reason_code == ("action_failed_confirmed_no_effect")


def test_projector_rejects_unconverged_terminal_source_combinations() -> None:
    dead_approval = _approval("approved", approval_id="approval_dead_unconverged")
    with pytest.raises(
        ConversationActionStateProjectionError,
        match="dead approval job has not converged",
    ):
        project_conversation_action_state(
            conversation_action_sources_from_orm(
                ConversationActionOrmSources(
                    approval=dead_approval,
                    proposal=_proposal(dead_approval),
                    decision=_decision(dead_approval, "approve"),
                    runtime_job=_job(dead_approval, status="dead", outcome="failed"),
                )
            )
        )

    withdrawal_approval = _approval("pending", approval_id="approval_withdraw_unconverged")
    with pytest.raises(
        ConversationActionStateProjectionError,
        match="withdrawal conflicts with approval status",
    ):
        project_conversation_action_state(
            conversation_action_sources_from_orm(
                ConversationActionOrmSources(
                    approval=withdrawal_approval,
                    proposal=_proposal(withdrawal_approval),
                    withdrawal=_withdrawal(withdrawal_approval),
                )
            )
        )

    unknown_effect_approval = _approval("failed", approval_id="approval_unknown_terminal")
    with pytest.raises(
        ConversationActionStateProjectionError,
        match="unresolved business action conflicts",
    ):
        project_conversation_action_state(
            conversation_action_sources_from_orm(
                ConversationActionOrmSources(
                    approval=unknown_effect_approval,
                    proposal=_proposal(unknown_effect_approval),
                    decision=_decision(unknown_effect_approval, "approve"),
                    business_action=_business_action(
                        unknown_effect_approval,
                        status="unknown",
                    ),
                )
            )
        )


@pytest.mark.parametrize(
    ("case", "source_name"),
    [
        ("pending", "proposal"),
        ("executed", "business_action"),
        ("rejected", "decision"),
        ("withdrawn", "withdrawal"),
        ("stale", "transition_event"),
        ("failed", "transition_event"),
    ],
)
def test_projector_rejects_missing_authoritative_lifecycle_sources(
    case: str,
    source_name: str,
) -> None:
    sources = _sources(case)
    if source_name == "transition_event":
        sources = sources.model_copy(
            update={
                "approval": sources.approval.model_copy(
                    update={
                        "transition_event_id": None,
                        "transition_event_hash": None,
                        "transition_event_type": None,
                    }
                )
            }
        )
    else:
        sources = sources.model_copy(update={source_name: None})

    with pytest.raises(
        ConversationActionStateProjectionError,
        match="missing or conflicts|incomplete or contradictory",
    ):
        project_conversation_action_state(sources)


def test_projector_never_uses_effect_as_a_substitute_for_executed_approval() -> None:
    sources = _sources("executed")
    inconsistent = sources.model_copy(
        update={
            "approval": sources.approval.model_copy(update={"status": "approved"}),
            "proposal": sources.proposal.model_copy(update={"status": "bound"})
            if sources.proposal is not None
            else None,
        }
    )

    with pytest.raises(
        ConversationActionStateProjectionError,
        match="successful business action requires an executed approval",
    ):
        project_conversation_action_state(inconsistent)


@pytest.mark.parametrize("case", ["withdrawn", "stale", "failed"])
def test_terminal_projection_never_falls_back_to_older_event_lineage(case: str) -> None:
    sources = _sources(case)
    assert sources.approval.transition_event_id is not None
    state = project_conversation_action_state(sources)

    assert state.source_event_id == sources.approval.transition_event_id
    assert state.source_event_hash == sources.approval.transition_event_hash
    assert state.source_event_id != sources.approval.expected_ticket_head_event_id
    if sources.decision is not None:
        assert state.source_event_id != sources.decision.canonical_event_id


@pytest.mark.parametrize("case", ["rejected", "withdrawn", "stale", "failed"])
def test_no_effect_terminals_never_become_customer_actionable(case: str) -> None:
    state = project_conversation_action_state(_sources(case))

    assert state.actionable is False
    assert state.allowed_customer_actions == ()
    assert state.execution_state in {"not_executed", "failed"}
    assert state.business_action_id is None
