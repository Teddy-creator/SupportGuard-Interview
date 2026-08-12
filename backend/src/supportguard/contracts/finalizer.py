from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supportguard.contracts.canonical_json import canonical_json_hash


def canonical_hash(value: object) -> str:
    return canonical_json_hash(value)


FINALIZER_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        "resolved",
        "failed",
        "rejected",
        "needs_clarification",
        "verification_pending",
        "manual_takeover",
    }
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateDelta(FrozenContract):
    state: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_embedded_final_terminal(self) -> StateDelta:
        if "final" not in self.state:
            return self
        final = self.state["final"]
        if not isinstance(final, dict):
            raise ValueError("finalizer final state must be an object")
        terminal = final.get("terminal_state")
        if not isinstance(terminal, str) or terminal not in FINALIZER_TERMINAL_STATES:
            raise ValueError("finalizer terminal state is missing or unsupported")
        return self


class FinalizerHeadsV2(FrozenContract):
    expected_ticket_head_event_id: str | None
    expected_ticket_sequence: int = Field(ge=0)
    expected_ticket_event_hash: str | None
    expected_run_status: str
    expected_run_status_version: int = Field(ge=1)
    parent_checkpoint_id: str | None
    parent_checkpoint_hash: str | None
    parent_checkpoint_version: int = Field(ge=0)
    final_checkpoint_id: str
    final_checkpoint_hash: str
    final_checkpoint_version: int = Field(ge=1)
    expected_marker_status: Literal["checkpoint_written"] = "checkpoint_written"
    expected_marker_status_version: int = Field(ge=2)
    expected_tool_ledger_head: str
    expected_capability_ledger_head: str
    expected_proposal_ledger_head: str
    expected_budget_ledger_head: str
    expected_context_snapshot_hash: str
    expected_context_ledger_hash: str
    expected_domain_resource_versions: dict[str, int]


class AgentCompleteDelta(FrozenContract):
    variant: Literal["agent_complete"] = "agent_complete"
    outcome: Literal["completed"] = "completed"


class HitlInterruptDelta(FrozenContract):
    variant: Literal["hitl_interrupt"] = "hitl_interrupt"
    outcome: Literal["interrupted"] = "interrupted"
    proposal_id: str
    proposal_hash: str
    approval_snapshot_hash: str
    observation_binding_hash: str


class ActionfulApprovalResumeDelta(FrozenContract):
    """Read-only compatibility contract for effects committed by an old worker.

    New workers never construct this variant before the Segment finalizer owns
    the effect transaction.  It remains parseable so an already-durable v2
    checkpoint can be recovered without replaying its external action.
    """

    variant: Literal["approval_actionful"] = "approval_actionful"
    outcome: Literal["completed"] = "completed"
    approval_id: str
    human_decision_id: str
    decision: Literal["approve", "edit_and_approve"]
    action_hash: str
    business_action_id: str
    effect_hash: str


class ActionIntentApprovalResumeDelta(FrozenContract):
    """A fenced intent; it cannot assert that a business effect already exists."""

    variant: Literal["approval_action_intent"] = "approval_action_intent"
    outcome: Literal["completed"] = "completed"
    approval_id: str
    human_decision_id: str
    decision: Literal["approve", "edit_and_approve"]
    action_hash: str
    execution_intent: Literal["execute_runtime_action"] = "execute_runtime_action"
    expected_approval_status: Literal["approved", "executed"]


class NoActionApprovalResumeDelta(FrozenContract):
    variant: Literal["approval_no_action"] = "approval_no_action"
    outcome: Literal["completed"] = "completed"
    approval_id: str
    human_decision_id: str
    decision: Literal["reject", "manual_takeover"]
    no_action_reason: str


class FailClosedApprovalResumeDelta(FrozenContract):
    variant: Literal["approval_fail_closed"] = "approval_fail_closed"
    outcome: Literal["completed"] = "completed"
    approval_id: str
    human_decision_id: str
    decision: Literal["approve", "edit_and_approve"]
    domain_outcome_reason: Literal["binding_stale", "logical_degradation"]
    validation_result: str
    reconciliation_event_id: str | None = None
    reconciliation_event_hash: str | None = None

    @model_validator(mode="after")
    def reconciliation_is_all_or_nothing(self) -> FailClosedApprovalResumeDelta:
        if (self.reconciliation_event_id is None) != (self.reconciliation_event_hash is None):
            raise ValueError("reconciliation event id and hash must be supplied together")
        return self


