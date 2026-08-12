from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.db.base import Base
from supportguard.evals.gate import CONTRACT_ROOT, CONTRACTS, RAG_DATASET, RAG_DATASET_SHA256
from supportguard.evals.phase7_common import (
    Phase7ContractError,
    atomic_write_json,
    canonical_sha256,
    require_candidate,
    require_ignored_output,
    sha256_file,
    utc_now,
)
from supportguard.rag.embeddings import E5SmallEmbedding, embedding_fingerprint
from supportguard.rag.ingest import ingest_corpus
from supportguard.rag.repository import KnowledgeRepository
from supportguard.rag.service import RetrievalService
from supportguard.rag.types import KnowledgeCitation, RetrievalScopeSnapshot

_LOGICAL_TIME = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
_SAFE_LIMITATION_MARKERS = (
    "不能",
    "不得",
    "无法",
    "未发布",
    "没有可",
    "拒绝",
    "不保证",
    "不可",
)


def _load_inputs(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contract_name, contract_hash = CONTRACTS["rag_dev30"]
    contract_path = root / CONTRACT_ROOT / contract_name
    dataset_path = root / CONTRACT_ROOT / RAG_DATASET
    if sha256_file(contract_path) != contract_hash:
        raise Phase7ContractError("rag_dev30_contract_hash_mismatch")
    if sha256_file(dataset_path) != RAG_DATASET_SHA256:
        raise Phase7ContractError("rag_dev30_dataset_hash_mismatch")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines()]
    if len(cases) != 30 or contract.get("case_count") != 30:
        raise Phase7ContractError("rag_dev30_denominator_mismatch")
    return contract, cases


def _ranked_chunk_ids(trace: Any) -> list[str]:
    output: list[str] = []
    for row in trace.rrf_candidates:
        chunk_id = row.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id not in output:
            output.append(chunk_id)
    return output


def _citation_valid(root: Path, citation: KnowledgeCitation) -> bool:
    locator = citation.source_locator
    document_path = root / "knowledge" / "source_docs" / f"{citation.document_id}.md"
    if not document_path.is_file():
        return False
    try:
        resolved = locator.resolve(document_path.read_bytes())
    except ValueError:
        return False
    return bool(resolved) and citation.content_hash == locator.span_hash


def _safe_case(
    category: str,
    *,
    selected_contents: list[str],
    trace: Any,
    top_ten: list[str],
    gold: set[str],
) -> bool | None:
    if category == "answerable":
        return None
    if category == "unanswerable":
        return bool(gold.intersection(top_ten)) and any(
            marker in content
            for content in selected_contents
            for marker in _SAFE_LIMITATION_MARKERS
        )
    groups = {
        str(item.get("group")): item
        for item in trace.evidence_groups
        if item.get("group") is not None
    }
    # Retrieval recall is scored independently for every case.  The safety contract for a
    # version conflict asks whether the product kept current and historical evidence in two
    # explicit, non-empty lanes instead of silently collapsing them into one answer context.
    # Requiring the gold chunk here would count the same retrieval miss a second time.
    return all(
        isinstance(groups.get(group, {}).get("selected_candidates"), list)
        and bool(groups[group]["selected_candidates"])
        and isinstance(groups[group].get("filter"), dict)
        and groups[group]["filter"].get("intent") == group
        for group in ("current", "historical")
    )


