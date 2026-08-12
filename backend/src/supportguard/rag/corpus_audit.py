from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from supportguard.rag.chunking import chunk_markdown
from supportguard.rag.types import DocumentMetadata


def _paragraphs(markdown: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", value.strip().lower())
        for value in re.split(r"\n\s*\n", markdown)
        if len(value.strip()) >= 80 and not value.lstrip().startswith(("#", "|", "```"))
    ]


def audit_corpus(root: Path, documents: list[DocumentMetadata]) -> dict[str, Any]:
    character_counts: dict[str, int] = {}
    paragraph_hashes: list[str] = []
    chunks = []
    combined = ""
    for document in documents:
        markdown = (root / document.source_path).read_text(encoding="utf-8")
        combined += markdown
        character_counts[document.document_id] = len(markdown)
        paragraph_hashes.extend(
            hashlib.sha256(value.encode()).hexdigest() for value in _paragraphs(markdown)
        )
        chunks.extend(chunk_markdown(document, markdown))

    repeated_paragraphs = len(paragraph_hashes) - len(set(paragraph_hashes))
    total_characters = sum(character_counts.values())
    unique_chunk_ratio = len({chunk.content_hash for chunk in chunks}) / len(chunks)
    report: dict[str, Any] = {
        "document_count": len(documents),
        "total_characters": total_characters,
        "character_counts": character_counts,
        "chunk_count": len(chunks),
        "unique_chunk_ratio": unique_chunk_ratio,
        "repeated_long_paragraphs": repeated_paragraphs,
        "has_tables": "| ---" in combined,
        "has_code_examples": "```" in combined,
        "has_error_matrix": "错误与重试矩阵" in combined,
        "has_incident_timeline": "事故时间线示例" in combined,
    }
    violations: list[str] = []
    if not 12 <= len(documents) <= 16:
        violations.append("document_count")
    if not 60_000 <= total_characters <= 100_000:
        violations.append("total_characters")
    if any(not 4_000 <= count <= 8_000 for count in character_counts.values()):
        violations.append("document_characters")
    if unique_chunk_ratio < 0.98:
        violations.append("unique_chunk_ratio")
    if repeated_paragraphs:
        violations.append("repeated_long_paragraphs")
    for key in ("has_tables", "has_code_examples", "has_error_matrix", "has_incident_timeline"):
        if not report[key]:
            violations.append(key)
    report["violations"] = violations
    report["passed"] = not violations
    return report
