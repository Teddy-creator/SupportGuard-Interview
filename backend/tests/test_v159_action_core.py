from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from supportguard.agent.action_specs import ACTION_SPECS, get_action_spec
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.legacy_recovery import recover_legacy_action_admission
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.obligations import (
    ContextCitationBinding,
    evaluate_action_obligations,
    qualified_knowledge_evidence_ids,
)
from supportguard.agent.proposal_assembler import (
    assemble_action_candidate,
    bind_provider_synthesis,
    canonicalize_unreferenced_provider_claims,
    evaluate_action_candidate_eligibility,
    provider_synthesis_binding_error_paths,
    provider_synthesis_reference_contract,
)
from supportguard.agent.schemas import (
    AgentDecision,
    BoundEvidenceSynthesis,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.agent.tool_policy import (
    semantic_batch_rejections,
    semantic_invocation_key,
)
from supportguard.contracts.action_preconditions import (
    ActionAdmission,
    ActionAdmissionV2,
    explicit_action_with_immediate_domain,
    explicit_current_turn_action,
    resolve_action_admission_v2,
    resolve_missing_action_preconditions,
)
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ReadToolCall, ToolGateway

NOW = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
RUN_ID = "run-v159"
TENANT_ID = "tenant-v159"
CUSTOMER_ID = "customer-v159"


def admission(
    *,
    requested_action: str,
    issue_type: str,
    message: str,
    requested_concurrency_limit: int | None = None,
    history: list[dict[str, Any]] | None = None,
    continuation_action: str | None = None,
) -> ActionAdmissionV2:
    return resolve_action_admission_v2(
        message,
        history or [],
        requested_action=requested_action,  # type: ignore[arg-type]
        issue_type=issue_type,
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        current_message_id="message-current",
        turn_group_id="turn-current",
        requested_concurrency_limit=requested_concurrency_limit,
        continuation_action=continuation_action,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("action_type", "issue_type", "message", "expected_arguments"),
    [
        (
            "refund",
            "billing_refund",
            "请给账单 bill_alpha 退款。",
            {"billing_record_id": "bill_alpha"},
        ),
        (
            "api_key_revocation",
            "credential_security",
            "请立即撤销 API Key key_alpha。",
            {"api_key_ref": "key_alpha"},
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "请把并发上限调整到 40。",
            {
                "change_type": "quota_change",
                "target": {"concurrency_limit": 40},
            },
        ),
    ],
)
def test_action_admission_v2_admits_unique_typed_customer_fields(
    action_type: str,
    issue_type: str,
    message: str,
    expected_arguments: dict[str, Any],
) -> None:
    result = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=message,
        requested_concurrency_limit=(40 if action_type == "entitlement_change" else None),
    )

    assert result.schema_version == "action-admission.v2"
    assert result.status == "admitted"
    assert result.action_type == action_type
    assert result.extracted_arguments == expected_arguments
    assert result.field_sources
    assert result.field_sources[0].message_id == "message-current"
    assert (
        result.scope_hash
        == hashlib.sha256(b'{"customer_id":"customer-v159","tenant_id":"tenant-v159"}').hexdigest()
    )


@pytest.mark.parametrize("punctuation", [".", ":"])
def test_billing_reference_excludes_ascii_sentence_final_punctuation(
    punctuation: str,
) -> None:
    message = f"Please refund bill_alpha{punctuation}"

    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message=message,
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {"billing_record_id": "bill_alpha"}
    source = result.field_sources[0]
    assert message[source.span_start : source.span_end] == "bill_alpha"


def test_api_key_clause_correction_binds_only_the_positive_resource() -> None:
    message = "不要撤销 API Key key_old。我刚才说错了，请撤销 API Key key_compromised，旧的不要动。"

    result = admission(
        requested_action="api_key_revocation",
        issue_type="credential_security",
        message=message,
    )

    assert explicit_current_turn_action(message) == "api_key_revocation"
    assert result.status == "admitted"
    assert result.extracted_arguments == {"api_key_ref": "key_compromised"}
    source = result.field_sources[0]
    assert message[source.span_start : source.span_end] == "key_compromised"


def test_entitlement_clause_correction_binds_only_the_final_target() -> None:
    message = "不要把并发提高到 80。请改成 40，按正常审批流程处理。"

    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message=message,
        requested_concurrency_limit=40,
    )

    assert explicit_current_turn_action(message) == "entitlement_change"
    assert result.status == "admitted"
    assert result.extracted_arguments == {
        "change_type": "quota_change",
        "target": {"concurrency_limit": 40},
    }
    source = result.field_sources[0]
    assert message[source.span_start : source.span_end].strip() == "40"


def test_action_admission_accepts_long_opaque_subscription_reference() -> None:
    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message=(
            "请把订阅 sub_customer_workspace_8f5df632a2b94d489916853daa19c2ef "
            "的并发配额从当前值明确提升到 60。"
        ),
        requested_concurrency_limit=60,
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {
        "change_type": "quota_change",
        "target": {"concurrency_limit": 60},
    }


@pytest.mark.parametrize(
    ("action_type", "issue_type", "message", "missing_field"),
    [
        ("refund", "billing_refund", "请帮我退款。", "billing_record_id"),
        (
            "api_key_revocation",
            "credential_security",
            "请立即撤销这个 API Key。",
            "api_key_ref",
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "请提高并发上限。",
            "target",
        ),
    ],
)
def test_action_admission_v2_reports_precise_missing_fields(
    action_type: str,
    issue_type: str,
    message: str,
    missing_field: str,
) -> None:
    result = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=message,
    )

    assert result.status == "missing"
    assert result.missing_fields == (missing_field,)
    assert result.clarification_question


def test_action_admission_v2_uses_only_six_accepted_customer_messages() -> None:
    history = [
        {
            "role": "customer",
            "id": f"message-{index}",
            "content": ("请帮我退款。" if index == 0 else f"补充背景 {index}"),
        }
        for index in range(7)
    ]

    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="bill_alpha",
        history=history,
        continuation_action="refund",
    )

    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"
    assert len(result.source_message_ids) == 6
    assert "message-0" not in result.source_message_ids


def test_action_admission_v2_continues_only_a_trusted_unfinished_action() -> None:
    history = [
        {
            "role": "customer",
            "id": "message-refund",
            "content": "请帮我退款。",
        }
    ]

    without_continuation = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="bill_alpha",
        history=history,
    )
    with_continuation = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="bill_alpha",
        history=history,
        continuation_action="refund",
    )

    assert without_continuation.status == "mismatch"
    assert without_continuation.reason_code == "plan_without_customer_action"
    assert with_continuation.status == "admitted"
    assert with_continuation.extracted_arguments == {"billing_record_id": "bill_alpha"}


def test_duplicate_relation_selects_the_duplicate_record_as_refund_target() -> None:
    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="bill_duplicate 是 bill_original 的重复扣费，请按现行政策退款。",
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {"billing_record_id": "bill_duplicate"}
    assert result.field_sources[0].message_id == "message-current"


def test_duplicate_relation_with_an_extra_record_remains_ambiguous() -> None:
    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message=(
            "bill_duplicate 是 bill_original 的重复扣费，但还提到了 bill_third，请按现行政策退款。"
        ),
    )

    assert result.status == "mismatch"
    assert result.reason_code == "resource_ref_ambiguous"


def test_explicit_refund_continuation_reuses_only_the_immediately_prior_record() -> None:
    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="如果确认是重复扣费，就按现行政策发起退款。",
        history=[
            {
                "role": "customer",
                "id": "message-diagnosis",
                "content": "帮我核对 bill_duplicate 为什么收了两次。",
            }
        ],
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {"billing_record_id": "bill_duplicate"}
    assert result.source_message_ids == ("message-diagnosis", "message-current")


def test_unqualified_refund_request_does_not_silently_inherit_a_prior_record() -> None:
    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message="请直接退款。",
        history=[
            {
                "role": "customer",
                "id": "message-diagnosis",
                "content": "帮我核对 bill_duplicate 为什么收了两次。",
            }
        ],
    )

    assert result.status == "missing"
    assert result.missing_fields == ("billing_record_id",)


