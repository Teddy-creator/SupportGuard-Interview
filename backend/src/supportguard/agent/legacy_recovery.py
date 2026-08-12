from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supportguard.actions.service import get_action_spec
from supportguard.agent.obligations import (
    ActionObligationLedger,
    evaluate_action_obligations,
)
from supportguard.contracts.action_preconditions import (
    ActionAdmission,
    ActionAdmissionV2,
    resolve_action_admission_v2,
)


@dataclass(frozen=True)
class LegacyAdmissionRecovery:
    admission: ActionAdmissionV2 | None
    ledger: ActionObligationLedger | None
    reason_code: str

    @property
    def recovered(self) -> bool:
        return self.admission is not None and self.ledger is not None


def _resource_binding_is_unique(
    *,
    admission: ActionAdmissionV2,
    ledger: ActionObligationLedger,
    observations: Sequence[dict[str, Any]],
    run_id: str,
) -> bool:
    """Reject legacy recovery when current observations admit competing bindings."""

    spec = get_action_spec(admission.action_type)  # type: ignore[arg-type]
    for obligation in spec.obligations:
        if obligation.kind == "knowledge" or obligation.observed_resource_field is None:
            continue
        entry = next(
            (item for item in ledger.obligations if item.obligation_id == obligation.obligation_id),
            None,
        )
        if entry is None or entry.binding is None or not entry.binding.resource_id:
            return False
        observed_resources: set[str] = set()
        for observation in observations:
            if (
                observation.get("run_id") != run_id
                or observation.get("tool_name") not in obligation.capabilities
                or observation.get("status") != "ok"
            ):
                continue
            scope = observation.get("trusted_scope")
            if not isinstance(scope, dict) or (
                scope.get("tenant_id") != admission.tenant_id
                or scope.get("customer_id") != admission.customer_id
                or scope.get("scope_hash") != admission.scope_hash
            ):
                return False
            data = observation.get("data")
            if not isinstance(data, dict):
                return False
            resource_id = str(data.get(obligation.observed_resource_field or "") or "")
            if resource_id:
                observed_resources.add(resource_id.casefold())
        if observed_resources != {entry.binding.resource_id.casefold()}:
            return False
    return True


def recover_legacy_action_admission(
    *,
    legacy: ActionAdmission,
    redacted_message: str,
    classification: Mapping[str, Any],
    tenant_id: str,
    customer_id: str,
    current_message_id: str,
    turn_group_id: str,
    observations: Sequence[dict[str, Any]],
    run_id: str,
    now: datetime | None = None,
) -> LegacyAdmissionRecovery:
    """Re-prove a v1 admission without trusting its missing provenance.

    The old payload can identify the recovery lane, but only current accepted
    message text, classification, authenticated scope, and current-run
    observations can authorize a v2 admission.
    """

    requested_action = str(classification.get("requested_action") or "none")
    issue_type = str(classification.get("issue_type") or "unknown")
    if (
        requested_action != legacy.action_type
        or issue_type != legacy.issue_type
        or not redacted_message.strip()
        or not current_message_id
        or not turn_group_id
    ):
        return LegacyAdmissionRecovery(None, None, "legacy_admission_context_mismatch")
    admission = resolve_action_admission_v2(
        redacted_message,
        (),
        requested_action=requested_action,
        issue_type=issue_type,
        tenant_id=tenant_id,
        customer_id=customer_id,
        current_message_id=current_message_id,
        turn_group_id=turn_group_id,
        classification_version=str(classification.get("schema_version") or "classification.v1"),
        requested_concurrency_limit=(
            int(classification["requested_concurrency_limit"])
            if isinstance(classification.get("requested_concurrency_limit"), int)
            else None
        ),
    )
    if (
        admission.status != "admitted"
        or admission.action_type != legacy.action_type
        or admission.issue_type != legacy.issue_type
    ):
        return LegacyAdmissionRecovery(None, None, "legacy_admission_not_reprovable")
    if any(
        admission.extracted_arguments.get(key) != value
        for key, value in legacy.extracted_arguments.items()
    ):
        return LegacyAdmissionRecovery(None, None, "legacy_admission_argument_mismatch")
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec(legacy.action_type),
        admission=admission,
        observations=observations,
        run_id=run_id,
        now=now,
    )
    if not ledger.all_reads_qualified:
        return LegacyAdmissionRecovery(None, None, "legacy_observation_binding_unproven")
    if not _resource_binding_is_unique(
        admission=admission,
        ledger=ledger,
        observations=observations,
        run_id=run_id,
    ):
        return LegacyAdmissionRecovery(None, None, "legacy_resource_binding_ambiguous")
    return LegacyAdmissionRecovery(admission, ledger, "legacy_admission_rehydrated")
