"""Compatibility facade for the shared action contract owner.

New code imports :mod:`supportguard.actions.service`.  This module preserves
the historical Agent-layer import path without creating a second registry.
"""

from supportguard.actions.service import (
    ACTION_SPECS,
    ActionErrorCodes,
    ActionIssueType,
    ActionSpec,
    ApiKeyRevocationProposalArguments,
    EntitlementChangeProposalArguments,
    EvidenceObligationSpec,
    ObligationKind,
    PolicyCapability,
    ProposalAction,
    ProposalFieldBinding,
    ProposalFieldSource,
    RefundProposalArguments,
    RuntimeEffectCapability,
    TerminalOutcomeClass,
    TerminalOutcomeMessageKey,
    TerminalOutcomePredicate,
    TerminalOutcomeRule,
    get_action_spec,
    get_action_spec_by_policy_capability,
    get_action_spec_by_proposal,
    get_action_spec_by_runtime_effect_capability,
    get_action_spec_or_none,
)

__all__ = [
    "ACTION_SPECS",
    "ActionErrorCodes",
    "ActionIssueType",
    "ActionSpec",
    "ApiKeyRevocationProposalArguments",
    "EntitlementChangeProposalArguments",
    "EvidenceObligationSpec",
    "ObligationKind",
    "PolicyCapability",
    "ProposalAction",
    "ProposalFieldBinding",
    "ProposalFieldSource",
    "RefundProposalArguments",
    "RuntimeEffectCapability",
    "TerminalOutcomeClass",
    "TerminalOutcomeMessageKey",
    "TerminalOutcomePredicate",
    "TerminalOutcomeRule",
    "get_action_spec",
    "get_action_spec_by_policy_capability",
    "get_action_spec_by_proposal",
    "get_action_spec_by_runtime_effect_capability",
    "get_action_spec_or_none",
]