@pytest.mark.parametrize(
    ("action_type", "issue_type", "message", "expected_arguments"),
    [
        (
            "api_key_revocation",
            "credential_security",
            ("API Key 引用 key_alpha 可能已经泄露，请核验元数据并按安全政策发起撤销。"),
            {"api_key_ref": "key_alpha"},
        ),
        (
            "api_key_revocation",
            "credential_security",
            ("我担心 key_alpha 被别人看到了，帮我确认它仍有效的话就按安全流程吊销。"),
            {"api_key_ref": "key_alpha"},
        ),
        (
            "api_key_revocation",
            "credential_security",
            "请先核验 API Key key_alpha，然后按安全政策撤销。",
            {"api_key_ref": "key_alpha"},
        ),
        (
            "entitlement_change",
            "entitlement_change",
            ("订阅 sub_alpha 的并发额度不够，请确认套餐后申请提升到 40。"),
            {
                "change_type": "quota_change",
                "target": {"concurrency_limit": 40},
            },
        ),
    ],
)
def test_action_admission_is_order_independent_for_explicit_requests(
    action_type: str,
    issue_type: str,
    message: str,
    expected_arguments: dict[str, Any],
) -> None:
    result = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=message,
        requested_concurrency_limit=(40 if action_type == "entitlement_change" else None),
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == expected_arguments


@pytest.mark.parametrize(
    ("action_type", "issue_type", "first_turn", "second_turn", "expected_arguments"),
    [
        (
            "api_key_revocation",
            "credential_security",
            "我需要撤销一枚可能泄露的 API Key，但还没有提供引用。",
            "Key Reference 是 key_alpha，请继续处理。",
            {"api_key_ref": "key_alpha"},
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "当前订阅的并发不足，我希望申请调整，但还没有给出目标值。",
            "目标并发上限是 40，请继续处理。",
            {
                "change_type": "quota_change",
                "target": {"concurrency_limit": 40},
            },
        ),
    ],
)
def test_action_admission_continues_explicit_multi_turn_requests(
    action_type: str,
    issue_type: str,
    first_turn: str,
    second_turn: str,
    expected_arguments: dict[str, Any],
) -> None:
    first = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=first_turn,
    )
    assert first.status == "missing"

    second = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=second_turn,
        requested_concurrency_limit=(40 if action_type == "entitlement_change" else None),
        history=[
            {
                "role": "customer",
                "id": "message-first",
                "content": first_turn,
            }
        ],
        continuation_action=action_type,
    )

    assert second.status == "admitted"
    assert second.extracted_arguments == expected_arguments


@pytest.mark.parametrize(
    "message",
    [
        "请问为什么 API Key key_alpha 被撤销了？",
        "不要撤销 API Key key_alpha，只需解释它的当前状态。",
        "我不想撤销 API Key key_alpha，只想了解风险。",
        "请问并发上限为什么被调整到 40？",
        "申请提高并发上限需要满足哪些条件？",
        "无需调整并发上限，我只想查看当前套餐。",
    ],
)
def test_action_admission_does_not_treat_questions_or_negation_as_authorization(
    message: str,
) -> None:
    result = admission(
        requested_action="none",
        issue_type="product_knowledge",
        message=message,
    )

    assert result.status == "none"
    assert result.reason_code == "no_high_risk_action"


@pytest.mark.parametrize(
    ("action_type", "issue_type", "message", "requested_concurrency_limit"),
    [
        ("api_key_revocation", "credential_security", "请别撤销 API Key key_old。", None),
        ("api_key_revocation", "credential_security", "请先别撤销 API Key key_old。", None),
        ("api_key_revocation", "credential_security", "请先不撤销 API Key key_old。", None),
        ("api_key_revocation", "credential_security", "请暂不撤销 API Key key_old。", None),
        ("api_key_revocation", "credential_security", "我拒绝撤销 API Key key_old。", None),
        (
            "api_key_revocation",
            "credential_security",
            "Please don’t revoke API Key key_old.",
            None,
        ),
        (
            "api_key_revocation",
            "credential_security",
            "You must not revoke API Key key_old.",
            None,
        ),
        (
            "api_key_revocation",
            "credential_security",
            "You should not revoke API Key key_old.",
            None,
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "请暂不把并发调整到 40。",
            40,
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "Please do not increase concurrency to 40.",
            40,
        ),
    ],
)
def test_action_admission_matching_plan_cannot_override_customer_negation(
    action_type: str,
    issue_type: str,
    message: str,
    requested_concurrency_limit: int | None,
) -> None:
    result = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=message,
        requested_concurrency_limit=requested_concurrency_limit,
    )

    assert explicit_current_turn_action(message) is None
    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"


@pytest.mark.parametrize(
    ("action_type", "issue_type", "message", "requested_concurrency_limit"),
    [
        (
            "api_key_revocation",
            "credential_security",
            "我想知道撤销 API Key key_old 会有什么影响？",
            None,
        ),
        (
            "api_key_revocation",
            "credential_security",
            "请查看 API Key key_old，不要撤销旧的；撤销是否会影响服务？",
            None,
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "我想知道把并发调整到 40 会有什么影响？",
            40,
        ),
    ],
)
def test_action_admission_impact_question_never_authorizes_matching_plan(
    action_type: str,
    issue_type: str,
    message: str,
    requested_concurrency_limit: int | None,
) -> None:
    result = admission(
        requested_action=action_type,
        issue_type=issue_type,
        message=message,
        requested_concurrency_limit=requested_concurrency_limit,
    )

    assert explicit_current_turn_action(message) is None
    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"


def test_coordinated_distinct_actions_remain_ambiguous_for_provider_none() -> None:
    message = "请给 bill_invoice_42 退款，同时撤销 key_primary_42。"

    result = admission(
        requested_action="none",
        issue_type="billing_refund",
        message=message,
    )

    assert explicit_current_turn_action(message) is None
    assert result.status == "mismatch"
    assert result.reason_code == "action_intent_ambiguous"


def test_negated_first_turn_cannot_be_resurrected_by_second_turn_field() -> None:
    first_turn = "请先别撤销这枚 API Key。"
    first = admission(
        requested_action="api_key_revocation",
        issue_type="credential_security",
        message=first_turn,
    )
    second = admission(
        requested_action="api_key_revocation",
        issue_type="credential_security",
        message="Key Reference 是 key_alpha。",
        history=[{"role": "customer", "id": "message-first", "content": first_turn}],
        continuation_action="api_key_revocation",
    )

    assert first.status == "mismatch"
    assert first.reason_code == "plan_without_customer_action"
    assert second.status == "mismatch"
    assert second.reason_code == "plan_without_customer_action"


def test_ie_p10_explicit_second_turn_correction_inherits_domain_not_old_target() -> None:
    first_turn = "不要把并发提高到80。"
    second_turn = "请改成40，按正常审批流程处理。"

    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message=second_turn,
        requested_concurrency_limit=40,
        history=[{"role": "customer", "id": "message-first", "content": first_turn}],
        continuation_action="entitlement_change",
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {
        "change_type": "quota_change",
        "target": {"concurrency_limit": 40},
    }
    assert len(result.field_sources) == 1
    assert result.field_sources[0].message_id == "message-current"
    assert result.source_message_ids == ("message-current",)


def test_immediate_entitlement_correction_does_not_require_prior_missing_state() -> None:
    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message="请改成 40，按正常审批流程处理。",
        requested_concurrency_limit=40,
        history=[
            {
                "role": "customer",
                "id": "message-first",
                "content": "不要把并发提高到 80。",
            }
        ],
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {
        "change_type": "quota_change",
        "target": {"concurrency_limit": 40},
    }
    assert result.source_message_ids == ("message-current",)
    assert (
        explicit_action_with_immediate_domain(
            "请改成 40，按正常审批流程处理。",
            [
                {
                    "role": "customer",
                    "content": "不要把并发提高到 80。",
                }
            ],
        )
        == "entitlement_change"
    )


def test_adjust_to_synonym_is_an_explicit_bounded_entitlement_request() -> None:
    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message="把当前项目的并发配额调整为 40，需要的话走审批。",
        requested_concurrency_limit=40,
    )

    assert result.status == "admitted"
    assert result.extracted_arguments == {
        "change_type": "quota_change",
        "target": {"concurrency_limit": 40},
    }


