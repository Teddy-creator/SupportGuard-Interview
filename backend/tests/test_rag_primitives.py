from datetime import UTC, datetime

from current_predicate_facts import record_predicate_operands
from supportguard.config import Settings
from supportguard.rag.chunking import chunk_markdown
from supportguard.rag.context_projection import (
    EVIDENCE_PROJECTION_V2,
    project_context_evidence,
)
from supportguard.rag.embeddings import (
    DeterministicEmbedding,
    configured_embedding_fingerprint,
    embedding_fingerprint,
)
from supportguard.rag.evidence import select_evidence
from supportguard.rag.intent import resolve_retrieval_intent
from supportguard.rag.query import normalize_query
from supportguard.rag.ranking import reciprocal_rank_fusion
from supportguard.rag.repository import _keyword_terms, _restricted_keyword_terms
from supportguard.rag.spans import lexical_query_terms, select_supporting_span
from supportguard.rag.types import DocumentMetadata, ParsedChunk, RankedChunk, SourceLocatorV1


def metadata(**overrides: object) -> DocumentMetadata:
    values: dict[str, object] = {
        "document_id": "api-errors-v2",
        "title": "API 错误处理",
        "document_type": "official_guide",
        "version": "2.0",
        "status": "active",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "authority_level": 90,
        "source_path": "knowledge/source_docs/api-errors-v2.md",
    }
    values.update(overrides)
    return DocumentMetadata.model_validate(values)


def chunk(identifier: str, **overrides: object) -> ParsedChunk:
    content = "429 concurrency_limit_exceeded 表示并发已满，不代表余额不足。"
    source = content.encode()
    values: dict[str, object] = {
        "chunk_id": identifier,
        "document_id": "limits-v2",
        "document_type": "official_policy",
        "title": "限额",
        "section_path": "429 > 并发",
        "sequence": 1,
        "content": content,
        "token_count": 20,
        "content_hash": identifier,
        "version": "2.0",
        "status": "active",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "document_family_key": "limits",
        "applicability_scope_hash": "a" * 64,
        "authority_level": 90,
        "source_locator": SourceLocatorV1.build(
            document_id="limits-v2",
            version="2.0",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    }
    values.update(overrides)
    return ParsedChunk.model_validate(values)


def test_query_normalization_preserves_exact_tokens() -> None:
    result = normalize_query("  POST /v1/chat/completions 返回 429，检查并发数  ")
    assert "429" in result.exact_tokens
    assert "/v1/chat/completions" in result.exact_tokens
    assert "并发限制" in result.normalized


def test_policy_query_normalization_is_neutral_to_runtime_resource_identity() -> None:
    first = normalize_query(
        "我担心这枚密钥 key_11111111111111111111111111111111 被别人看到了，"
        "仍有效的话就按安全流程吊销。"
    )
    second = normalize_query(
        "我担心这枚密钥 key_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 被别人看到了，"
        "仍有效的话就按安全流程吊销。"
    )

    assert first.normalized == second.normalized
    assert "key_" not in first.normalized
    assert "API Key" in first.normalized
    assert first.original != second.original


def test_query_normalization_does_not_strip_human_authored_reference() -> None:
    result = normalize_query("请解释 key_customer-visible 的轮换政策")

    assert "key_customer-visible" in result.normalized


def test_lexical_query_terms_expose_bounded_chinese_follow_up_ngrams() -> None:
    terms = lexical_query_terms("这个申请还在审批时，预计到账周期是什么？")
    assert {"到账", "周期", "预计到账"} <= terms


def test_keyword_terms_prioritize_specific_chinese_answer_dimensions() -> None:
    terms = _keyword_terms("这个申请还在审批时，预计到账周期是什么？", ())

    assert "到账周期" in terms
    assert terms.index("到账周期") < terms.index("到账")
    assert len(terms) <= 512


def test_restricted_compare_terms_prioritize_exact_subject_without_generic_ngrams() -> None:
    terms = _restricted_keyword_terms(
        "对比当前版本与旧版本：atlas-chat 当前支持哪些 JSON 输出能力？",
        ("atlas-chat",),
        intent="compare",
    )

    assert terms == ("atlas-chat", "json")
    assert "当前版本" not in terms
    assert len(terms) <= 64


def test_restricted_non_compare_terms_keep_normal_keyword_recall() -> None:
    query = "这个申请还在审批时，预计到账周期是什么？"

    assert _restricted_keyword_terms(query, (), intent="current") == _keyword_terms(
        query,
        (),
    )


def test_model_evidence_projection_preserves_claim_and_audit_locators() -> None:
    projected = project_context_evidence(
        {
            "chunk_id": "knowledge:c1",
            "source_locator": {"locator_hash": "a" * 64},
            "chunk_locator": {"locator_hash": "b" * 64},
        },
        citation_binding_id="citation_c1",
    )
    assert projected["source_locator_hash"] == "a" * 64
    assert projected["chunk_locator_hash"] == "b" * 64

    model_visible = project_context_evidence(
        {
            "chunk_id": "knowledge:c1",
            "source_locator": {"locator_hash": "a" * 64},
            "chunk_locator": {"locator_hash": "b" * 64},
        },
        citation_binding_id="citation_c1",
        projection_version=EVIDENCE_PROJECTION_V2,
    )
    assert model_visible["source_locator_hash"] == "a" * 64
    assert "chunk_locator_hash" not in model_visible


def test_heading_chunking_is_stable_and_bounded() -> None:
    markdown = "# API 错误\n\n## 401\n\n" + ("认证失败时检查 Bearer Header。\n\n" * 80)
    first = chunk_markdown(metadata(), markdown)
    second = chunk_markdown(metadata(), markdown)
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]
    assert all(item.token_count <= 512 for item in first)
    assert all(item.section_path == "API 错误 > 401" for item in first)
    source = markdown.encode("utf-8")
    for item in first:
        assert item.source_locator is not None
        assert item.source_locator.resolve(source).decode("utf-8") == item.content


