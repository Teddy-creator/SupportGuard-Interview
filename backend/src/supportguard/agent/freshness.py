from __future__ import annotations

from typing import Any

from supportguard.agent.evidence import observation_is_fresh
from supportguard.agent.schemas import CandidateResponse


def prune_stale_business_claims(
    candidate: CandidateResponse,
    *,
    observations: list[dict[str, Any]],
    citation_binding_map: dict[str, dict[str, Any]],
) -> tuple[CandidateResponse, int]:
    """Remove claims that depend on non-fresh business observations.

    Independently grounded knowledge and fresh business facts remain
    publishable. A later freshness disclaimer can never make a stale current
    claim safe.
    """

    source_to_observation: dict[str, dict[str, Any]] = {}
    for observation in observations:
        if observation.get("status") != "ok" or observation.get("tool_name") == "search_knowledge":
            continue
        for source in observation.get("source_refs", []):
            if isinstance(source, dict) and source.get("source_id"):
                source_to_observation[str(source["source_id"])] = observation
    publishable_source_ids = {
        source_id
        for source_id, observation in source_to_observation.items()
        if observation_is_fresh(observation)
    }
    kept_claims = [
        claim
        for claim in candidate.material_claims
        if set(claim.observation_source_ids) <= publishable_source_ids
    ]
    removed_count = len(candidate.material_claims) - len(kept_claims)
    if removed_count == 0:
        return candidate, 0

    kept_binding_ids = {
        binding_id for claim in kept_claims for binding_id in claim.citation_binding_ids
    }
    kept_business_source_ids = {
        source_id for claim in kept_claims for source_id in claim.observation_source_ids
    }
    kept_chunk_ids = {
        str(citation_binding_map[binding_id].get("chunk_id"))
        for binding_id in kept_binding_ids
        if binding_id in citation_binding_map and citation_binding_map[binding_id].get("chunk_id")
    }
    answer = "\n".join(
        dict.fromkeys(claim.text.strip() for claim in kept_claims if claim.text.strip())
    )
    if not answer:
        answer = "用于判断当前状态的实时数据已过期，无法确认此刻的状态。"
    return (
        candidate.model_copy(
            update={
                "answer": answer,
                "knowledge_chunk_ids": [
                    chunk_id
                    for chunk_id in candidate.knowledge_chunk_ids
                    if chunk_id in kept_chunk_ids
                ],
                "knowledge_citations": [
                    citation
                    for citation in candidate.knowledge_citations
                    if citation.citation_binding_id in kept_binding_ids
                ],
                "business_source_ids": [
                    source_id
                    for source_id in candidate.business_source_ids
                    if source_id in kept_business_source_ids
                ],
                "material_claims": kept_claims,
            }
        ),
        removed_count,
    )