@pytest.mark.parametrize("second_turn", ["40", "把并发改成40会有什么影响？"])
def test_ie_p10_domain_inheritance_rejects_bare_field_and_impact_question(
    second_turn: str,
) -> None:
    first_turn = "不要把并发提高到80。"

    result = admission(
        requested_action="entitlement_change",
        issue_type="entitlement_change",
        message=second_turn,
        requested_concurrency_limit=40,
        history=[{"role": "customer", "id": "message-first", "content": first_turn}],
        continuation_action="entitlement_change",
    )

    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"


@pytest.mark.parametrize(
    "message",
    [
        "根据退款政策，这条重复扣费的标准处理流程是什么？只说明流程，不要创建退款申请。",
        "重复扣费通常应该怎么处理？",
        "我只想了解重复扣费退款政策，不要发起退款。",
        "无需创建退款申请，请解释退款流程。",
        "不用退款，只查询 bill_alpha 的状态。",
        "如何申请退款？",
        "申请退款需要什么条件？",
        "Do not create a refund request; explain the duplicate-charge policy.",
        "Please only explain how the duplicate charge refund process works.",
    ],
)
def test_refund_questions_and_negation_never_grant_action_authority(
    message: str,
) -> None:
    result = admission(
        requested_action="none",
        issue_type="billing_refund",
        message=message,
    )

    assert explicit_current_turn_action(message) is None
    assert resolve_missing_action_preconditions(message, []) is None
    assert result.status == "none"
    assert result.reason_code == "no_high_risk_action"


@pytest.mark.parametrize(
    ("message", "expected_status"),
    [
        ("请检查账单 bill_alpha 是否为重复扣费，并按当前政策处理。", "admitted"),
        ("bill_alpha 是重复扣费，请按政策退款。", "admitted"),
        ("如果 bill_alpha 确认是重复扣费，请按政策退款。", "admitted"),
        ("不要解释了，请直接退款。", "missing"),
        ("Please refund bill_alpha.", "admitted"),
    ],
)
def test_explicit_refund_requests_remain_authorizing(
    message: str,
    expected_status: str,
) -> None:
    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message=message,
    )

    assert explicit_current_turn_action(message) == "refund"
    assert result.status == expected_status
    assert result.reason_code in {
        "action_request_admitted",
        "required_action_fields_missing",
    }


def test_provider_plan_cannot_turn_informational_refund_question_into_action() -> None:
    message = "重复扣费的标准处理流程是什么？不要创建退款申请。"

    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message=message,
    )

    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"


@pytest.mark.parametrize(
    "current_turn",
    [
        "不要退款了，bill_alpha 只用于查询状态。",
        "bill_alpha 的退款流程是什么？只解释流程。",
    ],
)
def test_refund_negation_or_information_cannot_inherit_prior_authority(
    current_turn: str,
) -> None:
    history = [
        {
            "role": "customer",
            "id": "message-prior-refund",
            "content": "请帮我退款。",
        }
    ]

    result = admission(
        requested_action="refund",
        issue_type="billing_refund",
        message=current_turn,
        history=history,
        continuation_action="refund",
    )

    assert explicit_current_turn_action(current_turn) is None
    assert resolve_missing_action_preconditions(current_turn, history) is None
    assert result.status == "mismatch"
    assert result.reason_code == "plan_without_customer_action"
    assert result.source_message_ids == ("message-current",)


@pytest.mark.asyncio
async def test_refund_process_question_reaches_bounded_read_tool_decision() -> None:
    message = "根据退款政策，这条重复扣费的标准处理流程是什么？只说明流程，不要创建退款申请。"
    capability = issue_test_runtime_capability(testing=True)
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None, test_capability=capability),
        test_capability=capability,
    )
    state = AgentState(
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        ticket_id="ticket-v1528-refund-question",
        run_id="run-v1528-refund-question",
        trace_id="trace-v1528-refund-question",
        redacted_message=message,
        customer_message_id="message-current",
        conversation_turn_id="turn-current",
        classification={
            "issue_type": "billing_refund",
            "risk": "low",
            "policy_boundary": "allowed",
            "requested_action": "none",
            "requested_concurrency_limit": None,
            "needs_realtime_facts": True,
            "support_subject": "customer_problem",
            "rationale": "Read-only refund process question.",
        },
        classification_context=[],
        relevant_history=[
            {
                "history_kind": "message",
                "role": "customer",
                "content": (
                    "账单 bill_demo_duplicate 的金额、状态以及是否为重复扣费分别是什么？只查询。"
                ),
            }
        ],
        tool_observations=[],
        evidence=[],
        provider_turns=[],
        llm_calls=1,
        tool_rounds=0,
        tool_attempts=0,
        step_index=0,
    )

    admission_update = await graph.intake_nodes.resolve_action_admission(state)
    merged = AgentState(**{**state, **admission_update})
    decision_update = await graph.decision_nodes.agent_decide(merged)
    decision = AgentDecision.model_validate(decision_update["agent_decision"])

    assert admission_update["action_admission"]["status"] == "none"
    assert admission_update["action_admission"]["reason_code"] == "no_high_risk_action"
    assert decision.decision_type == "tool_calls"
    assert {item.call.name for item in decision.tool_calls} == {
        "search_knowledge",
        "query_billing_record",
    }
    assert "candidate" not in decision_update


def test_action_admission_v2_none_is_explicit_state() -> None:
    result = admission(
        requested_action="none",
        issue_type="product_knowledge",
        message="atlas-chat 支持哪些响应格式？",
    )

    assert result.status == "none"
    assert result.action_type is None
    assert result.reason_code == "no_high_risk_action"


@pytest.mark.parametrize(
    ("requested_action", "issue_type", "message", "reason_code"),
    [
        (
            "none",
            "billing_refund",
            "请给账单 bill_alpha 退款。",
            "plan_omits_explicit_action",
        ),
        (
            "refund",
            "billing_refund",
            "请给 bill_alpha 和 bill_beta 退款。",
            "resource_ref_ambiguous",
        ),
        (
            "entitlement_change",
            "entitlement_change",
            "请把并发调整到 40，再调整到 50。",
            "target_ambiguous",
        ),
        (
            "refund",
            "credential_security",
            "请给账单 bill_alpha 退款。",
            "issue_type_mismatch",
        ),
    ],
)
def test_action_admission_v2_fails_closed_on_mismatch(
    requested_action: str,
    issue_type: str,
    message: str,
    reason_code: str,
) -> None:
    result = admission(
        requested_action=requested_action,
        issue_type=issue_type,
        message=message,
    )

    assert result.status == "mismatch"
    assert result.reason_code == reason_code
    assert result.clarification_question


def test_action_spec_registry_is_the_only_three_action_contract() -> None:
    assert tuple(ACTION_SPECS) == (
        "refund",
        "api_key_revocation",
        "entitlement_change",
    )
    assert {item.proposal_action for item in ACTION_SPECS.values()} == {
        "refund_proposal",
        "api_key_revocation_proposal",
        "entitlement_change_proposal",
    }
    assert {item.policy_capability for item in ACTION_SPECS.values()} == {
        "propose_refund",
        "propose_api_key_revocation",
        "propose_entitlement_change",
    }
    assert {item.runtime_effect_capability for item in ACTION_SPECS.values()} == {
        "execute_refund",
        "execute_api_key_revocation",
        "execute_entitlement_change",
    }
    for action_type, spec in ACTION_SPECS.items():
        assert spec.action_type == action_type
        assert spec.proposal_schema_name == spec.proposal_action
        assert spec.proposal_schema.model_json_schema()["type"] == "object"
        assert sum(item.kind == "knowledge" for item in spec.obligations) == 1
        assert len({item.target_field for item in spec.proposal_fields}) == len(
            spec.proposal_fields
        )


def test_action_spec_has_one_product_runtime_definition_site() -> None:
    source_root = Path("backend/src/supportguard")
    definition_sites = [
        path for path in source_root.rglob("*.py") if "ActionSpec(" in path.read_text()
    ]

    assert definition_sites == [Path("backend/src/supportguard/actions/service.py")]


