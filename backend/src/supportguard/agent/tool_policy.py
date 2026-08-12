from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from supportguard.actions.service import ActionSpec
from supportguard.agent.obligations import ActionObligationLedger
from supportguard.contracts.action_preconditions import ActionAdmissionV2
from supportguard.tools.gateway import ReadToolCall


def obligation_for_capability(
    action_spec: ActionSpec,
    capability: str,
) -> str | None:
    matches = [
        item.obligation_id for item in action_spec.obligations if capability in item.capabilities
    ]
    if len(matches) > 1:
        raise ValueError("one capability cannot own multiple action obligations")
    return matches[0] if matches else None


def semantic_invocation_key(
    *,
    action_spec: ActionSpec,
    admission: ActionAdmissionV2,
    ledger: ActionObligationLedger,
    call: ReadToolCall,
    index_snapshot: str | None = None,
) -> str:
    """Identify one semantic obligation without trusting query wording."""

    obligation_id = obligation_for_capability(action_spec, call.name)
    obligation = next(
        (item for item in action_spec.obligations if item.obligation_id == obligation_id),
        None,
    )
    ledger_entry = next(
        (item for item in ledger.obligations if item.obligation_id == obligation_id),
        None,
    )
    admitted_resource_ref = (
        admission.extracted_arguments.get(obligation.resource_ref_argument)
        if obligation is not None and obligation.resource_ref_argument is not None
        else None
    )
    payload = {
        "schema_version": "semantic-invocation.v1",
        "action_spec_version": action_spec.schema_version,
        "action_type": action_spec.action_type,
        "obligation_id": obligation_id,
        "capability": call.name,
        "tenant_id": admission.tenant_id,
        "customer_id": admission.customer_id,
        "scope_hash": admission.scope_hash,
        "admitted_resource_ref": admitted_resource_ref,
        "resolved_resource_id": (
            ledger_entry.binding.resource_id
            if ledger_entry is not None and ledger_entry.binding is not None
            else None
        ),
        "policy_family": obligation.policy_family if obligation is not None else None,
        "evidence_purpose": obligation.topic if obligation is not None else None,
        "index_snapshot": index_snapshot if call.name == "search_knowledge" else None,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def semantic_batch_rejections(
    *,
    action_spec: ActionSpec,
    ledger: ActionObligationLedger,
    calls: Sequence[ReadToolCall],
) -> dict[int, str]:
    """Reject only obligations qualified before this batch starts."""

    pending = set(ledger.unsatisfied_capabilities)
    rejected: dict[int, str] = {}
    for index, call in enumerate(calls):
        obligation_id = obligation_for_capability(action_spec, call.name)
        if obligation_id is not None and call.name not in pending:
            rejected[index] = "obligation_already_qualified"
    return rejected