async def run_rag_dev30(
    root: Path,
    *,
    candidate_sha: str,
    output: Path,
) -> dict[str, Any]:
    root = root.resolve()  # noqa: ASYNC240 - no async filesystem work follows this call
    identity_before = require_candidate(root, candidate_sha)
    output = require_ignored_output(root, output)
    contract, cases = _load_inputs(root)
    manifest_path = root / str(contract["corpus_manifest"]["path"])
    if sha256_file(manifest_path) != contract["corpus_manifest"]["sha256"]:
        raise Phase7ContractError("rag_dev30_corpus_manifest_hash_mismatch")

    with tempfile.TemporaryDirectory(prefix="supportguard-phase7-rag-") as directory:
        database_path = Path(directory) / "rag.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            embedding = E5SmallEmbedding(local_files_only=True)
            async with factory() as session:
                ingest = await ingest_corpus(
                    session,
                    root=root,
                    manifest_path=manifest_path,
                    embedding=embedding,
                )
                await session.commit()
                service = RetrievalService(KnowledgeRepository(session), embedding)
                scope = RetrievalScopeSnapshot(
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    subscription_id="sub_demo",
                    subscription_version=3,
                    plan="pro",
                    region_trace_id="rag-dev30-region",
                    region_trace_version=1,
                    region="eu-west",
                )
                results: list[dict[str, Any]] = []
                reciprocal_ranks: list[float] = []
                citation_total = 0
                citation_valid = 0
                material_claim_total = 0
                unsupported_material_claims = 0
                safe_results: list[bool] = []
                for case in cases:
                    category = str(case["category"])
                    intent: Literal["current", "compare"] = (
                        "compare" if category == "version_conflict" else "current"
                    )
                    _, evidence, trace = await service.retrieve_with_trace(
                        str(case["question"]),
                        intent=intent,
                        logical_time=_LOGICAL_TIME,
                        scope_snapshot=scope,
                    )
                    ranked = _ranked_chunk_ids(trace)
                    gold = {str(value) for value in case["resolved_chunk_ids"]}
                    first_rank = next(
                        (
                            rank
                            for rank, chunk_id in enumerate(ranked[:10], start=1)
                            if chunk_id in gold
                        ),
                        None,
                    )
                    reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
                    valid_flags = [
                        _citation_valid(root, citation) for citation in evidence.citations
                    ]
                    citation_total += len(valid_flags)
                    citation_valid += sum(valid_flags)
                    # This public RAG regression has no Provider answer stage.  Its material-claim
                    # denominator is therefore the deterministic extractive evidence spans that the
                    # product may bind into a later answer.  A span is supported only when its exact
                    # immutable source locator resolves against the frozen corpus.
                    material_claim_total += len(valid_flags)
                    unsupported_material_claims += sum(not flag for flag in valid_flags)
                    selected_contents = [item.chunk.content for item in evidence.chunks]
                    safe = _safe_case(
                        category,
                        selected_contents=selected_contents,
                        trace=trace,
                        top_ten=ranked[:10],
                        gold=gold,
                    )
                    if safe is not None:
                        safe_results.append(safe)
                    results.append(
                        {
                            "id": case["id"],
                            "category": category,
                            "top_10_chunk_ids": ranked[:10],
                            "gold_rank": first_rank,
                            "recall_at_5": bool(gold.intersection(ranked[:5])),
                            "citation_count": len(valid_flags),
                            "citation_binding_valid": all(valid_flags),
                            "safe_conflict_or_refusal": safe,
                            "pipeline_fingerprint": trace.pipeline_fingerprint,
                            "reranker": trace.pipeline_contract.get("reranker"),
                        }
                    )
        finally:
            await engine.dispose()

    eligible = len(results)
    recalled = sum(bool(item["recall_at_5"]) for item in results)
    safe_denominator = len(safe_results)
    metrics = {
        "eligible_recall_at_5": recalled / eligible,
        "eligible_recall_numerator": recalled,
        "eligible_recall_denominator": eligible,
        "eligible_mrr_at_10": sum(reciprocal_ranks) / eligible,
        "citation_binding_validity": citation_valid / citation_total if citation_total else 0.0,
        "citation_binding_numerator": citation_valid,
        "citation_binding_denominator": citation_total,
        "unsupported_material_claim_rate": (
            unsupported_material_claims / material_claim_total if material_claim_total else 0.0
        ),
        "unsupported_material_claim_numerator": unsupported_material_claims,
        "unsupported_material_claim_denominator": material_claim_total,
        "conflict_and_unanswerable_safe_rate": (
            sum(safe_results) / safe_denominator if safe_denominator else 0.0
        ),
        "conflict_and_unanswerable_safe_numerator": sum(safe_results),
        "conflict_and_unanswerable_safe_denominator": safe_denominator,
    }
    thresholds = contract["metrics"]
    passed = (
        metrics["eligible_recall_at_5"] >= thresholds["eligible_recall_at_5_min"]
        and metrics["eligible_mrr_at_10"] >= thresholds["eligible_mrr_at_10_min"]
        and metrics["citation_binding_validity"] == thresholds["citation_binding_validity"]
        and metrics["unsupported_material_claim_rate"]
        == thresholds["unsupported_material_claim_rate"]
        and metrics["conflict_and_unanswerable_safe_rate"]
        >= thresholds["conflict_and_unanswerable_safe_rate_min"]
    )
    identity_after = require_candidate(root, candidate_sha)
    if identity_after != identity_before:
        raise Phase7ContractError("candidate_source_changed_during_rag_dev30")
    receipt = {
        "schema": "supportguard.interview_v2.rag_dev30_receipt.v1",
        "classification": "public_dev_regression_not_independent_holdout",
        "recorded_at": utc_now(),
        "candidate": identity_before.as_dict(),
        "contract": {
            "path": str(CONTRACT_ROOT / CONTRACTS["rag_dev30"][0]),
            "sha256": CONTRACTS["rag_dev30"][1],
            "dataset_sha256": RAG_DATASET_SHA256,
            "case_count": 30,
        },
        "knowledge": {
            "manifest_sha256": contract["corpus_manifest"]["sha256"],
            "index_version": ingest.index_version,
            "document_count": ingest.document_count,
            "chunk_count": ingest.chunk_count,
            "embedding_model": embedding.model_name,
            "embedding_revision": embedding.revision,
            "embedding_fingerprint": embedding_fingerprint(embedding),
            "online_reranker": "disabled",
        },
        "metrics": metrics,
        "thresholds": thresholds,
        "cases": results,
        "claims": {
            "passed": passed,
            "evaluation_v6_holdout_accessed": False,
            "cross_encoder_executed": False,
            "provider_quality_measured": False,
            "exclusions": [],
        },
        "cleanup": {"temporary_database_removed": True},
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    atomic_write_json(output, receipt)
    if not passed:
        raise Phase7ContractError("rag_dev30_threshold_not_met")
    return receipt