def test_graph_has_no_action_specific_candidate_canonicalizers() -> None:
    graph_source = Path("backend/src/supportguard/agent/graph.py").read_text()
    action_flow_source = Path("backend/src/supportguard/agent/nodes/action_flow.py").read_text()
    read_loop_source = Path("backend/src/supportguard/agent/tool_loop.py").read_text()
    runtime_support_source = Path(
        "backend/src/supportguard/agent/nodes/runtime_support.py"
    ).read_text()
    agent_runtime_source = "\n".join(
        (graph_source, action_flow_source, read_loop_source, runtime_support_source)
    )

    assert "_canonicalize_requested_" not in agent_runtime_source
    assert "_evaluate_proposal_eligibility" not in agent_runtime_source
    assert "proposal_contracts =" not in agent_runtime_source
    assert (
        "CANDIDATE_ARGUMENT_SCHEMAS"
        not in Path("backend/src/supportguard/agent/schemas.py").read_text()
    )
    finish_reason_source = runtime_support_source.split("def _finish_reason", 1)[1].split(
        "def _fingerprint", 1
    )[0]
    assert '"proposed"' not in finish_reason_source
    assert "assemble_action_candidate(" in action_flow_source
    assert "from supportguard.agent.tool_policy import" in read_loop_source
    assert "from supportguard.agent.proposal_assembler import" in action_flow_source
    assert "self.action_flow_nodes.assemble_action" in graph_source


def test_action_current_usage_read_is_canonicalized_to_freshest_window() -> None:
    case = _CASES["entitlement_change"]
    admitted = _admitted("entitlement_change", case)
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("entitlement_change"),
        admission=admitted,
        observations=[],
        run_id=RUN_ID,
        now=NOW,
    )
    decision = AgentDecision.model_validate(
        {
            "decision_type": "tool_calls",
            "decision_summary": "Read current action evidence.",
            "tool_calls": [
                {
                    "tool_call_id": "call-subscription",
                    "call": {
                        "name": "query_subscription",
                        "arguments": {},
                    },
                },
                {
                    "tool_call_id": "call-usage",
                    "call": {
                        "name": "query_api_usage",
                        "arguments": {"window": "1h"},
                    },
                },
            ],
        }
    )

    canonical, changed = AgentRuntimeServices._canonicalize_action_read_arguments(  # noqa: SLF001
        AgentState(
            action_admission=admitted.model_dump(mode="json"),
            action_obligation_ledger=ledger.model_dump(mode="json"),
        ),
        decision,
    )

    assert changed is True
    assert canonical.tool_calls[0].call.arguments.model_dump(mode="json") == {}
    assert canonical.tool_calls[1].call.arguments.model_dump(mode="json") == {"window": "1m"}
    assert decision.tool_calls[1].call.arguments.model_dump(mode="json") == {"window": "1h"}


def test_non_action_usage_diagnostic_preserves_provider_window() -> None:
    decision = AgentDecision.model_validate(
        {
            "decision_type": "tool_calls",
            "decision_summary": "Read the requested reporting window.",
            "tool_calls": [
                {
                    "tool_call_id": "call-usage",
                    "call": {
                        "name": "query_api_usage",
                        "arguments": {"window": "24h"},
                    },
                }
            ],
        }
    )

    canonical, changed = AgentRuntimeServices._canonicalize_action_read_arguments(  # noqa: SLF001
        AgentState(action_admission={}, action_obligation_ledger={}),
        decision,
    )

    assert changed is False
    assert canonical == decision


_CASES = {
    "refund": {
        "issue_type": "billing_refund",
        "message": "请给账单 bill_alpha 退款。",
        "resource_tool": "query_billing_record",
        "resource_data": {
            "billing_record_id": "bill_alpha",
            "amount": "49.00",
            "currency": "USD",
            "status": "charged",
            "duplicate_of": "bill_original",
            "version": 2,
        },
        "document_id": "billing-refunds-v3",
        "document_type": "official_policy",
        "version": "3.1",
        "section": "重复扣费退款资格",
    },
    "api_key_revocation": {
        "issue_type": "credential_security",
        "message": "请立即撤销 API Key key_alpha。",
        "resource_tool": "query_api_key_metadata",
        "resource_data": {
            "api_key_id": "key_alpha",
            "fingerprint": "fp_alpha",
            "status": "active",
            "version": 3,
            "last_used_summary": {"region": "eu-west"},
        },
        "document_id": "api-key-incident-v1",
        "document_type": "security_policy",
        "version": "1.0",
        "section": "撤销资格",
    },
    "entitlement_change": {
        "issue_type": "entitlement_change",
        "message": "请把并发上限调整到 40。",
        "resource_tool": "query_subscription",
        "resource_data": {
            "subscription_id": "sub_alpha",
            "plan": "pro",
            "status": "active",
            "rpm_limit": 1000,
            "concurrency_limit": 20,
            "catalog_eligibility": ["quota_change", "plan_change"],
            "version": 4,
        },
        "document_id": "entitlement-changes-v1",
        "document_type": "official_policy",
        "version": "1.0",
        "section": "明确目标与配额审批",
    },
}


def _observation(
    *,
    tool_name: str,
    data: dict[str, Any],
    scope_hash: str,
    suffix: str,
    freshness_status: str = "fresh",
    tenant_id: str = TENANT_ID,
    customer_id: str = CUSTOMER_ID,
    status: str = "ok",
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "tool_call_id": f"call-{suffix}",
        "invocation_id": f"invocation-{suffix}",
        "observation_id": f"observation-{suffix}",
        "ticket_id": "ticket-v159",
        "run_id": RUN_ID,
        "attempt_index": 1,
        "status": status,
        "retryable": False,
        "observed_at": NOW.isoformat(),
        "freshness_status": freshness_status,
        "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
        "duration_ms": 1,
        "source_refs": [
            {
                "source_type": (
                    "knowledge_chunk" if tool_name == "search_knowledge" else "business_record"
                ),
                "source_id": (
                    str(data["evidence"][0]["chunk_id"])
                    if tool_name == "search_knowledge"
                    else f"record-{suffix}"
                ),
                "observed_at": NOW.isoformat(),
            }
        ],
        "trusted_scope": {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "scope_hash": scope_hash,
        },
        "data": data,
    }


def _knowledge_data(case: dict[str, Any]) -> dict[str, Any]:
    evidence_id = f"{case['document_id']}:c001"
    return {
        "normalized_query": "policy",
        "conflict": False,
        "refusal_reason": None,
        "index_version": "index-v159",
        "evidence": [
            {
                "evidence_id": evidence_id,
                "document_id": case["document_id"],
                "document_type": case["document_type"],
                "chunk_id": evidence_id,
                "title": "Policy",
                "section_path": case["section"],
                "version": case["version"],
                "effective_at": "2026-01-01T00:00:00Z",
                "content_hash": "a" * 64,
                "source_locator": {
                    "locator_hash": "b" * 64,
                    "index_version": "index-v159",
                },
                "eligibility_envelope": {
                    "corpus_snapshot_id": "corpus-v159",
                    "index_version": "index-v159",
                    "document_internal_id": f"internal-{case['document_id']}",
                    "chunk_id": evidence_id,
                    "status": "active",
                    "authority_level": 100,
                    "applicable_plan": None,
                    "applicable_region": None,
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_until": None,
                    "logical_time": NOW.isoformat(),
                    "filter_hash": "c" * 64,
                    "outcome": "eligible",
                    "reason_code": "eligible",
                },
                "supporting_span": "The current policy requires approval.",
                "supporting_span_eligible": True,
            }
        ],
    }


def _admitted(action_type: str, case: dict[str, Any]) -> ActionAdmissionV2:
    return admission(
        requested_action=action_type,
        issue_type=str(case["issue_type"]),
        message=str(case["message"]),
        requested_concurrency_limit=(40 if action_type == "entitlement_change" else None),
    )


