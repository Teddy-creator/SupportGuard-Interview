"""Deterministic publication closure for version-comparison answers."""

from __future__ import annotations

import re
from typing import Any

from supportguard.agent.evidence import (
    comparison_transition_claim,
    comparison_transition_markers,
)
from supportguard.agent.schemas import CandidateCitation, CandidateResponse, MaterialClaim


def _exact_context_evidence(
    details: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve one attempt-local binding to one exact eligible evidence identity."""

    matches = [
        item
        for item in evidence
        if item.get("supporting_span_eligible") is True
        and item.get("chunk_id") == details.get("chunk_id")
        and item.get("document_id") == details.get("document_id")
        and item.get("version") == details.get("version")
        and item.get("content_hash") == details.get("content_hash")
        and item.get("source_locator", {}).get("locator_hash") == details.get("locator_hash")
        and (
            not details.get("evidence_id")
            or item.get("evidence_id") == details.get("evidence_id")
        )
        and (
            not details.get("evidence_group")
            or (item.get("evidence_group") or "current") == details.get("evidence_group")
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _transition_supports_markers(
    evidence: dict[str, Any],
    markers: list[str],
) -> bool:
    if comparison_transition_claim([evidence]) is None:
        return False
    compact_span = re.sub(r"\s+", "", str(evidence.get("supporting_span") or "")).casefold()
    return all(marker in compact_span for marker in markers)


def _mentions_transition(evidence: dict[str, Any], markers: list[str]) -> bool:
    compact_span = re.sub(r"\s+", "", str(evidence.get("supporting_span") or "")).casefold()
    return any(marker in compact_span for marker in markers)


def _augment_transition_claims(
    claims: list[MaterialClaim],
    *,
    additions: list[tuple[str, dict[str, Any]]],
    markers: list[str],
) -> list[MaterialClaim]:
    claim_additions = [
        (binding_id, str(item.get("source_locator", {}).get("locator_hash") or ""))
        for binding_id, item in additions
        if _transition_supports_markers(item, markers)
    ]
    updated: list[MaterialClaim] = []
    for claim in claims:
        compact_claim = re.sub(r"\s+", "", claim.text).casefold()
        if not all(marker in compact_claim for marker in markers):
            updated.append(claim)
            continue
        updated.append(
            claim.model_copy(
                update={
                    "citation_binding_ids": list(
                        dict.fromkeys(
                            [
                                *claim.citation_binding_ids,
                                *(binding_id for binding_id, _ in claim_additions),
                            ]
                        )
                    ),
                    "knowledge_locator_hashes": list(
                        dict.fromkeys(
                            [
                                *claim.knowledge_locator_hashes,
                                *(
                                    locator_hash
                                    for _, locator_hash in claim_additions
                                    if len(locator_hash) == 64
                                ),
                            ]
                        )
                    ),
                }
            )
        )
    return updated


def canonicalize_comparison_citation_groups(
    candidate: CandidateResponse,
    *,
    evidence: list[dict[str, Any]],
    binding_map: dict[str, dict[str, Any]],
    comparison_complete: bool,
    evidence_replan_count: int,
) -> tuple[CandidateResponse, int]:
    """Close a bounded comparison over exact current-attempt bindings.

    A Provider rewrite can cover the evidence-derived transition while selecting
    only one comparison group. After the one allowed evidence replan, Runtime
    may add the missing group only when the current context already contains
    exact eligible bindings for both groups and a bound span carries the full
    material transition. Conversation history never participates.
    """

    if candidate.action != "answer" or not comparison_complete or evidence_replan_count < 1:
        return candidate, 0
    markers = comparison_transition_markers(evidence)
    if not markers:
        return candidate, 0

    eligible_bindings = [
        (str(binding_id), matched)
        for binding_id, details in binding_map.items()
        if isinstance(details, dict)
        and (matched := _exact_context_evidence(details, evidence)) is not None
    ]
    if not any(_transition_supports_markers(item, markers) for _, item in eligible_bindings):
        return candidate, 0

    existing_ids = {item.citation_binding_id for item in candidate.knowledge_citations}
    existing_groups = {
        str(item.get("evidence_group") or "current")
        for binding_id, item in eligible_bindings
        if binding_id in existing_ids
    }
    additions: list[tuple[str, dict[str, Any]]] = []
    for group in ("current", "historical"):
        if group in existing_groups:
            continue
        selected = next(
            (
                item
                for item in eligible_bindings
                if str(item[1].get("evidence_group") or "current") == group
                and _mentions_transition(item[1], markers)
            ),
            None,
        )
        if selected is None:
            return candidate, 0
        additions.append(selected)
    if not additions:
        return candidate, 0
    if not {"current", "historical"} <= existing_groups | {
        str(item.get("evidence_group") or "current") for _, item in additions
    }:
        return candidate, 0

    added_ids = [binding_id for binding_id, _ in additions]
    added_chunks = [str(item["chunk_id"]) for _, item in additions if item.get("chunk_id")]
    return (
        candidate.model_copy(
            update={
                "knowledge_citations": [
                    *candidate.knowledge_citations,
                    *(CandidateCitation(citation_binding_id=item) for item in added_ids),
                ],
                "knowledge_chunk_ids": list(
                    dict.fromkeys([*candidate.knowledge_chunk_ids, *added_chunks])
                ),
                "material_claims": _augment_transition_claims(
                    candidate.material_claims,
                    additions=additions,
                    markers=markers,
                ),
            }
        ),
        len(added_ids),
    )
