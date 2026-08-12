"""Current test-only classifier for the identity-bound action E2E carrier."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class IdentityConvergenceOutcome(StrEnum):
    CANDIDATE_NOT_PROPOSAL = "candidate_not_proposal"
    PROPOSAL_CAPABILITY_FAILED = "proposal_capability_failed"
    APPROVAL_PERSISTENCE_MISSING = "approval_persistence_missing"
    APPROVAL_IDENTITY_MISMATCH = "approval_identity_mismatch"
    APPROVAL_PROJECTION_MISSING = "approval_projection_missing"
    VISIBILITY_ORDERING_VIOLATION = "visibility_ordering_violation"
    FIXTURE_OR_ASSERTION_INVALID = "fixture_or_assertion_invalid"
    CONTRACT_PASS = "contract_pass"  # noqa: S105  # nosec B105
    DIAGNOSTIC_AMBIGUOUS = "diagnostic_ambiguous"


class IdentityConvergenceFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_valid: bool
    candidate_action: str | None = None
    expected_candidate_action: str
    policy_route: str | None = None
    capability_terminal_statuses: tuple[str, ...] = ()
    db_exact_a: int = Field(ge=0)
    db_same_run_a: int = Field(ge=0)
    db_same_tenant_a: int = Field(ge=0)
    db_exact_b: int = Field(ge=0)
    db_same_run_b: int = Field(ge=0)
    db_same_tenant_b: int = Field(ge=0)
    http_exact: int = Field(ge=0)
    http_total: int = Field(ge=0)
    approval_contract_valid: bool = False
    http_contract_valid: bool = False


def classify_identity_convergence(
    facts: IdentityConvergenceFacts,
) -> IdentityConvergenceOutcome:
    if (
        facts.db_exact_a > facts.db_same_run_a
        or facts.db_same_run_a > facts.db_same_tenant_a
        or facts.db_exact_b > facts.db_same_run_b
        or facts.db_same_run_b > facts.db_same_tenant_b
        or facts.http_exact > facts.http_total
        or facts.db_exact_a > 1
        or facts.db_exact_b > 1
        or facts.http_exact > 1
        or facts.db_exact_b < facts.db_exact_a
        or facts.db_same_run_b < facts.db_same_run_a
        or facts.db_same_tenant_b < facts.db_same_tenant_a
    ):
        return IdentityConvergenceOutcome.DIAGNOSTIC_AMBIGUOUS
    if not facts.fixture_valid:
        return IdentityConvergenceOutcome.FIXTURE_OR_ASSERTION_INVALID
    if (
        facts.candidate_action != facts.expected_candidate_action
        or facts.policy_route != "await_human_approval"
    ):
        return IdentityConvergenceOutcome.CANDIDATE_NOT_PROPOSAL
    capability_count = len(facts.capability_terminal_statuses)
    if capability_count > 1:
        return IdentityConvergenceOutcome.DIAGNOSTIC_AMBIGUOUS
    if capability_count != 1 or facts.capability_terminal_statuses[0] != "succeeded":
        return IdentityConvergenceOutcome.PROPOSAL_CAPABILITY_FAILED
    if facts.db_exact_a == 0:
        if facts.db_exact_b == 1:
            return IdentityConvergenceOutcome.VISIBILITY_ORDERING_VIOLATION
        if facts.db_same_run_b > 0:
            return IdentityConvergenceOutcome.APPROVAL_IDENTITY_MISMATCH
        return IdentityConvergenceOutcome.APPROVAL_PERSISTENCE_MISSING
    if not facts.approval_contract_valid:
        return IdentityConvergenceOutcome.APPROVAL_IDENTITY_MISMATCH
    if facts.http_exact == 0 or not facts.http_contract_valid:
        return IdentityConvergenceOutcome.APPROVAL_PROJECTION_MISSING
    return IdentityConvergenceOutcome.CONTRACT_PASS