def _nominal_observations(
    action_type: str,
    case: dict[str, Any],
    admitted: ActionAdmissionV2,
) -> list[dict[str, Any]]:
    observations = [
        _observation(
            tool_name=str(case["resource_tool"]),
            data=dict(case["resource_data"]),
            scope_hash=admitted.scope_hash,
            suffix=f"{action_type}-resource",
        ),
        _observation(
            tool_name="search_knowledge",
            data=_knowledge_data(case),
            scope_hash=admitted.scope_hash,
            suffix=f"{action_type}-knowledge",
        ),
    ]
    if action_type == "entitlement_change":
        observations.append(
            _observation(
                tool_name="query_api_usage",
                data={
                    "window": "1m",
                    "window_start": (NOW - timedelta(minutes=1)).isoformat(),
                    "window_end": NOW.isoformat(),
                    "request_count": 100,
                    "input_token_count": 1000,
                    "output_token_count": 500,
                    "concurrency_current": 20,
                    "concurrency_peak": 20,
                    "remaining_balance": "120.00",
                    "balance_currency": "USD",
                    "freshness_seconds": 1,
                    "freshness_status": "fresh",
                    "resource_version": "usage-v1",
                },
                scope_hash=admitted.scope_hash,
                suffix="entitlement-usage",
            )
        )
    return observations


def _legacy_admission(
    action_type: str,
    case: dict[str, Any],
) -> ActionAdmission:
    admitted = _admitted(action_type, case)
    return ActionAdmission(
        action_type=action_type,  # type: ignore[arg-type]
        issue_type=str(case["issue_type"]),  # type: ignore[arg-type]
        missing_fields=(),
        extracted_arguments=admitted.extracted_arguments,
        clarification_question="legacy-only",
    )


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_legacy_admission_rehydrates_only_from_current_provenance(
    action_type: str,
) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)

    recovery = recover_legacy_action_admission(
        legacy=_legacy_admission(action_type, case),
        redacted_message=str(case["message"]),
        classification={
            "schema_version": "classification.v2",
            "requested_action": action_type,
            "issue_type": case["issue_type"],
            "requested_concurrency_limit": (40 if action_type == "entitlement_change" else None),
        },
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        current_message_id="message-current",
        turn_group_id="turn-current",
        observations=_nominal_observations(action_type, case, admitted),
        run_id=RUN_ID,
        now=NOW,
    )

    assert recovery.recovered is True
    assert recovery.reason_code == "legacy_admission_rehydrated"
    assert recovery.admission is not None
    assert recovery.admission.schema_version == "action-admission.v2"
    assert recovery.ledger is not None
    assert recovery.ledger.all_reads_qualified is True


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_knowledge", "legacy_observation_binding_unproven"),
        ("wrong_scope", "legacy_observation_binding_unproven"),
        ("wrong_resource", "legacy_observation_binding_unproven"),
        ("competing_resource", "legacy_resource_binding_ambiguous"),
    ],
)
def test_legacy_admission_fails_closed_when_binding_is_not_provable(
    mutation: str,
    expected_reason: str,
) -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    observations = _nominal_observations("refund", case, admitted)
    if mutation == "missing_knowledge":
        observations = [item for item in observations if item["tool_name"] != "search_knowledge"]
    elif mutation == "wrong_scope":
        observations[0]["trusted_scope"]["tenant_id"] = "tenant-other"
    elif mutation == "wrong_resource":
        observations[0]["data"]["billing_record_id"] = "bill_other"
    else:
        competing = {
            **observations[0],
            "tool_call_id": "call-competing",
            "invocation_id": "invocation-competing",
            "observation_id": "observation-competing",
            "data": {
                **observations[0]["data"],
                "billing_record_id": "bill_other",
            },
        }
        observations.append(competing)

    recovery = recover_legacy_action_admission(
        legacy=_legacy_admission("refund", case),
        redacted_message=str(case["message"]),
        classification={
            "schema_version": "classification.v2",
            "requested_action": "refund",
            "issue_type": "billing_refund",
        },
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        current_message_id="message-current",
        turn_group_id="turn-current",
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert recovery.recovered is False
    assert recovery.reason_code == expected_reason


@pytest.mark.asyncio
async def test_graph_rehydrates_v1_checkpoint_without_new_provider_or_tool_call() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    observations = _nominal_observations("refund", case, admitted)
    for observation in observations:
        observation["fresh_until"] = "2099-01-01T00:00:00Z"
    provider = DeterministicFakeProvider()
    graph = SupportGraph(
        provider=provider,
        retrieval=None,
        gateway=ToolGateway(None),
    )

    update = await graph.action_flow_nodes.evaluate_obligations(
        AgentState(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            ticket_id="ticket-v159",
            run_id=RUN_ID,
            redacted_message=str(case["message"]),
            customer_message_id="message-current",
            conversation_turn_id="turn-current",
            classification={
                "schema_version": "classification.v2",
                "requested_action": "refund",
                "issue_type": "billing_refund",
                "policy_boundary": "allowed",
            },
            action_admission=_legacy_admission("refund", case).model_dump(mode="json"),
            tool_observations=observations,
            context_citation_bindings=[],
            llm_calls=2,
            tool_rounds=1,
            tool_attempts=2,
        )
    )

    assert update["action_admission"]["schema_version"] == "action-admission.v2"
    assert update["action_obligation_ledger"]["next_state"] == "synthesize"
    assert "llm_calls" not in update
    assert "tool_rounds" not in update
    assert "tool_attempts" not in update


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_obligation_ledger_requires_two_stage_knowledge_binding(
    action_type: str,
) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)
    observations = _nominal_observations(action_type, case, admitted)
    spec = get_action_spec(action_type)  # type: ignore[arg-type]

    qualified = evaluate_action_obligations(
        action_spec=spec,
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert qualified.all_reads_qualified is True
    assert qualified.all_obligations_satisfied is False
    assert qualified.next_state == "synthesize"
    knowledge = next(item for item in qualified.obligations if item.kind == "knowledge")
    assert knowledge.status == "read_qualified"
    assert qualified.unsatisfied_capabilities == ()

    evidence = _knowledge_data(case)["evidence"][0]
    bound = evaluate_action_obligations(
        action_spec=spec,
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        citation_bindings=[
            ContextCitationBinding(
                citation_binding_id=f"citation-{action_type}",
                provider_attempt_id="provider-attempt-2",
                evidence_id=str(evidence["evidence_id"]),
                document_id=str(evidence["document_id"]),
                chunk_id=str(evidence["chunk_id"]),
                content_hash=str(evidence["content_hash"]),
                locator_hash=str(evidence["source_locator"]["locator_hash"]),
            )
        ],
        provider_attempt_id="provider-attempt-2",
        now=NOW,
    )

    assert bound.all_obligations_satisfied is True
    assert bound.next_state == "assemble_candidate"
    assert all(item.status == "satisfied" for item in bound.obligations)


def test_action_synthesis_receives_only_contract_qualified_knowledge() -> None:
    case = _CASES["api_key_revocation"]
    admitted = _admitted("api_key_revocation", case)
    observations = _nominal_observations("api_key_revocation", case, admitted)
    knowledge = next(item for item in observations if item["tool_name"] == "search_knowledge")
    knowledge["data"]["evidence"].append(
        {
            **knowledge["data"]["evidence"][0],
            "evidence_id": "api-key-incident-v1:c-unqualified",
            "chunk_id": "api-key-incident-v1:c-unqualified",
            "section_path": "Unrelated appendix",
            "content_hash": "d" * 64,
            "source_locator": {
                **knowledge["data"]["evidence"][0]["source_locator"],
                "locator_hash": "e" * 64,
            },
        }
    )
    knowledge["source_refs"].append(
        {
            "source_type": "knowledge_chunk",
            "source_id": "api-key-incident-v1:c-unqualified",
            "observed_at": NOW.isoformat(),
        }
    )

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("api_key_revocation"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert qualified_knowledge_evidence_ids(ledger) == ("api-key-incident-v1:c001",)


def test_knowledge_obligation_rejects_a_wrong_document_type() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    observations = _nominal_observations("refund", case, admitted)
    knowledge = next(item for item in observations if item["tool_name"] == "search_knowledge")
    knowledge["data"]["evidence"][0]["document_type"] = "marketing"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    knowledge_entry = next(item for item in ledger.obligations if item.kind == "knowledge")
    assert knowledge_entry.status == "pending"
    assert knowledge_entry.reason_code == "knowledge_evidence_not_qualified"
    assert ledger.next_state == "collect_reads"


def test_knowledge_obligation_accepts_policy_term_in_bounded_supporting_span() -> None:
    case = _CASES["api_key_revocation"]
    admitted = _admitted("api_key_revocation", case)
    observations = _nominal_observations("api_key_revocation", case, admitted)
    knowledge = next(item for item in observations if item["tool_name"] == "search_knowledge")
    evidence = knowledge["data"]["evidence"][0]
    evidence["section_path"] = "事件处理顺序"
    evidence["supporting_span"] = "当前租户内仍有效的 API Key 必须先形成撤销提案并等待独立审批。"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("api_key_revocation"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    knowledge_entry = next(item for item in ledger.obligations if item.kind == "knowledge")
    assert knowledge_entry.status == "read_qualified"
    assert ledger.next_state == "synthesize"


def test_knowledge_obligation_rejects_unrelated_heading_and_supporting_span() -> None:
    case = _CASES["api_key_revocation"]
    admitted = _admitted("api_key_revocation", case)
    observations = _nominal_observations("api_key_revocation", case, admitted)
    knowledge = next(item for item in observations if item["tool_name"] == "search_knowledge")
    evidence = knowledge["data"]["evidence"][0]
    evidence["section_path"] = "常规事件记录"
    evidence["supporting_span"] = "请记录事件发生时间并联系支持团队。"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("api_key_revocation"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    knowledge_entry = next(item for item in ledger.obligations if item.kind == "knowledge")
    assert knowledge_entry.status == "pending"
    assert knowledge_entry.reason_code == "knowledge_evidence_not_qualified"
    assert ledger.next_state == "collect_reads"


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_obligation_ledger_marks_stale_current_resource_for_reread(
    action_type: str,
) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)
    observations = _nominal_observations(action_type, case, admitted)
    observations[0]["freshness_status"] = "stale"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert ledger.obligations[0].status == "stale"
    assert str(case["resource_tool"]) in ledger.unsatisfied_capabilities
    assert ledger.next_state == "collect_reads"


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_obligation_ledger_fails_closed_on_wrong_scope(action_type: str) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)
    observations = _nominal_observations(action_type, case, admitted)
    observations[0]["trusted_scope"]["tenant_id"] = "tenant-other"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert ledger.obligations[0].status == "failed"
    assert ledger.obligations[0].reason_code == "observation_scope_mismatch"
    assert ledger.next_state == "safe_stop"


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_obligation_ledger_fails_closed_on_policy_conflict(action_type: str) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)
    observations = _nominal_observations(action_type, case, admitted)
    knowledge = next(item for item in observations if item["tool_name"] == "search_knowledge")
    knowledge["data"]["conflict"] = True

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    knowledge_entry = next(item for item in ledger.obligations if item.kind == "knowledge")
    assert knowledge_entry.status == "conflicted"
    assert ledger.next_state == "safe_stop"


def test_obligation_ledger_ignores_observations_from_other_runs() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    observations = _nominal_observations("refund", case, admitted)
    for item in observations:
        item["run_id"] = "run-history"

    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        now=NOW,
    )

    assert all(item.status == "pending" for item in ledger.obligations)
    assert set(ledger.unsatisfied_capabilities) == {
        "query_billing_record",
        "search_knowledge",
    }


