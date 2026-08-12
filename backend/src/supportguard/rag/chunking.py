from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from supportguard.rag.temporal import applicability_scope_hash
from supportguard.rag.types import DocumentMetadata, ParsedChunk, SourceLocatorV1

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TOKEN = re.compile(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]|[^\s]")


def token_count(text: str) -> int:
    return len(_TOKEN.findall(text))


def indexed_text(title: str, section_path: str, content: str) -> str:
    return f"{title}\n{section_path}\n{content}"


def embedding_text(title: str, section_path: str, content: str) -> str:
    return f"passage: {indexed_text(title, section_path, content)}"


def _bounded_pieces(
    content: str,
    *,
    prefix: str,
    counter: Callable[[str], int],
    maximum: int,
) -> list[str]:
    if counter(prefix + content) <= maximum:
        return [content]
    pieces: list[str] = []
    remaining = content
    while remaining:
        low, high = 1, len(remaining)
        fit = 0
        while low <= high:
            middle = (low + high) // 2
            if counter(prefix + remaining[:middle]) <= maximum:
                fit = middle
                low = middle + 1
            else:
                high = middle - 1
        if fit == 0:
            raise ValueError("chunk metadata alone exceeds embedding token limit")
        boundary = max(
            remaining.rfind("\n", 0, fit),
            remaining.rfind("。", 0, fit),
            remaining.rfind(" ", 0, fit),
        )
        take = boundary + 1 if boundary >= fit // 2 else fit
        pieces.append(remaining[:take].strip())
        remaining = remaining[take:].lstrip()
    return [piece for piece in pieces if piece]


@dataclass(frozen=True)
class _Section:
    path: str
    body: str


def _sections(markdown: str) -> list[_Section]:
    headings: list[str] = []
    body: list[str] = []
    result: list[_Section] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            result.append(_Section(" > ".join(headings) or "正文", content))
        body.clear()

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if not match:
            body.append(line)
            continue
        flush()
        level = len(match.group(1))
        headings[level - 1 :] = [match.group(2)]
    flush()
    return result


def _windows(text: str, target: int, overlap: int, maximum: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    windows: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for paragraph in paragraphs:
        size = token_count(paragraph)
        if current and current_tokens + size > target:
            windows.append("\n\n".join(current))
            tail: list[str] = []
            tail_size = 0
            for prior in reversed(current):
                prior_size = token_count(prior)
                if tail and tail_size + prior_size > overlap:
                    break
                tail.insert(0, prior)
                tail_size += prior_size
            current, current_tokens = tail, tail_size
        if size > maximum:
            # Preserve the original canonical bytes; _bounded_pieces performs the
            # exact-source split under the embedding limit.
            windows.append(paragraph)
            current, current_tokens = [], 0
        else:
            current.append(paragraph)
            current_tokens += size
    if current:
        windows.append("\n\n".join(current))
    return windows


def chunk_markdown(
    metadata: DocumentMetadata,
    markdown: str,
    *,
    target_tokens: int = 325,
    overlap_tokens: int = 50,
    max_tokens: int = 450,
    token_counter: Callable[[str], int] = token_count,
) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    canonical_bytes = markdown.encode("utf-8")
    locator_cursor = 0
    for section in _sections(markdown):
        prefix = f"passage: {metadata.title}\n{section.path}\n"
        raw_windows = _windows(section.body, target_tokens, overlap_tokens, max_tokens)
        contents = [
            piece
            for raw in raw_windows
            for piece in _bounded_pieces(
                raw,
                prefix=prefix,
                counter=token_counter,
                maximum=512,
            )
        ]
        for content in contents:
            content_bytes = content.encode("utf-8")
            byte_start = canonical_bytes.find(content_bytes, locator_cursor)
            if byte_start < 0:
                byte_start = canonical_bytes.find(content_bytes)
            if byte_start < 0:
                raise ValueError("chunk content is not an exact canonical source byte span")
            byte_end = byte_start + len(content_bytes)
            locator_cursor = byte_start + 1
            digest = hashlib.sha256(content_bytes).hexdigest()
            sequence = len(chunks)
            chunk_id = f"{metadata.document_id}:c{sequence:03d}:{digest[:8]}"
            chunks.append(
                ParsedChunk(
                    chunk_id=chunk_id,
                    document_id=metadata.document_id,
                    document_type=metadata.document_type,
                    title=metadata.title,
                    section_path=section.path,
                    sequence=sequence,
                    content=content,
                    token_count=token_counter(
                        embedding_text(metadata.title, section.path, content)
                    ),
                    content_hash=digest,
                    version=metadata.version,
                    status=metadata.status,
                    effective_at=metadata.effective_at,
                    document_family_key=metadata.document_id,
                    applicability_scope_hash=applicability_scope_hash(
                        metadata.applicable_plan, metadata.applicable_region
                    ),
                    authority_level=metadata.authority_level,
                    applicable_plan=metadata.applicable_plan,
                    applicable_region=metadata.applicable_region,
                    source_locator=SourceLocatorV1.build(
                        document_id=metadata.document_id,
                        version=metadata.version,
                        source_bytes=canonical_bytes,
                        byte_start=byte_start,
                        byte_end=byte_end,
                    ),
                )
            )
    return chunks
