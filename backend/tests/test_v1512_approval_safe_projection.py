from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from supportguard.api.approval_projection import (
    ApprovalDetailResponse,
    project_approval_detail,
)

_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_POISONS = (
    "Bearer projection-secret",
    "<script>projection-poison</script>",
    "MCP_RAW_PROJECTION_POISON",
    "Traceback: PROJECTION_EXCEPTION_POISON",
)
_FORBIDDEN_KEYS = {
    "review_context_raw",
    "original_ticket",
    "redacted_ticket",
    "observation_binding",
    "action_hash",
    "snapshot_hash",
    "event_hash",
    "idempotency_key",
    "actor_id",
    "last_error",
    "result",
}


def _poisoned_legacy_row() -> dict[str, object]:
    return {
        "id": "approval_safe_projection",
        "ticket_id": "ticket_safe_projection",
        "run_id": "run_private",
        "checkpoint_id": "checkpoint_private",
        "status": "pending",
        "action_type": "refund",
        "resource_type": "billing_record_id",
        "resource_id": "bill_demo_duplicate",
        "origin_turn_id": "turn_safe_projection",
        "action_payload": {
            "billing_record_id": "bill_demo_duplicate",
            "refund_reason": _POISONS[0],
        },
        "requested_change": {},
        "review_context": {
            "original_ticket": _POISONS[1],
            "redacted_ticket": _POISONS[0],
            "tool_observations": [{"mcp_raw": _POISONS[2], "exception": _POISONS[3]}],
        },
        "resource_facts": {
            "status": "charged",
            "amount": "49.00",
            "currency": "USD",
            "duplicate_of": "bill_demo_original",
            "version": 2,
        },
        "risk": "high",
        "business_version": 2,
        "status_version": 1,
        "actionable": True,
        "proposal_summary": {"status": "bound", "resource_version": 2},
        "proposal": {
            "observation_binding": [{"exception": _POISONS[3]}],
            "action_hash": "a" * 64,
        },
        "snapshot_summary": {
            "resource_version": 2,
            "policy_bound": True,
            "citation_count": 2,
        },
        "snapshot": {
            "snapshot_hash": "b" * 64,
            "action_hash": "c" * 64,
            "policy_binding": {"private": _POISONS[0]},
        },
        "original_request": "请核验 bill_demo_duplicate 是否重复扣费并按政策退款。",
        "evidence_summaries": [
            {
                "title": "计费、重复扣费与退款政策",
                "section_path": "重复扣费 > 决策矩阵",
                "version": "3.1",
                "freshness": "current",
            },
            {
                "title": "计费、重复扣费与退款政策",
                "section_path": "重复扣费 > 操作检查清单",
                "version": "3.1",
                "freshness": "current",
            },
        ],
        "ticket_summary": {"status": "awaiting_approval"},
        "ticket": {"final_response": _POISONS[1]},
        "human_decision_summary": None,
        "human_decision": {"actor_id": _POISONS[0], "reason": _POISONS[3]},
        "resume_job_summary": None,
        "resume_job": {"last_error": _POISONS[3], "outcome": _POISONS[2]},
        "business_action_summary": None,
        "business_action": {"result": {"secret": _POISONS[0]}},
        "action_hash": "d" * 64,
        "idempotency_key": _POISONS[0],
        "created_at": _NOW,
        "updated_at": _NOW,
        "decided_at": None,
        "consumed_at": None,
    }


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_walk_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value))
    return set()


def test_approval_projection_discards_legacy_json_and_preserves_safe_review_facts() -> None:
    projected = project_approval_detail(_poisoned_legacy_row())
    payload = projected.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["resource_identity"] == {
        "resource_type": "billing_record_id",
        "resource_id": "bill_demo_duplicate",
        "origin_turn_id": "turn_safe_projection",
        "identity_source": "persisted",
        "identity_complete": True,
    }
    assert payload["action_payload"] == {
        "billing_record_id": "bill_demo_duplicate",
        "amount": "49.00",
        "currency": "USD",
        "refund_reason": None,
        "original_billing_record_id": "bill_demo_original",
    }
    assert payload["review_context"]["freshness"] == {
        "status": "current",
        "proposed_version": 2,
        "current_version": 2,
    }
    assert payload["review_context"]["original_request"] == (
        "请核验 bill_demo_duplicate 是否重复扣费并按政策退款。"
    )
    assert payload["review_context"]["tool_observations"] == [
        {
            "data": {
                "kind": "billing_record",
                "billing_record_id": "bill_demo_duplicate",
                "status": "charged",
                "amount": "49.00",
                "currency": "USD",
                "duplicate_of": "bill_demo_original",
                "version": 2,
            }
        }
    ]
    assert payload["review_context"]["evidence"] == [
        {
            "title": "计费、重复扣费与退款政策",
            "section_path": "重复扣费 > 决策矩阵",
            "version": "3.1",
            "freshness": "current",
        },
        {
            "title": "计费、重复扣费与退款政策",
            "section_path": "重复扣费 > 操作检查清单",
            "version": "3.1",
            "freshness": "current",
        },
    ]
    assert payload["review_context"]["policy_route"] == "确定性策略与证据已绑定"
    assert payload["proposed_diff"][-1]["proposed"] == "按原始审批快照"
    assert payload["allowed_actions"] == ["approve", "edit_and_approve", "reject"]
    assert _walk_keys(payload).isdisjoint(_FORBIDDEN_KEYS)
    assert all(poison not in serialized for poison in _POISONS)