def test_semantic_knowledge_key_is_stable_across_query_rewrites() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=[],
        run_id=RUN_ID,
        now=NOW,
    )
    first = ReadToolCall.model_validate(
        {
            "name": "search_knowledge",
            "arguments": {"query": "重复扣费退款政策"},
        }
    )
    rewritten = ReadToolCall.model_validate(
        {
            "name": "search_knowledge",
            "arguments": {"query": "等额 duplicate charge 的审批规则"},
        }
    )

    first_key = semantic_invocation_key(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        ledger=ledger,
        call=first,
        index_snapshot="index-v159",
    )
    rewritten_key = semantic_invocation_key(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        ledger=ledger,
        call=rewritten,
        index_snapshot="index-v159",
    )

    assert first_key == rewritten_key
    assert len(first_key) == 64


def test_same_batch_calls_remain_eligible_until_one_qualifies() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    pending = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=[],
        run_id=RUN_ID,
        now=NOW,
    )
    calls = [
        ReadToolCall.model_validate(
            {
                "name": "search_knowledge",
                "arguments": {"query": "first legal query"},
            }
        ),
        ReadToolCall.model_validate(
            {
                "name": "search_knowledge",
                "arguments": {"query": "second legal query"},
            }
        ),
    ]

    assert (
        semantic_batch_rejections(
            action_spec=get_action_spec("refund"),
            ledger=pending,
            calls=calls,
        )
        == {}
    )

    qualified = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=_nominal_observations("refund", case, admitted),
        run_id=RUN_ID,
        now=NOW,
    )
    assert semantic_batch_rejections(
        action_spec=get_action_spec("refund"),
        ledger=qualified,
        calls=list(reversed(calls)),
    ) == {
        0: "obligation_already_qualified",
        1: "obligation_already_qualified",
    }


def test_bound_evidence_synthesis_schema_has_no_action_authority() -> None:
    with pytest.raises(ValidationError):
        ProviderBoundEvidenceSynthesis.model_validate(
            {
                "schema_version": "bound-evidence-synthesis.v1",
                "answer": "Bound explanation.",
                "material_claims": [
                    {
                        "text": "Bound explanation.",
                        "citation_binding_ids": ["citation-policy"],
                        "observation_source_ids": ["record-current"],
                    }
                ],
                "action": "refund_proposal",
                "proposed_arguments": {"billing_record_id": "bill_alpha"},
            }
        )


def test_synthesis_binding_validator_rejects_cross_namespace_authority() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "已核验当前账单与退款政策。",
            "material_claims": [
                {
                    "text": "当前账单符合退款政策。",
                    "citation_binding_ids": ["citation-policy"],
                    # Structurally valid, but the model confused a knowledge
                    # chunk identity with a business Observation source.
                    "observation_source_ids": ["billing-refunds-v3:c001"],
                }
            ],
        }
    )

    errors = provider_synthesis_binding_error_paths(
        synthesis=synthesis,
        evidence=[
            {
                "citation_binding_id": "citation-policy",
                "chunk_id": "billing-refunds-v3:c001",
                "source_locator_hash": "a" * 64,
                "supporting_span_eligible": True,
            }
        ],
        observations=[
            {
                "tool_name": "query_billing_record",
                "status": "ok",
                "source_refs": [{"source_id": "record-current"}],
            }
        ],
    )

    assert "material_claims.0.observation_source_ids:unknown_business_source" in errors
    assert "material_claims:business_source_required" in errors


def test_synthesis_reference_contract_projects_only_current_allowed_ids() -> None:
    contract = provider_synthesis_reference_contract(
        evidence=[
            {
                "citation_binding_id": "citation-eligible",
                "supporting_span_eligible": True,
            },
            {
                "citation_binding_id": "citation-background",
                "supporting_span_eligible": False,
            },
        ],
        observations=[
            {
                "tool_name": "query_subscription",
                "status": "ok",
                "source_refs": [
                    {"source_id": "subscription-current"},
                    {"source_id": "subscription-current"},
                ],
            },
            {
                "tool_name": "search_knowledge",
                "status": "ok",
                "source_refs": [{"source_id": "knowledge-not-business"}],
            },
            {
                "tool_name": "query_api_usage",
                "status": "error",
                "source_refs": [{"source_id": "failed-not-allowed"}],
            },
        ],
    )

    assert contract["allowed_citation_binding_ids"] == ["citation-eligible"]
    assert contract["allowed_observation_source_ids"] == ["subscription-current"]
    assert "omit the claim" in contract["per_claim_rule"]
    assert len(contract["global_rules"]) == 2


