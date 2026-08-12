import hashlib
import json
from pathlib import Path

from supportguard.rag.types import DocumentMetadata


def load_manifest(path: Path) -> list[DocumentMetadata]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [DocumentMetadata.model_validate(item) for item in data]


def corpus_version(
    documents: list[DocumentMetadata],
    root: Path,
    *,
    pipeline_identity: dict[str, object] | None = None,
) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.document_id):
        source = root / document.source_path
        digest.update(document.model_dump_json().encode())
        digest.update(source.read_bytes())
    digest.update(
        json.dumps(
            pipeline_identity or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    return f"kb-{digest.hexdigest()[:16]}"
