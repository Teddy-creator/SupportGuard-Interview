from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from supportguard.agent.conversation_semantics import resolve_action_state_query

ROOT = Path(__file__).resolve().parents[2]
SEMANTICS_SOURCE = ROOT / "backend/src/supportguard/agent/conversation_semantics.py"
INTAKE_SOURCE = ROOT / "backend/src/supportguard/agent/nodes/intake.py"
LEGACY_SUPPORT_SOURCE = ROOT / "backend/src/supportguard/agent/nodes/decision_support.py"


def _action_state(
    *,
    approval_id: str,
    action_type: str,
    resource_id: str,
    projection_status: str = "failed",
) -> dict[str, Any]:
    return {
        "schema_version": "conversation-action-state.v1",
        "approval_id": approval_id,
        "origin_run_id": "run_origin",
        "origin_turn_id": "turn_origin",
        "action_type": action_type,
        "resource_type": {
            "refund": "billing_record",
            "api_key_revocation": "api_key",
            "entitlement_change": "subscription",
        }[action_type],
        "resource_id": resource_id,
        "resource_version": 2,
        "approval_status": projection_status,
        "projection_status": projection_status,
        "status_version": 3,
        "actionable": False,
        "allowed_customer_actions": [],
        "decision_class": "none",
        "customer_safe_reason_code": {
            "failed": "action_failed_confirmed_no_effect",
            "rejected": "approval_rejected_no_effect",
        }[projection_status],
        "execution_state": "failed" if projection_status == "failed" else "not_executed",
        "business_action_id": None,
        "updated_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
        "source_event_id": "event_action",
        "source_event_hash": "a" * 64,
        "grants_action_authority": False,
    }


def test_conversation_semantics_is_the_only_current_action_query_owner() -> None:
    semantics_tree = ast.parse(SEMANTICS_SOURCE.read_text())
    functions = {
        node.name: node
        for node in semantics_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "resolve_action_state_query" in functions
    assert (
        max(
            node.end_lineno - node.lineno + 1
            for node in functions.values()
            if node.end_lineno is not None
        )
        < 200
    )

    intake_source = INTAKE_SOURCE.read_text()
    legacy_source = LEGACY_SUPPORT_SOURCE.read_text()
    assert "action_state_query = resolve_action_state_query(" in intake_source
    assert "def _resolve_action_state_query(" not in intake_source
    assert "def _resolve_action_state_query(" not in legacy_source


def test_ambiguous_api_key_options_never_echo_credential_references() -> None:
    opaque_reference = "key_customer_opaque_42"
    query = resolve_action_state_query(
        f"{opaque_reference} 的 API Key 撤销申请状态怎么样了？",
        [
            _action_state(
                approval_id="approval_key_1",
                action_type="api_key_revocation",
                resource_id=opaque_reference,
            ),
            _action_state(
                approval_id="approval_key_2",
                action_type="api_key_revocation",
                resource_id=opaque_reference,
            ),
        ],
    )

    assert query is not None
    assert query["resolution"] == "ambiguous"
    assert query["grants_action_authority"] is False
    assert all(
        option
        == {
            "action_type": "api_key_revocation",
            "resource_type": "api_key",
            "projection_status": "failed",
            "resource_reference_hidden": True,
        }
        for option in query["candidate_options"]
    )
    assert opaque_reference not in json.dumps(query, ensure_ascii=False)


def test_ambiguous_noncredential_options_keep_stable_explainable_order() -> None:
    actions = [
        _action_state(
            approval_id="approval_bill_2",
            action_type="refund",
            resource_id="bill_2",
        ),
        _action_state(
            approval_id="approval_bill_1",
            action_type="refund",
            resource_id="bill_1",
        ),
    ]

    query = resolve_action_state_query(
        "bill_2 和 bill_1 的退款申请状态怎么样了？",
        actions,
    )
    reversed_query = resolve_action_state_query(
        "bill_2 和 bill_1 的退款申请状态怎么样了？",
        list(reversed(actions)),
    )

    assert query == reversed_query
    assert query is not None
    assert query["resolution"] == "ambiguous"
    assert [item["resource_id"] for item in query["candidate_options"]] == [
        "bill_1",
        "bill_2",
    ]
    assert query["grants_action_authority"] is False