def test_unreferenced_claim_canonicalization_rebuilds_answer_from_retained_claims() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "不应保留的无证据结论。\n64k 是旧版限制，128k 是当前限制。",
            "material_claims": [
                {"text": "不应保留的无证据结论。"},
                {
                    "text": "64k 是旧版限制，128k 是当前限制。",
                    "citation_binding_ids": ["citation-current", "citation-historical"],
                },
                {"text": "另一个不应保留的无证据结论。"},
            ],
        }
    )
    evidence = [
        {
            "citation_binding_id": "citation-current",
            "chunk_id": "models:current",
            "source_locator_hash": "a" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "current",
        },
        {
            "citation_binding_id": "citation-historical",
            "chunk_id": "models:historical",
            "source_locator_hash": "b" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "historical",
        },
    ]

    result = canonicalize_unreferenced_provider_claims(
        synthesis=synthesis,
        evidence=evidence,
        observations=[],
        require_business_source=False,
        required_knowledge_groups=("current", "historical"),
        required_answer_markers=("64k", "128k"),
    )

    assert result is not None
    canonical, bound, indices = result
    assert indices == (0, 2)
    assert canonical.answer == "64k 是旧版限制，128k 是当前限制。"
    assert [claim.text for claim in canonical.material_claims] == [canonical.answer]
    assert bound.answer == canonical.answer
    assert {item.citation_binding_id for item in bound.knowledge_citations} == {
        "citation-current",
        "citation-historical",
    }


@pytest.mark.parametrize(
    "synthesis",
    [
        ProviderBoundEvidenceSynthesis.model_validate(
            {
                "answer": "全部内容都没有证据。",
                "material_claims": [{"text": "全部内容都没有证据。"}],
            }
        ),
        ProviderBoundEvidenceSynthesis.model_validate(
            {
                "answer": "一个无证据 Claim 与一个未知引用 Claim。",
                "material_claims": [
                    {"text": "一个无证据 Claim。"},
                    {
                        "text": "一个未知引用 Claim。",
                        "citation_binding_ids": ["citation-unknown"],
                    },
                ],
            }
        ),
    ],
)
def test_unreferenced_claim_canonicalization_rejects_all_pruned_or_mixed_errors(
    synthesis: ProviderBoundEvidenceSynthesis,
) -> None:
    assert (
        canonicalize_unreferenced_provider_claims(
            synthesis=synthesis,
            evidence=[],
            observations=[],
            require_knowledge_source=False,
            require_business_source=False,
        )
        is None
    )


def test_unreferenced_claim_canonicalization_revalidates_markers_after_pruning() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "旧版为 64k，当前为 128k。",
            "material_claims": [
                {"text": "当前为 128k。"},
                {
                    "text": "旧版为 64k。",
                    "citation_binding_ids": ["citation-current", "citation-historical"],
                },
            ],
        }
    )
    evidence = [
        {
            "citation_binding_id": "citation-current",
            "chunk_id": "models:current",
            "source_locator_hash": "a" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "current",
        },
        {
            "citation_binding_id": "citation-historical",
            "chunk_id": "models:historical",
            "source_locator_hash": "b" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "historical",
        },
    ]

    assert (
        canonicalize_unreferenced_provider_claims(
            synthesis=synthesis,
            evidence=evidence,
            observations=[],
            require_business_source=False,
            required_knowledge_groups=("current", "historical"),
            required_answer_markers=("64k", "128k"),
        )
        is None
    )


def test_unreferenced_claim_canonicalization_rejects_overlong_derived_answer() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "原始回答在长度范围内。",
            "material_claims": [
                {"text": "无证据 Claim。"},
                *[
                    {
                        "text": str(index) + ("字" * 999),
                        "citation_binding_ids": ["citation-current"],
                    }
                    for index in range(5)
                ],
            ],
        }
    )

    assert (
        canonicalize_unreferenced_provider_claims(
            synthesis=synthesis,
            evidence=[
                {
                    "citation_binding_id": "citation-current",
                    "chunk_id": "models:current",
                    "source_locator_hash": "a" * 64,
                    "supporting_span_eligible": True,
                }
            ],
            observations=[],
            require_business_source=False,
        )
        is None
    )


def test_synthesis_binding_supports_a_knowledge_only_answer_contract() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "重复扣费通常需要账单编号、金额与扣费时间。",
            "material_claims": [
                {
                    "text": "重复扣费通常需要账单编号、金额与扣费时间。",
                    "citation_binding_ids": ["citation-policy"],
                }
            ],
        }
    )
    evidence = [
        {
            "citation_binding_id": "citation-policy",
            "chunk_id": "billing-refunds-v3:c001",
            "source_locator_hash": "a" * 64,
            "supporting_span_eligible": True,
        }
    ]

    assert "material_claims:business_source_required" in (
        provider_synthesis_binding_error_paths(
            synthesis=synthesis,
            evidence=evidence,
            observations=[],
        )
    )

    bound = bind_provider_synthesis(
        synthesis=synthesis,
        evidence=evidence,
        observations=[],
        require_business_source=False,
    )
    contract = provider_synthesis_reference_contract(
        evidence=evidence,
        observations=[],
        require_business_source=False,
    )

    assert bound.knowledge_chunk_ids == ["billing-refunds-v3:c001"]
    assert bound.business_source_ids == []
    assert contract["global_rules"] == [
        "at least one material claim must use an allowed citation_binding_id"
    ]


def test_comparison_synthesis_requires_both_groups_and_material_markers() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "当前版本与历史版本的限制不同。",
            "material_claims": [
                {
                    "text": "当前版本与历史版本的限制不同。",
                    "citation_binding_ids": ["citation-current"],
                }
            ],
        }
    )
    evidence = [
        {
            "citation_binding_id": "citation-current",
            "chunk_id": "models:current",
            "source_locator_hash": "a" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "current",
        },
        {
            "citation_binding_id": "citation-historical",
            "chunk_id": "models:historical",
            "source_locator_hash": "b" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "historical",
        },
    ]

    errors = provider_synthesis_binding_error_paths(
        synthesis=synthesis,
        evidence=evidence,
        observations=[],
        require_business_source=False,
        required_knowledge_groups=("current", "historical"),
        required_answer_markers=("64k", "128k"),
    )
    contract = provider_synthesis_reference_contract(
        evidence=evidence,
        observations=[],
        require_business_source=False,
        required_knowledge_groups=("current", "historical"),
        required_answer_markers=("64k", "128k"),
    )

    assert "material_claims:citation_group_required:historical" in errors
    assert "material_claims:required_marker_missing:64k" in errors
    assert "material_claims:required_marker_missing:128k" in errors
    assert contract["allowed_citation_binding_ids_by_group"] == {
        "current": ["citation-current"],
        "historical": ["citation-historical"],
    }
    assert contract["required_knowledge_groups"] == ["current", "historical"]
    assert contract["required_answer_markers"] == ["64k", "128k"]
    assert "cite at least one ID from each matching group" in contract["contract_instruction"]


def test_comparison_synthesis_binds_only_when_public_claims_cover_contract() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "上下文上限从 64k 提升到 128k。",
            "material_claims": [
                {
                    "text": "上下文上限从 64k 提升到 128k。",
                    "citation_binding_ids": [
                        "citation-current",
                        "citation-historical",
                    ],
                }
            ],
        }
    )
    evidence = [
        {
            "citation_binding_id": "citation-current",
            "chunk_id": "models:current",
            "source_locator_hash": "a" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "current",
        },
        {
            "citation_binding_id": "citation-historical",
            "chunk_id": "models:historical",
            "source_locator_hash": "b" * 64,
            "supporting_span_eligible": True,
            "evidence_group": "historical",
        },
    ]

    bound = bind_provider_synthesis(
        synthesis=synthesis,
        evidence=evidence,
        observations=[],
        require_business_source=False,
        required_knowledge_groups=("current", "historical"),
        required_answer_markers=("64k", "128k"),
    )

    assert bound.knowledge_chunk_ids == ["models:current", "models:historical"]
    assert {item.citation_binding_id for item in bound.knowledge_citations} == {
        "citation-current",
        "citation-historical",
    }


def test_synthesis_binding_supports_a_business_only_answer_contract() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "当前账户状态正常。",
            "material_claims": [
                {
                    "text": "当前账户状态正常。",
                    "observation_source_ids": ["account-current"],
                }
            ],
        }
    )
    observations = [
        {
            "tool_name": "query_customer_context",
            "status": "ok",
            "source_refs": [{"source_id": "account-current"}],
        }
    ]

    bound = bind_provider_synthesis(
        synthesis=synthesis,
        evidence=[],
        observations=observations,
        require_knowledge_source=False,
    )

    assert bound.knowledge_chunk_ids == []
    assert bound.business_source_ids == ["account-current"]


