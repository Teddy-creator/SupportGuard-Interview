from __future__ import annotations

from typing import Any

EVIDENCE_PROJECTION_V1 = "context-evidence.v1"
EVIDENCE_PROJECTION_V2 = "context-evidence.v2"


def project_context_evidence(
    evidence: dict[str, Any],
    *,
    citation_binding_id: str | None = None,
    projection_version: str = EVIDENCE_PROJECTION_V1,
) -> dict[str, Any]:
    """Project evidence using an explicitly replayable wire contract.

    Version 1 remains the default for historical ledgers created before the
    projection version was recorded.  New Provider requests use version 2,
    which keeps the chunk-boundary locator audit-only and exposes only the
    supporting-span locator as model-selectable claim support.
    """

    if projection_version not in {EVIDENCE_PROJECTION_V1, EVIDENCE_PROJECTION_V2}:
        raise ValueError("unsupported evidence projection version")

    projected = {
        key: evidence.get(key)
        for key in (
            "evidence_id",
            "document_id",
            "chunk_id",
            "title",
            "section_path",
            "version",
            "effective_at",
            "index_version",
            "content_hash",
            "supporting_span",
            "supporting_span_eligible",
            "supporting_span_reason",
            "retrieval_score",
            "evidence_group",
        )
    }
    locator_names = (
        ("source_locator", "chunk_locator")
        if projection_version == EVIDENCE_PROJECTION_V1
        else ("source_locator",)
    )
    for source_name in locator_names:
        locator = evidence.get(source_name)
        if isinstance(locator, dict):
            projected[f"{source_name}_hash"] = locator.get("locator_hash")
    eligibility = evidence.get("eligibility_envelope")
    if isinstance(eligibility, dict):
        projected["eligibility"] = {
            key: eligibility.get(key)
            for key in (
                "corpus_snapshot_id",
                "index_version",
                "filter_hash",
                "outcome",
                "reason_code",
            )
        }
    if citation_binding_id is not None:
        projected["citation_binding_id"] = citation_binding_id
    return projected
