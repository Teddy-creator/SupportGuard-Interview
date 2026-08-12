from __future__ import annotations

from typing import Any

from supportguard.contracts.canonical_json import canonical_json_hash


def approval_snapshot_payload(
    *,
    approval_id: str,
    proposal_id: str,
    tenant_id: str,
    run_id: str,
    origin_job_id: str,
    origin_marker_id: str,
    origin_fencing_token: int,
    action_type: str,
    action_payload: dict[str, Any],
    action_hash: str,
    resource_version: int,
    policy_binding: dict[str, Any],
    citation_binding_refs: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "approval-snapshot.v2",
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "origin": {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "job_id": origin_job_id,
            "marker_id": origin_marker_id,
            "fencing_token": origin_fencing_token,
        },
        "action_type": action_type,
        "action_payload": action_payload,
        "action_hash": action_hash,
        "resource_version": resource_version,
        "policy_binding": policy_binding,
        "citation_binding_refs": citation_binding_refs,
    }


def approval_snapshot_hash(payload: dict[str, Any]) -> str:
    return canonical_json_hash(payload)