def test_provider_material_claim_schema_exposes_nonempty_reference_union() -> None:
    schema = ProviderBoundEvidenceSynthesis.model_json_schema()
    claim_schema = schema["$defs"]["ProviderMaterialClaim"]

    assert claim_schema["anyOf"] == [
        {"properties": {"citation_binding_ids": {"minItems": 1}}},
        {"properties": {"observation_source_ids": {"minItems": 1}}},
    ]


def test_runtime_derives_all_redundant_evidence_identity_from_claim_references() -> None:
    synthesis = ProviderBoundEvidenceSynthesis.model_validate(
        {
            "answer": "已核验当前账单与退款政策。",
            "material_claims": [
                {
                    "text": "当前账单符合退款政策。",
                    "citation_binding_ids": [
                        "citation-policy",
                        "citation-policy",
                    ],
                    "observation_source_ids": [
                        "record-current",
                        "record-current",
                    ],
                }
            ],
        }
    )

    bound = bind_provider_synthesis(
        synthesis=synthesis,
        evidence=[
            {
                "citation_binding_id": "citation-policy",
                "chunk_id": "billing-refunds-v3:c001",
                "source_locator_hash": "a" * 64,
                "supporting_span_eligible": True,
            }
        ],
        observations=[
            {
                "tool_name": "query_billing_record",
                "status": "ok",
                "source_refs": [{"source_id": "record-current"}],
            }
        ],
    )

    assert bound.knowledge_chunk_ids == ["billing-refunds-v3:c001"]
    assert [item.citation_binding_id for item in bound.knowledge_citations] == ["citation-policy"]
    assert bound.business_source_ids == ["record-current"]
    assert bound.material_claims[0].knowledge_locator_hashes == ["a" * 64]


@pytest.mark.parametrize("action_type", tuple(_CASES))
def test_three_actions_share_one_deterministic_candidate_assembler(
    action_type: str,
) -> None:
    case = _CASES[action_type]
    admitted = _admitted(action_type, case)
    observations = _nominal_observations(action_type, case, admitted)
    evidence = _knowledge_data(case)["evidence"][0]
    citation_id = f"citation-{action_type}"
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        citation_bindings=[
            ContextCitationBinding(
                citation_binding_id=citation_id,
                provider_attempt_id="provider-attempt-assembler",
                evidence_id=str(evidence["evidence_id"]),
                document_id=str(evidence["document_id"]),
                chunk_id=str(evidence["chunk_id"]),
                content_hash=str(evidence["content_hash"]),
                locator_hash=str(evidence["source_locator"]["locator_hash"]),
            )
        ],
        provider_attempt_id="provider-attempt-assembler",
        now=NOW,
    )
    business_source_ids = [
        str(source["source_id"])
        for observation in observations
        if observation["tool_name"] != "search_knowledge"
        for source in observation["source_refs"]
    ]
    synthesis = BoundEvidenceSynthesis(
        answer="已依据当前资源与政策完成核验。",
        knowledge_chunk_ids=[str(evidence["chunk_id"])],
        knowledge_citations=[{"citation_binding_id": citation_id}],
        business_source_ids=business_source_ids,
        material_claims=[
            {
                "text": "已依据当前资源与政策完成核验。",
                "citation_binding_ids": [citation_id],
                "observation_source_ids": business_source_ids,
            }
        ],
    )

    candidate = assemble_action_candidate(
        action_spec=get_action_spec(action_type),  # type: ignore[arg-type]
        admission=admitted,
        ledger=ledger,
        observations=observations,
        synthesis=synthesis,
    )

    assert (
        candidate.action
        == get_action_spec(  # type: ignore[arg-type]
            action_type
        ).proposal_action
    )
    assert candidate.proposed_arguments
    eligibility = evaluate_action_candidate_eligibility(
        candidate=candidate,
        admission_payload=admitted.model_dump(mode="json"),
        ledger_payload=ledger.model_dump(mode="json"),
        observations=observations,
        now=NOW,
    )
    assert eligibility.eligible is True
    assert eligibility.action_type == action_type
    assert eligibility.trusted_arguments == candidate.proposed_arguments


def test_proposal_eligibility_revalidates_bound_observation_freshness() -> None:
    case = _CASES["refund"]
    admitted = _admitted("refund", case)
    observations = _nominal_observations("refund", case, admitted)
    evidence = _knowledge_data(case)["evidence"][0]
    citation_id = "citation-refund-stale"
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        citation_bindings=[
            ContextCitationBinding(
                citation_binding_id=citation_id,
                provider_attempt_id="provider-attempt-stale",
                evidence_id=str(evidence["evidence_id"]),
                document_id=str(evidence["document_id"]),
                chunk_id=str(evidence["chunk_id"]),
                content_hash=str(evidence["content_hash"]),
                locator_hash=str(evidence["source_locator"]["locator_hash"]),
            )
        ],
        provider_attempt_id="provider-attempt-stale",
        now=NOW,
    )
    business_source = str(observations[0]["source_refs"][0]["source_id"])
    candidate = assemble_action_candidate(
        action_spec=get_action_spec("refund"),
        admission=admitted,
        ledger=ledger,
        observations=observations,
        synthesis=BoundEvidenceSynthesis(
            answer="已依据当前资源与政策完成核验。",
            knowledge_chunk_ids=[str(evidence["chunk_id"])],
            knowledge_citations=[{"citation_binding_id": citation_id}],
            business_source_ids=[business_source],
            material_claims=[
                {
                    "text": "已依据当前资源与政策完成核验。",
                    "citation_binding_ids": [citation_id],
                    "observation_source_ids": [business_source],
                }
            ],
        ),
    )

    eligibility = evaluate_action_candidate_eligibility(
        candidate=candidate,
        admission_payload=admitted.model_dump(mode="json"),
        ledger_payload=ledger.model_dump(mode="json"),
        observations=observations,
        now=NOW + timedelta(minutes=6),
    )

    assert eligibility.eligible is False
    assert eligibility.error_code == "proposal_resource_observation_stale"


def test_proposal_eligibility_rejects_non_typed_entitlement_target() -> None:
    case = _CASES["entitlement_change"]
    admitted = _admitted("entitlement_change", case)
    observations = _nominal_observations("entitlement_change", case, admitted)
    evidence = _knowledge_data(case)["evidence"][0]
    citation_id = "citation-entitlement-invalid-target"
    ledger = evaluate_action_obligations(
        action_spec=get_action_spec("entitlement_change"),
        admission=admitted,
        observations=observations,
        run_id=RUN_ID,
        citation_bindings=[
            ContextCitationBinding(
                citation_binding_id=citation_id,
                provider_attempt_id="provider-attempt-invalid-target",
                evidence_id=str(evidence["evidence_id"]),
                document_id=str(evidence["document_id"]),
                chunk_id=str(evidence["chunk_id"]),
                content_hash=str(evidence["content_hash"]),
                locator_hash=str(evidence["source_locator"]["locator_hash"]),
            )
        ],
        provider_attempt_id="provider-attempt-invalid-target",
        now=NOW,
    )
    business_source = str(observations[0]["source_refs"][0]["source_id"])
    candidate = assemble_action_candidate(
        action_spec=get_action_spec("entitlement_change"),
        admission=admitted,
        ledger=ledger,
        observations=observations,
        synthesis=BoundEvidenceSynthesis(
            answer="已依据当前资源与政策完成核验。",
            knowledge_chunk_ids=[str(evidence["chunk_id"])],
            knowledge_citations=[{"citation_binding_id": citation_id}],
            business_source_ids=[business_source],
            material_claims=[
                {
                    "text": "已依据当前资源与政策完成核验。",
                    "citation_binding_ids": [citation_id],
                    "observation_source_ids": [business_source],
                }
            ],
        ),
    )
    invalid_arguments = dict(candidate.proposed_arguments)
    invalid_arguments["target"] = {"unsupported_quota": 40}

    eligibility = evaluate_action_candidate_eligibility(
        candidate=candidate.model_copy(update={"proposed_arguments": invalid_arguments}),
        admission_payload=admitted.model_dump(mode="json"),
        ledger_payload=ledger.model_dump(mode="json"),
        observations=observations,
        now=NOW,
    )

    assert eligibility.eligible is False
    assert eligibility.error_code == "proposal_argument_schema_invalid"
