from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from supportguard.memory.service import MAX_ATTEMPTED_ACTIONS, merge_attempted_actions


def _projection(
    *,
    approval_id: str = "approval_1",
    action_type: str = "refund",
    resource_type: str = "billing_record",
    resource_id: str = "bill_1",
    projection_status: str = "pending",
    approval_status: str = "pending",
    execution_state: str = "not_started",
    status_version: int = 1,
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "conversation-action-state.v1",
        "approval_id": approval_id,
        "origin_run_id": "run_1",
        "origin_turn_id": "turn_1",
        "action_type": action_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "resource_version": 2,
        "approval_status": approval_status,
        "projection_status": projection_status,
        "status_version": status_version,
        "actionable": projection_status == "pending",
        "allowed_customer_actions": (
            ["withdraw"] if projection_status == "pending" else []
        ),
        "decision_class": "none",
        "customer_safe_reason_code": {
            "pending": "approval_pending",
            "approved": "approval_approved_awaiting_execution",
            "executed": "action_execution_confirmed",
        }[projection_status],
        "execution_state": execution_state,
        "business_action_id": None,
        "updated_at": updated_at or datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
        "source_event_id": None,
        "source_event_hash": None,
        "grants_action_authority": False,
    }


def test_plain_follow_up_does_not_destroy_existing_action_history() -> None:
    existing = merge_attempted_actions([], [_projection()])

    persisted = merge_attempted_actions(existing, [])

    assert persisted == existing
    assert persisted[0]["projection_status"] == "pending"
    assert persisted[0]["historical"] is True
    assert persisted[0]["grants_action_authority"] is False


def test_same_action_identity_is_superseded_by_newer_projection() -> None:
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    pending = merge_attempted_actions(
        [],
        [_projection(updated_at=started)],
    )

    executed = merge_attempted_actions(
        pending,
        [
            _projection(
                approval_status="executed",
                projection_status="executed",
                execution_state="succeeded",
                status_version=2,
                updated_at=started + timedelta(minutes=1),
            )
        ],
    )

    assert len(executed) == 1
    assert executed[0]["projection_status"] == "executed"
    assert executed[0]["status_version"] == 2


def test_distinct_action_identities_are_preserved() -> None:
    actions = merge_attempted_actions(
        [],
        [
            _projection(),
            _projection(
                approval_id="approval_2",
                action_type="api_key_revocation",
                resource_type="api_key",
                resource_id="key_1",
            ),
        ],
    )

    assert len(actions) == 2
    assert {
        (
            item["action_identity"]["action_type"],
            item["action_identity"]["resource_id"],
        )
        for item in actions
    } == {("refund", "bill_1"), ("api_key_revocation", "key_1")}


def test_same_approval_cannot_regress_from_terminal_to_stale_checkpoint() -> None:
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    executed = merge_attempted_actions(
        [],
        [
            _projection(
                approval_status="executed",
                projection_status="executed",
                execution_state="succeeded",
                status_version=3,
                updated_at=started,
            )
        ],
    )

    regressed = merge_attempted_actions(
        executed,
        [
            _projection(
                approval_status="pending",
                projection_status="pending",
                execution_state="not_started",
                status_version=1,
                updated_at=started + timedelta(minutes=1),
            )
        ],
    )

    assert len(regressed) == 1
    assert regressed[0]["projection_status"] == "executed"
    assert regressed[0]["status_version"] == 3


def test_new_approval_generation_supersedes_old_terminal_for_same_resource() -> None:
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    executed = merge_attempted_actions(
        [],
        [
            _projection(
                approval_id="approval_old",
                approval_status="executed",
                projection_status="executed",
                execution_state="succeeded",
                status_version=4,
                updated_at=started,
            )
        ],
    )

    current = merge_attempted_actions(
        executed,
        [
            _projection(
                approval_id="approval_new",
                status_version=1,
                updated_at=started + timedelta(minutes=1),
            )
        ],
    )

    assert len(current) == 1
    assert current[0]["approval_id"] == "approval_new"
    assert current[0]["projection_status"] == "pending"


def test_timestamp_comparison_normalizes_naive_and_offset_history() -> None:
    baseline = merge_attempted_actions(
        [],
        [
            _projection(
                status_version=2,
                updated_at=datetime(2026, 7, 28, 8, 30, tzinfo=UTC),
            )
        ],
    )
    baseline[0]["observed_at"] = "2026-07-28T16:30:00+08:00"

    older_naive = merge_attempted_actions(
        baseline,
        [
            _projection(
                approval_id="approval_older_generation",
                status_version=1,
                updated_at=datetime(2026, 7, 28, 8, 15),
            )
        ],
    )
    newer_naive = merge_attempted_actions(
        older_naive,
        [
            _projection(
                approval_id="approval_newer_generation",
                status_version=1,
                updated_at=datetime(2026, 7, 28, 8, 45),
            )
        ],
    )

    assert older_naive[0]["approval_id"] == "approval_1"
    assert newer_naive[0]["approval_id"] == "approval_newer_generation"
    assert newer_naive[0]["observed_at"].endswith("+00:00")


def test_attempted_action_memory_is_bounded_and_prioritizes_active_and_latest() -> None:
    origin = datetime(2026, 7, 1, tzinfo=UTC)
    projections = [
        _projection(
            approval_id=f"approval_terminal_{index:03d}",
            resource_id=f"bill_terminal_{index:03d}",
            approval_status="executed",
            projection_status="executed",
            execution_state="succeeded",
            status_version=2,
            updated_at=origin + timedelta(minutes=index + 1),
        )
        for index in range(MAX_ATTEMPTED_ACTIONS + 6)
    ]
    projections.append(
        _projection(
            approval_id="approval_active_old",
            resource_id="bill_active_old",
            updated_at=origin,
        )
    )

    actions = merge_attempted_actions([], projections)

    assert len(actions) == MAX_ATTEMPTED_ACTIONS
    retained_ids = {
        item["action_identity"]["resource_id"] for item in actions
    }
    assert "bill_active_old" in retained_ids
    assert f"bill_terminal_{MAX_ATTEMPTED_ACTIONS + 5:03d}" in retained_ids
    assert all(item["grants_action_authority"] is False for item in actions)


def test_invalid_current_projection_fails_closed() -> None:
    invalid = _projection()
    invalid["grants_action_authority"] = True

    with pytest.raises(ValidationError):
        merge_attempted_actions([], [invalid])


def test_legacy_history_is_retained_but_marked_non_authoritative() -> None:
    legacy = {
        "action": "answer",
        "proposal": {
            "proposal_id": "proposal_legacy",
            "raw_payload": "must not survive",
        },
    }

    actions = merge_attempted_actions([legacy, {"raw_error": "secret"}], [])

    assert actions == [
        {
            "schema_version": "attempted-action-memory-legacy.v1",
            "legacy_record_present": True,
            "historical": True,
            "grants_action_authority": False,
        }
    ]
