from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from supportguard.rag.types import EvidenceSet, KnowledgeCitation, RankedChunk


def select_evidence(
    candidates: Sequence[RankedChunk],
    *,
    plan: str | None = None,
    region: str | None = None,
    token_budget: int = 1400,
    max_items: int = 4,
    now: datetime | None = None,
    historical: bool = False,
    version_scoped: bool = False,
) -> EvidenceSet:
    current_time = now or datetime.now(UTC)
    eligible = [
        item
        for item in candidates
        if item.chunk.source_locator is not None
        if item.chunk.status in ({"active", "deprecated"} if historical else {"active"})
        and (
            version_scoped
            or (
                item.chunk.effective_at <= current_time
                and (
                    item.chunk.effective_until is None
                    or current_time < item.chunk.effective_until
                )
            )
        )
        and (item.chunk.applicable_plan in (None, plan))
        and (item.chunk.applicable_region in (None, region))
    ]
    family_candidates: dict[str, list[RankedChunk]] = {}
    for item in eligible:
        family_candidates.setdefault(item.chunk.document_family_key, []).append(item)
    scoped: list[RankedChunk] = []
    for family_items in family_candidates.values():
        specificity = {
            id(item): int(item.chunk.applicable_plan is not None)
            + int(item.chunk.applicable_region is not None)
            for item in family_items
        }
        highest = max(specificity.values())
        winning_scopes = {
            (item.chunk.applicable_plan, item.chunk.applicable_region)
            for item in family_items
            if specificity[id(item)] == highest
        }
        if len(winning_scopes) != 1:
            for item in family_items:
                item.omission_reason = "scope_specificity_ambiguous"
            return EvidenceSet(
                chunks=[],
                citations=[],
                conflict=True,
                refusal_reason="historical_interval_ambiguous",
            )
        winning_scope = next(iter(winning_scopes))
        for item in family_items:
            if (
                item.chunk.applicable_plan,
                item.chunk.applicable_region,
            ) == winning_scope:
                scoped.append(item)
            else:
                item.omission_reason = "less_specific_scope"
    eligible = scoped
    eligible.sort(
        key=lambda item: (
            -(
                item.rerank_score
                if item.rerank_score is not None
                else item.rrf_score
            ),
            -item.chunk.authority_level,
            -item.chunk.effective_at.timestamp(),
        )
    )
    by_section: dict[tuple[str, str], set[str]] = {}
    for item in eligible:
        by_section.setdefault((item.chunk.document_id, item.chunk.section_path), set()).add(
            item.chunk.version
        )
    conflict = any(len(versions) > 1 for versions in by_section.values())

    selected: list[RankedChunk] = []
    seen_hashes: set[str] = set()
    used = 0
    for item in eligible:
        if item.chunk.content_hash in seen_hashes:
            item.omission_reason = "duplicate_content_hash"
            continue
        if selected and used + item.chunk.token_count > token_budget:
            item.omission_reason = "evidence_token_budget_exceeded"
            continue
        if len(selected) >= max_items:
            item.omission_reason = "evidence_item_limit_reached"
            continue
        selected.append(item)
        seen_hashes.add(item.chunk.content_hash)
        used += item.chunk.token_count
    if not selected:
        return EvidenceSet(chunks=[], citations=[], refusal_reason="insufficient_current_evidence")

    citations = [
        KnowledgeCitation(
            document_id=item.chunk.document_id,
            chunk_id=item.chunk.chunk_id,
            title=item.chunk.title,
            section_path=item.chunk.section_path,
            version=item.chunk.version,
            effective_at=item.chunk.effective_at,
            excerpt=item.chunk.content[:240],
            content_hash=item.chunk.content_hash,
            source_locator=item.chunk.source_locator,
        )
        for item in selected
    ]
    return EvidenceSet(
        chunks=selected,
        citations=citations,
        conflict=conflict,
        refusal_reason="conflicting_current_evidence" if conflict else None,
    )
