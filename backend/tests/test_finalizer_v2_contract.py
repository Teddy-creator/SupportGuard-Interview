from __future__ import annotations

import pytest
from pydantic import ValidationError

from current_predicate_facts import record_predicate_operands
from supportguard.contracts.finalizer import (
    AgentCompleteDelta,
    FinalizerHeadsV2,
    FinalizerPayloadV2,
    HitlInterruptDelta,
    finalizer_payload_mismatch_paths,
)


def heads() -> FinalizerHeadsV2:
    return FinalizerHeadsV2(
        expected_ticket_head_event_id=None,
        expected_ticket_sequence=0,
        expected_ticket_event_hash=None,
        expected_run_status="running",
        expected_run_status_version=1,
        parent_checkpoint_id=None,
        parent_checkpoint_hash=None,
        parent_checkpoint_version=0,
        final_checkpoint_id="checkpoint-final",
        final_checkpoint_hash="a" * 64,
        final_checkpoint_version=1,
        expected_marker_status_version=2,
        expected_tool_ledger_head="b" * 64,
        expected_capability_ledger_head="c" * 64,
        expected_proposal_ledger_head="9" * 64,
        expected_budget_ledger_head="d" * 64,
        expected_context_snapshot_hash="e" * 64,
        expected_context_ledger_hash="f" * 64,
        expected_domain_resource_versions={"ticket:ticket_demo": 1},
    )


def build(domain_delta: AgentCompleteDelta | HitlInterruptDelta) -> FinalizerPayloadV2:
    return FinalizerPayloadV2.build(
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        run_id="run_demo",
        job_id="job_demo",
        segment_id="marker_demo",
        delivery_generation=1,
        fencing_token=1,
        parent_segment_id=None,
        marker_id="marker_demo",
        segment_kind="agent_start",
        prepared_payload_hash="1" * 64,
        expected_heads=heads(),
        state={"final": {"terminal_state": "resolved"}},
        domain_delta=domain_delta,
    )


def test_finalizer_v2_hash_binds_typed_heads_and_variant() -> None:
    payload = build(AgentCompleteDelta())
    payload.verify()
    assert payload.schema_version == "finalizer.v2"
    assert payload.domain_delta.variant == "agent_complete"


def test_interrupt_variant_requires_snapshot_and_observation_binding() -> None:
    with pytest.raises(ValidationError):
        HitlInterruptDelta.model_validate(
            {
                "proposal_id": "proposal_demo",
                "proposal_hash": "a" * 64,
                "approval_snapshot_hash": "b" * 64,
            }
        )


def test_untyped_or_tampered_finalizer_payload_is_rejected() -> None:
    payload = build(AgentCompleteDelta())
    tampered = payload.model_dump(mode="json")
    tampered["expected_heads"] = {"canonical_parent_id": None}
    with pytest.raises(ValidationError) as shape_error:
        FinalizerPayloadV2.model_validate(tampered)

    tampered = payload.model_copy(update={"payload_hash": "0" * 64})
    with pytest.raises(ValueError, match="payload hash mismatch") as hash_error:
        tampered.verify()
    record_predicate_operands(
        requirement_id="C4-P0-05d",
        predicate_id="c4_p0_05d",
        subject_kind="typed_finalizer_payload_contract",
        operands={
            "shape_error": str(shape_error.value),
            "hash_error": str(hash_error.value),
            "tampered_hash": tampered.payload_hash,
            "expected_nonzero_hash": payload.payload_hash,
        },
    )


def test_finalizer_payload_mismatch_paths_report_names_without_values() -> None:
    persisted = build(AgentCompleteDelta())
    rebuilt = FinalizerPayloadV2.build(
        tenant_id=persisted.tenant_id,
        ticket_id=persisted.ticket_id,
        run_id=persisted.run_id,
        job_id=persisted.job_id,
        segment_id=persisted.segment_id,
        delivery_generation=persisted.delivery_generation,
        fencing_token=persisted.fencing_token,
        parent_segment_id=persisted.parent_segment_id,
        marker_id=persisted.marker_id,
        segment_kind=persisted.segment_kind,
        prepared_payload_hash=persisted.prepared_payload_hash,
        expected_heads=persisted.expected_heads.model_copy(
            update={
                "expected_budget_ledger_head": "7" * 64,
                "expected_domain_resource_versions": {
                    "ticket:ticket_demo": 2,
                    "proposal:proposal_demo": 1,
                },
            }
        ),
        state=persisted.state_delta.state,
        domain_delta=persisted.domain_delta,
    )

    paths = finalizer_payload_mismatch_paths(persisted, rebuilt)

    assert paths == (
        "expected_heads.expected_budget_ledger_head",
        "expected_heads.expected_domain_resource_versions.proposal:proposal_demo",
        "expected_heads.expected_domain_resource_versions.ticket:ticket_demo",
    )
    assert all("7" * 64 not in path for path in paths)
