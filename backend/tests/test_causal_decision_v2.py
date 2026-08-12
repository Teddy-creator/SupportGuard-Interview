from __future__ import annotations

import pytest
from pydantic import ValidationError

from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import (
    EscalationCausalDecisionV2,
    ProposalCausalDecisionV2,
)
from supportguard.contracts.context import PolicyCapabilityMcpCallContext


def _proposal(**updates: object) -> ProposalCausalDecisionV2:
    payload: dict[str, object] = {
        "capability_name": "propose_refund",
        "action_type": "refund",
        "resource_id": "bill_中文",
        "resource_version": 2,
        "model_arguments": {
            "billing_record_id": "bill_中文",
            "refund_reason": "重复扣费😀",
            "idempotency_key": "idem_1",
        },
        "observation_binding_hash": "b" * 64,
        "policy_version": "supportguard-policy-gate.v1",
    }
    payload.update(updates)
    return ProposalCausalDecisionV2.model_validate(payload)


def test_proposal_payload_is_exact_and_unicode_hash_is_stable() -> None:
    decision = _proposal()
    assert set(decision.model_dump()) == {
        "variant",
        "capability_name",
        "action_type",
        "resource_id",
        "resource_version",
        "model_arguments",
        "observation_binding_hash",
        "policy_version",
    }
    assert canonical_json_hash(decision.model_dump()) == canonical_json_hash(
        decision.model_dump(mode="json")
    )


def test_escalation_payload_is_exact() -> None:
    decision = EscalationCausalDecisionV2(
        ticket_id="ticket_a",
        ticket_version=3,
        customer_id="customer_a",
        model_arguments={"reason": "需要人工", "idempotency_key": "idem_a"},
        observation_binding_hash="c" * 64,
        policy_version="supportguard-policy-gate.v1",
    )
    assert set(decision.model_dump()) == {
        "variant",
        "capability_name",
        "ticket_id",
        "ticket_version",
        "customer_id",
        "model_arguments",
        "observation_binding_hash",
        "policy_version",
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"unknown": "field"},
        {"capability_name": "propose_api_key_revocation"},
        {"action_type": "api_key_revocation"},
        {"model_arguments": {"amount": 1.5}},
        {"resource_version": 0},
    ],
)
def test_proposal_schema_fails_closed(updates: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _proposal(**updates)


def test_context_rejects_payload_or_binding_hash_substitution() -> None:
    decision = _proposal()
    with pytest.raises(ValidationError, match="causal decision hash"):
        PolicyCapabilityMcpCallContext(
            capability_invocation_id="cap_1",
            capability_attempt_id="attempt_1",
            capability_name="propose_refund",
            effect_identity="e" * 64,
            capability_attempt=1,
            capability_sequence=1,
            causal_decision_hash="a" * 64,
            causal_decision=decision,
            observation_binding_hash="b" * 64,
            call_deadline="2026-07-18T00:00:00Z",
            worker_deadline="2026-07-18T00:00:01Z",
        )

    with pytest.raises(ValidationError, match="observation binding hash"):
        PolicyCapabilityMcpCallContext(
            capability_invocation_id="cap_1",
            capability_attempt_id="attempt_1",
            capability_name="propose_refund",
            effect_identity="e" * 64,
            capability_attempt=1,
            capability_sequence=1,
            causal_decision_hash=canonical_json_hash(decision.model_dump()),
            causal_decision=decision,
            observation_binding_hash="d" * 64,
            call_deadline="2026-07-18T00:00:00Z",
            worker_deadline="2026-07-18T00:00:01Z",
        )