def test_approval_projection_nested_contracts_reject_extra_fields() -> None:
    payload = project_approval_detail(_poisoned_legacy_row()).model_dump(mode="python")
    payload["review_context"]["freshness"]["exception"] = _POISONS[3]

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ApprovalDetailResponse.model_validate(payload)


def test_approval_detail_public_schema_exposes_only_the_safe_dto() -> None:
    schema = ApprovalDetailResponse.model_json_schema()

    assert set(schema["properties"]) == {
        "id",
        "ticket_id",
        "status",
        "action_type",
        "resource_type",
        "resource_id",
        "origin_turn_id",
        "resource_identity",
        "action_payload",
        "review_context",
        "business_version",
        "status_version",
        "resource_summary",
        "risk",
        "actionable",
        "allowed_actions",
        "execution_preconditions",
        "proposed_diff",
        "proposal",
        "ticket",
        "human_decision",
        "resume_job",
        "business_action",
        "created_at",
        "updated_at",
        "decided_at",
        "consumed_at",
    }
    encoded = json.dumps(schema, sort_keys=True)
    for forbidden_key in _FORBIDDEN_KEYS:
        assert f'"{forbidden_key}"' not in encoded


@pytest.mark.parametrize(
    ("action_type", "resource_type", "resource_id", "resource_facts", "requested_change"),
    [
        (
            "api_key_revocation",
            "api_key_id",
            "key_demo_leaked",
            {"status": "active", "version": 2},
            {},
        ),
        (
            "entitlement_change",
            "subscription_id",
            "sub_demo",
            {
                "status": "active",
                "plan": "pro",
                "rpm_limit": 60,
                "concurrency_limit": 40,
                "version": 3,
            },
            {
                "change_type": "quota_change",
                "target": {"concurrency_limit": 60},
            },
        ),
    ],
)
def test_all_action_types_use_authoritative_safe_fact_shapes(
    action_type: str,
    resource_type: str,
    resource_id: str,
    resource_facts: dict[str, object],
    requested_change: dict[str, object],
) -> None:
    raw = _poisoned_legacy_row()
    raw.update(
        {
            "action_type": action_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "resource_facts": resource_facts,
            "requested_change": requested_change,
            "business_version": resource_facts["version"],
        }
    )

    payload = project_approval_detail(raw).model_dump(mode="json")

    assert payload["resource_id"] == resource_id
    assert payload["review_context"]["freshness"]["status"] == "current"
    assert len(payload["review_context"]["tool_observations"]) == 1
    assert ("edit_and_approve" in payload["allowed_actions"]) is (
        action_type == "entitlement_change"
    )
    assert all(poison not in json.dumps(payload, ensure_ascii=False) for poison in _POISONS)


def test_executed_entitlement_diff_preserves_immutable_pre_effect_value() -> None:
    raw = _poisoned_legacy_row()
    raw.update(
        {
            "status": "executed",
            "action_type": "entitlement_change",
            "resource_type": "subscription_id",
            "resource_id": "sub_demo",
            "business_version": 3,
            "resource_facts": {
                "status": "active",
                "plan": "pro",
                "rpm_limit": 1000,
                "concurrency_limit": 48,
                "version": 4,
            },
            "requested_change": {
                "change_type": "quota_change",
                "current": {
                    "plan": "pro",
                    "rpm_limit": 1000,
                    "concurrency_limit": 24,
                },
                "target": {"concurrency_limit": 48},
            },
            "actionable": False,
            "human_decision_summary": {
                "decision": "edit_and_approve",
                "created_at": _NOW,
            },
            "business_action_summary": {
                "status": "succeeded",
                "resource_version": 3,
                "created_at": _NOW,
            },
        }
    )

    payload = project_approval_detail(raw).model_dump(mode="json")

    assert payload["proposed_diff"] == [
        {
            "field": "concurrency_limit",
            "current": "24",
            "proposed": "48",
        }
    ]
    assert payload["review_context"]["freshness"]["status"] == "changed_since_proposal"