DomainDeltaV2 = Annotated[
    AgentCompleteDelta
    | HitlInterruptDelta
    | ActionfulApprovalResumeDelta
    | ActionIntentApprovalResumeDelta
    | NoActionApprovalResumeDelta
    | FailClosedApprovalResumeDelta,
    Field(discriminator="variant"),
]


class FinalizerPayloadV2(FrozenContract):
    schema_version: Literal["finalizer.v2"] = "finalizer.v2"
    canonicalization_version: Literal["canonical-json.v1"] = "canonical-json.v1"
    tenant_id: str
    ticket_id: str
    run_id: str
    job_id: str
    segment_id: str
    delivery_generation: int = Field(ge=1, le=5)
    fencing_token: int = Field(ge=1)
    parent_segment_id: str | None
    marker_id: str
    segment_kind: Literal["agent_start", "approval_resume"]
    prepared_payload_hash: str
    expected_heads: FinalizerHeadsV2
    state_delta: StateDelta
    state_delta_ref: str
    state_delta_hash: str
    domain_delta: DomainDeltaV2
    domain_delta_ref: str
    domain_delta_hash: str
    payload_hash: str

    @classmethod
    def build(
        cls,
        *,
        tenant_id: str,
        ticket_id: str,
        run_id: str,
        job_id: str,
        segment_id: str,
        delivery_generation: int,
        fencing_token: int,
        parent_segment_id: str | None,
        marker_id: str,
        segment_kind: Literal["agent_start", "approval_resume"],
        prepared_payload_hash: str,
        expected_heads: FinalizerHeadsV2,
        state: dict[str, Any],
        domain_delta: DomainDeltaV2,
    ) -> FinalizerPayloadV2:
        state_delta = StateDelta(state=state)
        state_json = state_delta.model_dump(mode="json")
        domain_json = domain_delta.model_dump(mode="json")
        body = {
            "schema_version": "finalizer.v2",
            "canonicalization_version": "canonical-json.v1",
            "tenant_id": tenant_id,
            "ticket_id": ticket_id,
            "run_id": run_id,
            "job_id": job_id,
            "segment_id": segment_id,
            "delivery_generation": delivery_generation,
            "fencing_token": fencing_token,
            "parent_segment_id": parent_segment_id,
            "marker_id": marker_id,
            "segment_kind": segment_kind,
            "prepared_payload_hash": prepared_payload_hash,
            "expected_heads": expected_heads.model_dump(mode="json"),
            "state_delta": state_json,
            "state_delta_ref": f"segment:{segment_id}:state",
            "state_delta_hash": canonical_hash(state_json),
            "domain_delta": domain_json,
            "domain_delta_ref": f"segment:{segment_id}:domain",
            "domain_delta_hash": canonical_hash(domain_json),
        }
        return cls.model_validate({**body, "payload_hash": canonical_hash(body)})

    def verify(self) -> None:
        state_json = self.state_delta.model_dump(mode="json")
        domain_json = self.domain_delta.model_dump(mode="json")
        if canonical_hash(state_json) != self.state_delta_hash:
            raise ValueError("finalizer state delta hash mismatch")
        if canonical_hash(domain_json) != self.domain_delta_hash:
            raise ValueError("finalizer domain delta hash mismatch")
        body = self.model_dump(mode="json", exclude={"payload_hash"})
        if canonical_hash(body) != self.payload_hash:
            raise ValueError("finalizer payload hash mismatch")


def finalizer_payload_mismatch_paths(
    persisted: FinalizerPayloadV2,
    rebuilt: FinalizerPayloadV2,
) -> tuple[str, ...]:
    """Return bounded field paths that changed without exposing payload values."""

    mismatches: list[str] = []
    max_paths = 32

    def compare(left: Any, right: Any, *, path: str) -> None:
        if len(mismatches) >= max_paths:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child_path = f"{path}.{key}" if path else str(key)
                if key not in left or key not in right:
                    mismatches.append(child_path)
                    if len(mismatches) >= max_paths:
                        return
                    continue
                compare(left[key], right[key], path=child_path)
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                mismatches.append(f"{path}.length")
                if len(mismatches) >= max_paths:
                    return
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
                compare(left_item, right_item, path=f"{path}[{index}]")
                if len(mismatches) >= max_paths:
                    return
            return
        if type(left) is not type(right) or left != right:
            mismatches.append(path or "$")

    compare(
        persisted.model_dump(mode="json", exclude={"payload_hash"}),
        rebuilt.model_dump(mode="json", exclude={"payload_hash"}),
        path="",
    )
    return tuple(mismatches)
