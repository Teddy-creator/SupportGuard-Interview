from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from supportguard.agent.action_specs import get_action_spec
from supportguard.agent.obligations import (
    ActionObligationLedger,
    evaluate_action_obligations,
)
from supportguard.agent.responses import (
    render_executed_action_update,
    render_terminal_business_outcome,
)
from supportguard.contracts.action_preconditions import (
    ActionAdmissionV2,
    resolve_action_admission_v2,
)

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
RUN_ID = "run-v1510"
TENANT_ID = "tenant-v1510"
CUSTOMER_ID = "customer-v1510"


def _admission(
    action_type: str,
    *,
    target: int = 40,
) -> ActionAdmissionV2:
    messages = {
        "refund": "请给账单 bill_terminal 退款。",
        "api_key_revocation": "请立即撤销 API Key key_terminal。",
        "entitlement_change": f"请把并发上限调整到 {target}。",
    }
    issues = {
        "refund": "billing_refund",
        "api_key_revocation": "credential_security",
        "entitlement_change": "entitlement_change",
    }
    return resolve_action_admission_v2(
        messages[action_type],
        [],
        requested_action=action_type,  # type: ignore[arg-type]
        issue_type=issues[action_type],
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        current_message_id="message-v1510",
        turn_group_id="turn-v1510",
        requested_concurrency_limit=(
            target if action_type == "entitlement_change" else None
        ),
    )


def _observation(
    admission: ActionAdmissionV2,
    *,
    tool_name: str,
    data: dict[str, Any] | None,
    status: str = "ok",
    source: bool = True,
    freshness_status: str = "fresh",
    tenant_id: str = TENANT_ID,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": f"call-{tool_name}",
        "invocation_id": f"invocation-{tool_name}",
        "observation_id": f"observation-{tool_name}",
        "run_id": run_id,
        "attempt_index": 1,
        "status": status,
        "retryable": False,
        "observed_at": NOW.isoformat(),
        "freshness_status": freshness_status,
        "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
        "trusted_scope": {
            "tenant_id": tenant_id,
            "customer_id": CUSTOMER_ID,
            "scope_hash": (
                admission.scope_hash
                if tenant_id == TENANT_ID
                else hashlib.sha256(
                    (
                        '{"customer_id":"customer-v1510",'
                        f'"tenant_id":"{tenant_id}"}}'
                    ).encode()
                ).hexdigest()
            ),
        },
        "data": data or {},
        "source_refs": (
            [
                {
                    "source_type": "business_record",
                    "source_id": f"source-{tool_name}",
                    "observed_at": NOW.isoformat(),
                }
            ]
            if source
            else []
        ),
    }
    if tool_name == "query_billing_record":
        payload["request_binding"] = {
            "arguments_hash": "a" * 64,
            "resource_ref": admission.extracted_arguments.get(
                "billing_record_id"
            ),
        }
    elif tool_name == "query_api_key_metadata":
        payload["request_binding"] = {
            "arguments_hash": "b" * 64,
            "resource_ref": admission.extracted_arguments.get("api_key_ref"),
        }
    return payload


def _ledger(
    action_type: str,
    observation: dict[str, Any],
    *,
    target: int = 40,
) -> ActionObligationLedger:
    admitted = _admission(action_type, target=target)
    return evaluate_action_obligations(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        observations=[observation],
        run_id=RUN_ID,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("action_type", "resource_id", "result", "expected"),
    [
        (
            "refund",
            "bill_terminal",
            {"amount": "49.00", "currency": "USD"},
            "49.00 USD",
        ),
        (
            "api_key_revocation",
            "key_terminal",
            {"status": "revoked"},
            "key_terminal",
        ),
        (
            "entitlement_change",
            "sub_terminal",
            {
                "before": {"concurrency_limit": 24},
                "after": {"concurrency_limit": 48},
            },
            "从 24 调整为 48",
        ),
        (
            "entitlement_change",
            "sub_terminal",
            {"before": {"plan": "starter"}, "after": {"plan": "pro"}},
            "从 starter 调整为 pro",
        ),
    ],
)
def test_committed_action_update_uses_authoritative_safe_result(
    action_type: str,
    resource_id: str,
    result: dict[str, Any],
    expected: str,
) -> None:
    answer = render_executed_action_update(
        action_type,
        resource_id=resource_id,
        result=result,
    )

    assert expected in answer
    assert "安全" in answer


