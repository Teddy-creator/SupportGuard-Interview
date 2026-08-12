from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ApprovalReaderIdentity(BaseModel):
    """Version-tolerant Approval identity exposed by the compatible reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_type: str | None
    resource_id: str | None
    origin_turn_id: str | None
    identity_source: Literal["persisted", "proposal_compat", "unavailable"]
    identity_complete: bool


_RESOURCE_TYPES = {
    "refund": "billing_record_id",
    "api_key_revocation": "api_key_id",
    "entitlement_change": "subscription_id",
}


def approval_reader_identity(payload: dict[str, Any]) -> ApprovalReaderIdentity:
    """Normalize b179 and v1.5.12 Approval projections without probing new columns.

    b180+ projections include all three persisted identity fields.  The b179
    projection predates them, but its bound Proposal remains an authoritative
    source for the resource id.  It cannot prove the originating Turn, so the
    fallback deliberately stays incomplete.
    """

    resource_type = payload.get("resource_type")
    resource_id = payload.get("resource_id")
    origin_turn_id = payload.get("origin_turn_id")
    if all(
        isinstance(value, str) and bool(value.strip())
        for value in (resource_type, resource_id, origin_turn_id)
    ):
        return ApprovalReaderIdentity(
            resource_type=str(resource_type),
            resource_id=str(resource_id),
            origin_turn_id=str(origin_turn_id),
            identity_source="persisted",
            identity_complete=True,
        )

    proposal = payload.get("proposal")
    proposal_resource_id = (
        proposal.get("resource_id") if isinstance(proposal, dict) else None
    )
    action_type = payload.get("action_type")
    compat_resource_type = (
        _RESOURCE_TYPES.get(action_type) if isinstance(action_type, str) else None
    )
    if (
        compat_resource_type is not None
        and isinstance(proposal_resource_id, str)
        and proposal_resource_id.strip()
    ):
        return ApprovalReaderIdentity(
            resource_type=compat_resource_type,
            resource_id=proposal_resource_id,
            origin_turn_id=None,
            identity_source="proposal_compat",
            identity_complete=False,
        )
    return ApprovalReaderIdentity(
        resource_type=None,
        resource_id=None,
        origin_turn_id=None,
        identity_source="unavailable",
        identity_complete=False,
    )


def attach_approval_reader_identity(payload: dict[str, Any]) -> dict[str, Any]:
    payload["resource_identity"] = approval_reader_identity(payload).model_dump()
    return payload
