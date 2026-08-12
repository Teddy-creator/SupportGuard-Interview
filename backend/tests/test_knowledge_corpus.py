from pathlib import Path

from supportguard.rag.chunking import chunk_markdown
from supportguard.rag.corpus_audit import audit_corpus
from supportguard.rag.manifest import load_manifest


def test_frozen_corpus_meets_complexity_and_integrity_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    documents = load_manifest(root / "knowledge/manifests/documents.json")
    assert 12 <= len(documents) <= 16
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_markdown(
            document,
            (root / document.source_path).read_text(encoding="utf-8"),
        )
    ]
    report = audit_corpus(root, documents)
    assert report["passed"], report["violations"]
    assert 60_000 <= report["total_characters"] <= 100_000
    assert all(4_000 <= value <= 8_000 for value in report["character_counts"].values())
    assert len(chunks) == report["chunk_count"]
    assert all(chunk.token_count <= 512 for chunk in chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert len({chunk.content_hash for chunk in chunks}) / len(chunks) >= 0.98
    assert {document.status for document in documents} == {"active", "deprecated"}
    assert {document.document_type for document in documents} >= {
        "official_policy",
        "faq",
        "changelog",
        "migration_guide",
    }


def test_refund_policy_separates_runtime_execution_from_external_settlement() -> None:
    root = Path(__file__).resolve().parents[2]
    documents = load_manifest(root / "knowledge/manifests/documents.json")
    policy = next(item for item in documents if item.document_id == "billing-refunds-v3")
    chunks = chunk_markdown(
        policy,
        (root / policy.source_path).read_text(encoding="utf-8"),
    )
    settlement = [
        item
        for item in chunks
        if "原支付方式" in item.content and "5 至 10 个工作日" in item.content
    ]
    assert len(settlement) == 1
    assert "系统已执行" in settlement[0].content
    assert "资金已到账" in settlement[0].content
    assert "不连接真实支付渠道" in settlement[0].content
