from __future__ import annotations

import pytest

from supportguard.services.approval_edits import (
    ApprovalEditNotAllowed,
    apply_approval_edit,
    revision_matches_approval_edit,
)


def _entitlement_payload() -> dict[str, object]:
    return {
        "subscription_id": "sub_demo",
        "customer_id": "cust_demo",
        "change_type": "quota_change",
        "current": {
            "plan": "PRO",
            "rpm_limit": 1000,
            "concurrency_limit": 24,
        },
        "target": {"concurrency_limit": 40},
        "reason": "Customer requested a concurrency adjustment.",
        "business_version": 3,
    }


def test_entitlement_edit_changes_only_the_nested_concurrency_target() -> None:
    base = _entitlement_payload()

    revised = apply_approval_edit(
        action_type="entitlement_change",
        base_payload=base,
        edited_payload={"target_concurrency": 48},
    )

    assert base["target"] == {"concurrency_limit": 40}
    assert revised["target"] == {"concurrency_limit": 48}
    assert {key: value for key, value in revised.items() if key != "target"} == {
        key: value for key, value in base.items() if key != "target"
    }
    assert revision_matches_approval_edit(
        action_type="entitlement_change",
        base_payload=base,
        revision_payload=revised,
    )


@pytest.mark.parametrize(
    "edited_payload",
    [
        {},
        {"resource_id": "sub_foreign", "unsupported_field": True},
        {"target_concurrency": True},
        {"target_concurrency": 0},
        {"target_concurrency": 1_000_001},
        {"target_concurrency": "48"},
        {"target_concurrency": 48, "refund_reason": "not allowed"},
    ],
)
def test_entitlement_edit_rejects_unknown_or_unsafe_fields(
    edited_payload: dict[str, object],
) -> None:
    with pytest.raises(ApprovalEditNotAllowed, match="approval_edit_not_allowed"):
        apply_approval_edit(
            action_type="entitlement_change",
            base_payload=_entitlement_payload(),
            edited_payload=edited_payload,
        )


@pytest.mark.parametrize(
    "tamper",
    [
        {"subscription_id": "sub_foreign"},
        {"business_version": 4},
        {"target": {"rpm_limit": 48}},
        {"change_type": "plan_change"},
    ],
)
def test_entitlement_revision_contract_rejects_immutable_field_changes(
    tamper: dict[str, object],
) -> None:
    base = _entitlement_payload()
    revised = apply_approval_edit(
        action_type="entitlement_change",
        base_payload=base,
        edited_payload={"target_concurrency": 48},
    )
    revised.update(tamper)

    assert not revision_matches_approval_edit(
        action_type="entitlement_change",
        base_payload=base,
        revision_payload=revised,
    )


def test_refund_edit_remains_a_single_allowlisted_field() -> None:
    base = {
        "billing_record_id": "bill_demo",
        "customer_id": "cust_demo",
        "amount": "49.00",
        "currency": "USD",
        "business_version": 2,
        "refund_reason": "Duplicate charge.",
    }

    revised = apply_approval_edit(
        action_type="refund",
        base_payload=base,
        edited_payload={"refund_reason": "Duplicate billing lineage verified."},
    )

    assert revised["refund_reason"] == "Duplicate billing lineage verified."
    assert revision_matches_approval_edit(
        action_type="refund",
        base_payload=base,
        revision_payload=revised,
    )