def test_deterministic_embedding_is_normalized_and_repeatable() -> None:
    provider = DeterministicEmbedding()
    first = provider.embed_query("429 concurrency_limit_exceeded")
    assert first == provider.embed_query("429 concurrency_limit_exceeded")
    assert len(first) == 384
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


def test_configured_fixture_fingerprint_matches_the_query_provider() -> None:
    settings = Settings(app_env="test", embedding_mode="deterministic-fixture")
    assert configured_embedding_fingerprint(settings) == embedding_fingerprint(
        DeterministicEmbedding()
    )


def test_rrf_combines_independent_rankings() -> None:
    a, b, c = chunk("a"), chunk("b"), chunk("c")
    fused = reciprocal_rank_fusion([a, b], [c, a])
    assert fused[0].chunk.chunk_id == "a"
    assert fused[0].vector_rank == 1
    assert fused[0].keyword_rank == 2


def test_evidence_rejects_deprecated_and_wrong_scope() -> None:
    candidates = [
        RankedChunk(chunk=chunk("old", status="deprecated"), rrf_score=1.0),
        RankedChunk(chunk=chunk("enterprise", applicable_plan="enterprise"), rrf_score=0.9),
        RankedChunk(chunk=chunk("pro", applicable_plan="pro"), rrf_score=0.8),
    ]
    result = select_evidence(candidates, plan="pro", now=datetime(2026, 2, 1, tzinfo=UTC))
    assert [item.chunk.chunk_id for item in result.chunks] == ["pro"]
    assert result.citations[0].chunk_id == "pro"