def test_committed_action_update_does_not_render_untrusted_result_values() -> None:
    answer = render_executed_action_update(
        "entitlement_change",
        resource_id="<script>bad</script>",
        result={
            "before": {"plan": "<img onerror=alert(1)>"},
            "after": {"plan": "javascript:alert(1)"},
        },
    )

    assert answer == "订阅 当前资源 的套餐或配额变更已经安全执行完成。"
    assert "script" not in answer
    assert "javascript" not in answer


def test_refunded_billing_record_is_terminal_before_nullable_duplicate_field() -> None:
    admitted = _admission("refund")
    observation = _observation(
        admitted,
        tool_name="query_billing_record",
        data={
            "billing_record_id": "bill_terminal",
            "amount": "49.00",
            "currency": "USD",
            "status": "refunded",
            "duplicate_of": None,
            "version": 3,
        },
    )

    ledger = _ledger("refund", observation)

    assert ledger.next_state == "explain_terminal"
    assert ledger.reason_code == "refund_status_not_actionable"
    assert ledger.terminal_outcome is not None
    assert ledger.terminal_outcome.observed_facts["status"] == "refunded"
    assert ledger.terminal_outcome.binding.source_ids == (
        "source-query_billing_record",
    )
    assert ledger.unsatisfied_capabilities == ()


@pytest.mark.parametrize(
    ("action_type", "tool_name", "data", "expected_code"),
    [
        (
            "refund",
            "query_billing_record",
            {
                "billing_record_id": "bill_terminal",
                "amount": "49.00",
                "currency": "USD",
                "status": "charged",
                "duplicate_of": None,
                "version": 2,
            },
            "refund_duplicate_relation_unconfirmed",
        ),
        (
            "api_key_revocation",
            "query_api_key_metadata",
            {
                "api_key_id": "key_terminal",
                "fingerprint": "fp_terminal",
                "status": "revoked",
                "version": 2,
                "last_used_summary": {},
            },
            "api_key_status_not_actionable",
        ),
        (
            "entitlement_change",
            "query_subscription",
            {
                "subscription_id": "sub_terminal",
                "plan": "pro",
                "status": "inactive",
                "rpm_limit": 1000,
                "concurrency_limit": 20,
                "catalog_eligibility": ["quota_change", "plan_change"],
                "version": 4,
            },
            "subscription_status_not_actionable",
        ),
        (
            "entitlement_change",
            "query_subscription",
            {
                "subscription_id": "sub_terminal",
                "plan": "pro",
                "status": "active",
                "rpm_limit": 1000,
                "concurrency_limit": 40,
                "catalog_eligibility": ["quota_change", "plan_change"],
                "version": 4,
            },
            "entitlement_concurrency_target_noop",
        ),
        (
            "entitlement_change",
            "query_subscription",
            {
                "subscription_id": "sub_terminal",
                "plan": "pro",
                "status": "active",
                "rpm_limit": 1000,
                "concurrency_limit": 20,
                "catalog_eligibility": ["plan_change"],
                "version": 4,
            },
            "entitlement_target_unsupported",
        ),
    ],
)
def test_registry_drives_terminal_business_outcomes(
    action_type: str,
    tool_name: str,
    data: dict[str, Any],
    expected_code: str,
) -> None:
    admitted = _admission(action_type)
    ledger = _ledger(
        action_type,
        _observation(admitted, tool_name=tool_name, data=data),
    )

    assert ledger.next_state == "explain_terminal"
    assert ledger.terminal_outcome is not None
    assert ledger.terminal_outcome.outcome_code == expected_code
    assert ledger.terminal_outcome.proposal_allowed is False
    assert ledger.terminal_outcome.approval_allowed is False
    assert ledger.terminal_outcome.execution_allowed is False


@pytest.mark.parametrize(
    ("action_type", "tool_name"),
    [
        ("refund", "query_billing_record"),
        ("api_key_revocation", "query_api_key_metadata"),
        ("entitlement_change", "query_subscription"),
    ],
)
def test_not_found_is_a_bounded_resource_outcome_without_invented_source(
    action_type: str,
    tool_name: str,
) -> None:
    admitted = _admission(action_type)
    ledger = _ledger(
        action_type,
        _observation(
            admitted,
            tool_name=tool_name,
            data={},
            status="not_found",
            source=False,
        ),
    )

    assert ledger.next_state == "explain_terminal"
    assert ledger.terminal_outcome is not None
    assert ledger.terminal_outcome.terminal_class == "resource_not_available"
    assert ledger.terminal_outcome.binding.source_ids == ()


