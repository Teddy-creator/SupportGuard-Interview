from __future__ import annotations

import re
from dataclasses import dataclass

from supportguard.rag.types import ParsedChunk, SourceLocator

_BOUNDARY = re.compile(r"[^。！？!?\n]+[。！？!?]?|[^\n]+")
_LATIN_TERM = re.compile(r"[A-Za-z0-9_./:-]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_IDENTIFIER_SEPARATOR = re.compile(r"[_./:-]+")
_HTTP_STATUS = re.compile(r"[1-5]\d{2}")
_MAX_CONTEXT_SENTENCES = 3
_MAX_CONTEXT_CHARACTERS = 800


def lexical_query_terms(query: str) -> set[str]:
    """Return matchable terms without treating an entire Chinese clause as one token."""

    terms = {term.lower() for term in _LATIN_TERM.findall(query)}
    for match in _CJK_RUN.finditer(query):
        run = match.group(0)
        if len(run) < 2:
            continue
        # Bounded n-grams make Chinese retrieval queries lexically auditable
        # without requiring a runtime tokenizer or language-specific model.
        for width in range(2, min(4, len(run)) + 1):
            terms.update(run[index : index + width] for index in range(len(run) - width + 1))
    return terms


def _is_structured_line(value: str) -> bool:
    stripped = value.lstrip()
    return stripped.startswith(("|", "- ", "* ", "> ", "```")) or bool(
        re.match(r"\d+[.)]\s", stripped)
    )


def _match_score(
    value: str,
    *,
    terms: set[str],
    document_frequency: dict[str, int],
    sentence_count: int,
) -> int:
    """Rank a support sentence by specific query evidence, not first overlap.

    A long chunk can contain generic regression prose such as “旧版本冲突”
    before the actual product transition.  Selecting the first matching
    sentence lets those generic words hide a later, much stronger
    ``atlas-chat / JSON / 64k`` match.  Inverse sentence frequency and a
    bounded exact-token bonus keep the choice deterministic while preferring
    the sentence that carries the query's distinctive subject.
    """

    folded = value.casefold()
    score = 0
    for term in terms:
        if term not in folded:
            continue
        specificity = sentence_count + 1 - document_frequency[term]
        exact_token_bonus = 4 if _LATIN_TERM.fullmatch(term) else 1
        score += len(term) ** 2 * specificity * exact_token_bonus
    return score


def _query_anchor_terms(query: str) -> set[str]:
    """Extract exact diagnostic anchors without making every short token dominant.

    Long structured chunks often repeat generic words such as ``API`` and
    ``错误`` across every row.  HTTP status codes and delimited identifiers are
    different: they name the diagnostic lane selected by the customer.  Their
    components also let a query such as ``concurrency_limit_exceeded`` match a
    human-readable table row such as ``429 concurrency``.
    """

    anchors: set[str] = set()
    for raw_term in _LATIN_TERM.findall(query):
        term = raw_term.casefold()
        if _HTTP_STATUS.fullmatch(term):
            anchors.add(term)
        if not _IDENTIFIER_SEPARATOR.search(term):
            continue
        anchors.add(term)
        anchors.update(
            component
            for component in _IDENTIFIER_SEPARATOR.split(term)
            if len(component) >= 4
        )
    return anchors


def _anchor_score(value: str, *, anchors: set[str]) -> int:
    folded = value.casefold()
    score = 0
    for anchor in anchors:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(anchor)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if pattern.search(folded):
            score += len(anchor) ** 2
    return score


def _bounded_context_window(
    content: str,
    matches: list[re.Match[str]],
    chosen_index: int,
) -> tuple[int, int]:
    """Keep nearby prose needed to interpret one exact supporting sentence.

    Cross-lingual vector retrieval can identify the right short section while
    lexical span matching only recognizes an English product token in its
    first sentence.  A bounded forward window preserves the answer-bearing
    sentences without turning tables or checklists into broad citations.
    """

    chosen = matches[chosen_index]
    start = chosen.start()
    end = chosen.end()
    if _is_structured_line(chosen.group(0)):
        return start, end
    for candidate in matches[chosen_index + 1 : chosen_index + _MAX_CONTEXT_SENTENCES]:
        gap = content[end : candidate.start()]
        if "\n\n" in gap or _is_structured_line(candidate.group(0)):
            break
        candidate_end = candidate.end()
        if candidate_end - start > _MAX_CONTEXT_CHARACTERS:
            break
        end = candidate_end
    return start, end


@dataclass(frozen=True, slots=True)
class SupportingSpan:
    text: str
    locator: SourceLocator
    material_claim_eligible: bool
    reason_code: str


def select_supporting_span(chunk: ParsedChunk, query: str) -> SupportingSpan:
    if chunk.source_locator is None:
        raise ValueError("supporting_span_requires_chunk_locator")
    terms = lexical_query_terms(query)
    anchors = _query_anchor_terms(query)
    matches = list(_BOUNDARY.finditer(chunk.content))
    document_frequency = {
        term: sum(term in match.group(0).casefold() for match in matches) for term in terms
    }
    ranked = [
        (
            _anchor_score(match.group(0), anchors=anchors),
            _match_score(
                match.group(0),
                terms=terms,
                document_frequency=document_frequency,
                sentence_count=len(matches),
            ),
            -index,
            index,
        )
        for index, match in enumerate(matches)
    ]
    chosen_index = (
        max(ranked, key=lambda item: (item[0], item[1], item[2]))[3]
        if ranked
        and (
            max(item[0] for item in ranked) > 0
            or max(item[1] for item in ranked) > 0
        )
        else None
    )
    lexical = chosen_index is not None
    if chosen_index is None:
        if not matches:
            raise ValueError("empty_chunk_cannot_support_claim")
        chosen_index = 0
    window_start, window_end = _bounded_context_window(chunk.content, matches, chosen_index)
    raw_window = chunk.content[window_start:window_end]
    text = raw_window.strip()
    relative_character_start = window_start + len(raw_window) - len(raw_window.lstrip())
    relative_byte_start = len(chunk.content[:relative_character_start].encode("utf-8"))
    span_bytes = text.encode("utf-8")
    locator = chunk.source_locator.subspan(
        parent_span=chunk.content.encode("utf-8"),
        relative_start=relative_byte_start,
        relative_end=relative_byte_start + len(span_bytes),
    )
    return SupportingSpan(
        text=text,
        locator=locator,
        material_claim_eligible=lexical,
        reason_code="lexical_support_span" if lexical else "background_only_no_unique_support",
    )