def test_evidence_refuses_when_no_current_applicable_source() -> None:
    result = select_evidence(
        [RankedChunk(chunk=chunk("old", status="deprecated"), rrf_score=1.0)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert result.refusal_reason == "insufficient_current_evidence"
    assert result.citations == []


def test_embedding_prefix_and_metadata_are_inside_hard_token_limit() -> None:
    def character_tokenizer(value: str) -> int:
        return len(value)

    long = "# 标题\n\n## 很长章节\n\n" + ("可追溯证据内容。" * 300)
    chunks = chunk_markdown(metadata(title="权威标题"), long, token_counter=character_tokenizer)
    assert len(chunks) > 1
    assert all(item.token_count <= 512 for item in chunks)


def test_evidence_selects_multiple_independent_sources_and_stable_locators() -> None:
    candidates = [
        RankedChunk(chunk=chunk("policy", document_id="policy"), rrf_score=1.0),
        RankedChunk(chunk=chunk("manual", document_id="manual"), rrf_score=0.9),
    ]
    result = select_evidence(candidates, now=datetime(2026, 2, 1, tzinfo=UTC))
    assert [item.chunk.chunk_id for item in result.chunks] == ["policy", "manual"]
    assert all(citation.content_hash for citation in result.citations)
    assert all(
        citation.source_locator.locator_schema == "source-locator.v1"
        and citation.source_locator.version == "2.0"
        for citation in result.citations
    )


def test_historical_intent_can_retain_deprecated_evidence() -> None:
    result = select_evidence(
        [RankedChunk(chunk=chunk("old", status="deprecated"), rrf_score=1.0)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        historical=True,
    )
    assert [item.chunk.chunk_id for item in result.chunks] == ["old"]


def test_scope_specificity_beats_relevance_within_document_family() -> None:
    candidates = [
        RankedChunk(chunk=chunk("global"), rrf_score=1.0),
        RankedChunk(
            chunk=chunk("plan", applicable_plan="pro", applicability_scope_hash="b" * 64),
            rrf_score=0.8,
        ),
        RankedChunk(
            chunk=chunk(
                "plan-region",
                applicable_plan="pro",
                applicable_region="us",
                applicability_scope_hash="c" * 64,
            ),
            rrf_score=0.6,
        ),
    ]
    result = select_evidence(
        candidates,
        plan="pro",
        region="us",
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert [item.chunk.chunk_id for item in result.chunks] == ["plan-region"]
    assert candidates[0].omission_reason == "less_specific_scope"
    assert candidates[1].omission_reason == "less_specific_scope"
    record_predicate_operands(
        requirement_id="C6-P0-13",
        predicate_id="scope_specificity_deterministic",
        subject_kind="temporal_scope_selection",
        operands={
            "selected_chunk_ids": [item.chunk.chunk_id for item in result.chunks],
            "input_rrf_scores": [item.rrf_score for item in candidates],
            "omission_reasons": [item.omission_reason for item in candidates[:2]],
            "selected_scope_hash": result.chunks[0].chunk.applicability_scope_hash,
        },
    )


def test_equal_specificity_plan_and_region_scopes_fail_closed() -> None:
    result = select_evidence(
        [
            RankedChunk(chunk=chunk("plan", applicable_plan="pro"), rrf_score=1.0),
            RankedChunk(chunk=chunk("region", applicable_region="us"), rrf_score=0.9),
        ],
        plan="pro",
        region="us",
        historical=True,
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert result.chunks == []
    assert result.conflict is True
    assert result.refusal_reason == "historical_interval_ambiguous"


def test_version_only_selection_is_not_bound_to_trace_time() -> None:
    future = chunk(
        "future-version",
        status="deprecated",
        version="9.0",
        effective_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    result = select_evidence(
        [RankedChunk(chunk=future, rrf_score=1.0)],
        historical=True,
        version_scoped=True,
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert [item.chunk.chunk_id for item in result.chunks] == ["future-version"]
    record_predicate_operands(
        requirement_id="C6-P0-13",
        predicate_id="version_only_not_current_time_bound",
        subject_kind="version_only_evidence_selection",
        operands={
            "selected_chunk_ids": [item.chunk.chunk_id for item in result.chunks],
            "document_effective_year": future.effective_at.year,
            "query_time_year": 2026,
            "version_scoped": True,
        },
    )


def test_retrieval_intent_is_derived_only_from_explicit_user_semantics() -> None:
    assert resolve_retrieval_intent("当前 429 怎么处理").intent == "current"
    assert resolve_retrieval_intent("历史 v2.0 的规则是什么").intent == "historical"
    assert resolve_retrieval_intent("对比之前和现在的配额").intent == "compare"
    assert resolve_retrieval_intent("两个版本对区域限制的说法不同").intent == "compare"
    assert resolve_retrieval_intent("这两个版本最主要的区别是什么？").intent == "compare"
    current_and_old = resolve_retrieval_intent(
        "当前版本（v5）是 128k；旧版本是 64k。这两个版本最主要的区别是什么？"
    )
    assert current_and_old.intent == "compare"
    assert current_and_old.historical_version is None
    explicit_pair = resolve_retrieval_intent("请比较当前 v5 和历史 v2.2")
    assert explicit_pair.intent == "compare"
    assert explicit_pair.historical_version == "2.2"
    current_version = resolve_retrieval_intent("当前 v5 支持哪些能力？最新手册 v5 怎么说？")
    assert current_version.intent == "current"
    assert current_version.historical_version is None
    dated = resolve_retrieval_intent("查询 2026-03-14 当时的退款规则")
    assert dated.intent == "historical"
    assert dated.as_of == datetime(2026, 3, 14, tzinfo=UTC)
    ambiguous = resolve_retrieval_intent("查询 2026年3月 当时的退款规则")
    assert ambiguous.reason_code == "ambiguous_historical_anchor"
    record_predicate_operands(
        requirement_id="C6-P0-13",
        predicate_id="ambiguous_date_clarified",
        subject_kind="retrieval_intent_parser",
        operands={
            "exact_date_intent": dated.intent,
            "exact_date": dated.as_of.isoformat() if dated.as_of else None,
            "ambiguous_reason_code": ambiguous.reason_code,
            "ambiguous_as_of": ambiguous.as_of.isoformat() if ambiguous.as_of else None,
        },
    )
    intent_operands = {
        "exact_date_intent": dated.intent,
        "exact_date": dated.as_of.isoformat() if dated.as_of else None,
        "ambiguous_reason_code": ambiguous.reason_code,
        "ambiguous_as_of": ambiguous.as_of.isoformat() if ambiguous.as_of else None,
    }
    for predicate_id in ("historical_anchor_parsed", "ambiguous_anchor_clarified"):
        record_predicate_operands(
            requirement_id="C5-P0-13",
            predicate_id=predicate_id,
            subject_kind="retrieval_intent_parser",
            operands=intent_operands,
        )


def test_supporting_span_is_a_verifiable_subrange_not_the_whole_chunk() -> None:
    item = chunk("support")
    span = select_supporting_span(item, "concurrency_limit_exceeded")
    assert span.material_claim_eligible is True
    assert span.locator.byte_start >= item.source_locator.byte_start  # type: ignore[union-attr]
    assert span.locator.byte_end <= item.source_locator.byte_end  # type: ignore[union-attr]
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_keeps_bounded_prose_context_for_cross_lingual_query() -> None:
    content = (
        "SupportGuard marks the record refunded; this does not mean bank settlement."
        "合格退款按原支付方式退回，通常 5 至 10 个工作日到账。"
        "支付机构较慢时最长可能需要 15 个工作日。"
        "不得承诺具体日期。"
    )
    source = content.encode()
    item = chunk(
        "refund-route",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="limits-v2",
            version="2.0",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(item, "refund processing time original payment route")

    assert span.material_claim_eligible is True
    assert "原支付方式" in span.text
    assert "5 至 10 个工作日" in span.text
    assert "15 个工作日" in span.text
    assert len(span.text) <= 800
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_matches_a_natural_language_chinese_follow_up() -> None:
    content = (
        "人工批准后，该状态只表示退款指令已受理，不表示银行已经入账。"
        "合格退款按原支付方式退回，通常 5 至 10 个工作日到账。"
        "支付机构较慢时最长可能需要 15 个工作日。"
    )
    source = content.encode()
    item = chunk(
        "refund-route-zh",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="billing-refunds-v3",
            version="3.1",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(item, "退款审批期间能否继续咨询？退款通常多久原路退回？")

    assert span.material_claim_eligible is True
    assert "原支付方式" in span.text
    assert "5 至 10 个工作日" in span.text
    assert "15 个工作日" in span.text
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_does_not_expand_structured_rows() -> None:
    content = (
        "| refunded | return existing result |\n"
        "| charged | create proposal |\n"
        "| disputed | transfer to human |"
    )
    source = content.encode()
    item = chunk(
        "structured-refund",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="limits-v2",
            version="2.0",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(item, "refunded")

    assert span.text == "| refunded | return existing result |"
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_prefers_specific_product_transition_over_generic_conflict() -> None:
    content = (
        "- 回归必须注入缺少证据、旧版本冲突、工具超时和恶意文本。\n"
        "- 上下文提升：2026-03 atlas-chat revision 从 64k 提升至 128k；"
        "旧请求继续按 64k 解释。\n"
        "- 其他套餐版本变更仍按各自政策处理。"
    )
    source = content.encode()
    item = chunk(
        "atlas-transition",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="policy-changelog-2026",
            version="2026.07",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(
        item,
        "对比当前版本与旧版本：atlas-chat 当前支持哪些 JSON 输出能力？",
    )

    assert span.material_claim_eligible is True
    assert "atlas-chat" in span.text
    assert "64k" in span.text
    assert "128k" in span.text
    assert "缺少证据、旧版本冲突" not in span.text
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_uses_diagnostic_anchors_in_a_long_decision_matrix() -> None:
    content = (
        "| 场景 | 触发条件 | 必要证据 | 处理决定 | 例外与禁止项 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 429 rate limit | 一分钟窗口达到 RPM | requests_last_minute | "
        "等待窗口恢复并指数退避 | 余额充足也不能绕过 RPM |\n"
        "| 429 concurrency | 长连接占满并发槽位 | concurrency_current、并发上限 | "
        "减少并行流或等待已有请求结束 | 增加客户端重试会放大拥塞 |\n"
        "| model_not_found | 请求模型名无法解析 | model、region、plan、API 版本 | "
        "核对拼写和开放范围 | 不能把配置错误描述为平台事故 |"
    )
    source = content.encode()
    item = chunk(
        "diagnostic-decision-matrix",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="api-errors-retries-v2",
            version="2.2",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(
        item,
        "为什么账户有余额但 API 返回 429 concurrency_limit_exceeded 并发限制错误？",
    )

    assert span.text.startswith("| 429 concurrency |")
    assert "减少并行流或等待已有请求结束" in span.text
    assert "model_not_found" not in span.text
    assert span.locator.resolve(item.content.encode()).decode() == span.text


def test_supporting_span_uses_identifier_components_in_a_long_checklist() -> None:
    content = (
        "- **429 rate limit**：核对套餐 RPM；等待窗口恢复并指数退避。\n"
        "- **429 concurrency**：核对 concurrency_current 与并发上限；"
        "减少并行流或等待已有请求结束。\n"
        "- **500 before response**：查询 request_id 后再决定是否重试。\n"
        "- **model_not_found**：核对 model、region、plan 和 API 版本。"
    )
    source = content.encode()
    item = chunk(
        "diagnostic-checklist",
        content=content,
        source_locator=SourceLocatorV1.build(
            document_id="api-errors-retries-v2",
            version="2.2",
            source_bytes=source,
            byte_start=0,
            byte_end=len(source),
        ),
    )

    span = select_supporting_span(
        item,
        "concurrency_limit_exceeded 应该如何处理？",
    )

    assert span.text.startswith("- **429 concurrency**")
    assert "减少并行流或等待已有请求结束" in span.text
    assert span.locator.resolve(item.content.encode()).decode() == span.text
