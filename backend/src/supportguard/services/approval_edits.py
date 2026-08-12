from __future__ import annotations

from typing import Any


class ApprovalEditNotAllowed(ValueError):
    """The requested revision changes fields outside the action allowlist."""


MAX_TARGET_CONCURRENCY = 1_000_000


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalEditNotAllowed("approval_edit_not_allowed")
    return value.strip()


def _required_target_concurrency(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TARGET_CONCURRENCY
    ):
        raise ApprovalEditNotAllowed("approval_edit_not_allowed")
    return value


def apply_approval_edit(
    *,
    action_type: str,
    base_payload: dict[str, Any],
    edited_payload: dict[str, object],
) -> dict[str, Any]:
    """Create one immutable action revision from an action-specific allowlist."""

    payload = dict(base_payload)
    if action_type == "refund":
        if set(edited_payload) != {"refund_reason"}:
            raise ApprovalEditNotAllowed("approval_edit_not_allowed")
        payload["refund_reason"] = _required_text(edited_payload["refund_reason"])
        return payload

    if action_type == "entitlement_change":
        if set(edited_payload) != {"target_concurrency"}:
            raise ApprovalEditNotAllowed("approval_edit_not_allowed")
        target = base_payload.get("target")
        if (
            base_payload.get("change_type") != "quota_change"
            or not isinstance(target, dict)
            or set(target) != {"concurrency_limit"}
        ):
            raise ApprovalEditNotAllowed("approval_edit_not_allowed")
        payload["target"] = {
            "concurrency_limit": _required_target_concurrency(
                edited_payload["target_concurrency"]
            )
        }
        return payload

    raise ApprovalEditNotAllowed("approval_edit_not_allowed")


def revision_matches_approval_edit(
    *,
    action_type: str,
    base_payload: dict[str, Any],
    revision_payload: dict[str, Any],
) -> bool:
    """Verify that a persisted revision differs only by one legal edit."""

    try:
        if action_type == "refund":
            edited_payload: dict[str, object] = {
                "refund_reason": revision_payload.get("refund_reason")
            }
        elif action_type == "entitlement_change":
            target = revision_payload.get("target")
            if not isinstance(target, dict):
                return False
            edited_payload = {"target_concurrency": target.get("concurrency_limit")}
        else:
            return False
        expected = apply_approval_edit(
            action_type=action_type,
            base_payload=base_payload,
            edited_payload=edited_payload,
        )
    except ApprovalEditNotAllowed:
        return False
    return expected == revision_payload
