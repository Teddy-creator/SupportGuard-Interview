"""Historical adapter for the current pure Policy owner.

Old checkpoints and characterization tests may still name routes that the
Interview Edition no longer exposes.  The adapter keeps those values parseable
without making them members of the current ``agent.policy.PolicyRoute``.
"""

from enum import StrEnum

from supportguard.agent.policy import (
    PolicyInput,
    PublicationDecision,
    evaluate_policy,
)
from supportguard.agent.policy import (
    decide_policy as _decide_policy,
)
from supportguard.agent.schemas import CandidateResponse


class PolicyRoute(StrEnum):
    ANSWER = "answer"
    SAFE_ACTION = "safe_action"
    AWAIT_APPROVAL = "await_human_approval"
    REJECT = "reject"
    MANUAL_TAKEOVER = "manual_takeover"


PolicyDecision = PublicationDecision


def decide_policy(
    candidate: CandidateResponse,
    *,
    evidence_conflict: bool,
    citation_integrity: bool = True,
    proposal_eligible: bool | None = None,
) -> PolicyRoute:
    """Return a legacy enum while delegating all current judgment."""

    return PolicyRoute(
        _decide_policy(
            candidate,
            evidence_conflict=evidence_conflict,
            citation_integrity=citation_integrity,
            proposal_eligible=proposal_eligible,
        ).value
    )


__all__ = [
    "PolicyDecision",
    "PolicyInput",
    "PublicationDecision",
    "PolicyRoute",
    "decide_policy",
    "evaluate_policy",
]
