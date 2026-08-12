from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from supportguard.rag.types import ParsedChunk, RankedChunk


def reciprocal_rank_fusion(
    vector: Sequence[ParsedChunk],
    keyword: Sequence[ParsedChunk],
    *,
    k: int = 60,
    document_coherence_weight: float = 0.15,
) -> list[RankedChunk]:
    merged: dict[str, RankedChunk] = {}
    for rank, chunk in enumerate(vector, 1):
        item = merged.setdefault(chunk.chunk_id, RankedChunk(chunk=chunk))
        item.vector_rank = rank
        item.vector_similarity = chunk.vector_similarity
        item.vector_contribution = 1 / (k + rank)
        item.rrf_score += item.vector_contribution
    for rank, chunk in enumerate(keyword, 1):
        item = merged.setdefault(chunk.chunk_id, RankedChunk(chunk=chunk))
        item.keyword_rank = rank
        item.keyword_score = chunk.keyword_score
        item.keyword_contribution = 1 / (k + rank)
        item.rrf_score += item.keyword_contribution
        if item.chunk.exact_token_match is False and chunk.exact_token_match:
            item.chunk = item.chunk.model_copy(
                update={"exact_token_match": chunk.exact_token_match}
            )
    # Long manuals often contain several mutually supporting sections. A small,
    # bounded document-level signal prevents generic appendices from unrelated
    # manuals displacing the core section, while chunk relevance remains primary.
    document_scores: dict[str, list[float]] = defaultdict(list)
    for item in merged.values():
        document_scores[item.chunk.document_id].append(item.rrf_score)
    coherence = {
        document_id: sum(sorted(scores, reverse=True)[:3])
        for document_id, scores in document_scores.items()
    }
    for item in merged.values():
        item.rrf_score += document_coherence_weight * coherence[item.chunk.document_id]
    fused = sorted(merged.values(), key=lambda item: (-item.rrf_score, item.chunk.chunk_id))
    # RRF rewards candidates that occur in both channels, and the bounded
    # document-coherence signal intentionally rewards mutually supporting
    # sections.  Without a channel-head reservation those two useful signals
    # can nevertheless push the strongest semantic or exact lexical match out
    # of the first evidence window when one long manual contributes many
    # generic sections.  Preserve exactly the first result from each
    # independent channel, ordered by their fused score, then keep the original
    # RRF order for every remaining candidate.  This is query-agnostic and does
    # not change scores, eligibility, or the bounded candidate denominator.
    channel_heads = {item.chunk_id for item in (*vector[:1], *keyword[:1])}
    return [
        *[item for item in fused if item.chunk.chunk_id in channel_heads],
        *[item for item in fused if item.chunk.chunk_id not in channel_heads],
    ]