def test_not_found_for_a_different_requested_resource_is_not_terminal() -> None:
    admitted = _admission("refund")
    observation = _observation(
        admitted,
        tool_name="query_billing_record",
        data={},
        status="not_found",
        source=False,
    )
    observation["request_binding"]["resource_ref"] = "bill_other"

    ledger = _ledger("refund", observation)

    assert ledger.terminal_outcome is None
    assert ledger.next_state == "collect_reads"


def test_stale_or_wrong_scope_fact_never_becomes_a_business_terminal() -> None:
    admitted = _admission("refund")
    refunded = {
        "billing_record_id": "bill_terminal",
        "amount": "49.00",
        "currency": "USD",
        "status": "refunded",
        "duplicate_of": None,
        "version": 3,
    }
    stale = _ledger(
        "refund",
        _observation(
            admitted,
            tool_name="query_billing_record",
            data=refunded,
            freshness_status="stale",
        ),
    )
    wrong_scope = _ledger(
        "refund",
        _observation(
            admitted,
            tool_name="query_billing_record",
            data=refunded,
            tenant_id="tenant-other",
        ),
    )

    assert stale.terminal_outcome is None
    assert stale.next_state == "collect_reads"
    assert wrong_scope.terminal_outcome is None
    assert wrong_scope.next_state == "safe_stop"
    assert wrong_scope.reason_code == "observation_scope_mismatch"


def test_invalid_tool_output_schema_survives_obligation_aggregation() -> None:
    admitted = _admission("refund")
    observation = _observation(
        admitted,
        tool_name="query_billing_record",
        data={},
        status="invalid_input",
        source=False,
    )
    observation["error_code"] = "tool_output_schema_invalid"

    ledger = _ledger("refund", observation)

    assert ledger.next_state == "safe_stop"
    assert ledger.reason_code == "tool_output_schema_invalid"


def test_normal_actionable_resource_does_not_trigger_terminal_path() -> None:
    admitted = _admission("refund")
    ledger = _ledger(
        "refund",
        _observation(
            admitted,
            tool_name="query_billing_record",
            data={
                "billing_record_id": "bill_terminal",
                "amount": "49.00",
                "currency": "USD",
                "status": "charged",
                "duplicate_of": "bill_original",
                "version": 2,
            },
        ),
    )

    assert ledger.terminal_outcome is None
    assert ledger.next_state == "collect_reads"


def test_terminal_outcome_hash_and_rendering_are_stable_and_actionable() -> None:
    admitted = _admission("refund")
    observation = _observation(
        admitted,
        tool_name="query_billing_record",
        data={
            "billing_record_id": "bill_terminal",
            "amount": "49.00",
            "currency": "USD",
            "status": "refunded",
            "duplicate_of": None,
            "version": 3,
        },
    )
    first = _ledger("refund", observation).terminal_outcome
    second = _ledger("refund", dict(observation)).terminal_outcome

    assert first is not None and second is not None
    assert first.outcome_hash == second.outcome_hash
    rendering = render_terminal_business_outcome(first)
    assert "已经退款" in rendering.answer
    assert "不会再次创建退款申请" in rendering.answer
    assert "没有创建审批" in rendering.answer
    assert rendering.material_claim is not None


def test_v1_ledger_remains_readable_without_terminal_outcome() -> None:
    legacy = ActionObligationLedger.model_validate(
        {
            "schema_version": "action-obligation-ledger.v1",
            "action_spec_version": "action-spec.v1",
            "action_type": "refund",
            "run_id": RUN_ID,
            "tenant_id": TENANT_ID,
            "customer_id": CUSTOMER_ID,
            "scope_hash": _admission("refund").scope_hash,
            "obligations": [],
            "unsatisfied_capabilities": [],
            "next_state": "collect_reads",
            "reason_code": "legacy_checkpoint",
        }
    )

    assert legacy.terminal_outcome is None
    assert legacy.schema_version == "action-obligation-ledger.v1"
