from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from supportguard.agent.context import build_trusted_task_state
from supportguard.agent.conversation_semantics import resolve_action_state_query
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.nodes.intake import IntakeNodes
from supportguard.agent.schemas import Classification
from supportguard.providers.base import ProviderCallResult
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway

_STATUS_FACTS: dict[str, tuple[str, str, str, bool, list[str]]] = {
    "pending": ("pending", "not_started", "approval_pending", True, ["withdraw"]),
    "approved": (
        "approved",
        "queued",
        "approval_approved_awaiting_execution",
        False,
        [],
    ),
    "executing": (
        "approved",
        "in_progress",
        "action_execution_in_progress",
        False,
        [],
    ),
    "verification_pending": (
        "approved",
        "verification_pending",
        "action_execution_verification_pending",
        False,
        [],
    ),
    "executed": (
        "executed",
        "succeeded",
        "action_execution_confirmed",
        False,
        [],
    ),
    "rejected": (
        "rejected",
        "not_executed",
        "approval_rejected_no_effect",
        False,
        [],
    ),
    "stale": (
        "stale",
        "not_executed",
        "action_requires_fresh_verification",
        False,
        [],
    ),
    "withdrawn": (
        "withdrawn",
        "not_executed",
        "approval_withdrawn_no_effect",
        False,
        [],
    ),
    "failed": (
        "failed",
        "failed",
        "action_failed_confirmed_no_effect",
        False,
        [],
    ),
    "manual_takeover_legacy": (
        "manual_takeover",
        "legacy_stopped",
        "legacy_manual_takeover_no_operator",
        False,
        [],
    ),
}


def _action_state(
    status: str,
    *,
    approval_id: str = "approval_current",
    action_type: str = "refund",
    resource_id: str = "bill_invoice_42",
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    approval_status, execution_state, reason_code, actionable, allowed = _STATUS_FACTS[status]
    return {
        "schema_version": "conversation-action-state.v1",
        "approval_id": approval_id,
        "origin_run_id": "run_origin",
        "origin_turn_id": "turn_origin",
        "action_type": action_type,
        "resource_type": {
            "refund": "billing_record",
            "api_key_revocation": "api_key",
            "entitlement_change": "subscription",
        }[action_type],
        "resource_id": resource_id,
        "resource_version": 2,
        "approval_status": approval_status,
        "projection_status": status,
        "status_version": 3,
        "actionable": actionable,
        "allowed_customer_actions": allowed,
        "decision_class": {
            "rejected": "reject",
            "withdrawn": "customer_withdrawal",
            "manual_takeover_legacy": "legacy_manual_takeover",
        }.get(status, "none"),
        "customer_safe_reason_code": reason_code,
        "execution_state": execution_state,
        "business_action_id": (
            "business_action_42" if status in {"executing", "executed"} else None
        ),
        "updated_at": (updated_at or datetime(2026, 7, 28, tzinfo=UTC)).isoformat(),
        "source_event_id": "event_action_42",
        "source_event_hash": "a" * 64,
        "grants_action_authority": False,
    }


class _AdversarialFakeClassificationFixture(DeterministicFakeProvider):
    """Honest fake that tries to widen authority in its advisory classification."""

    def __init__(self) -> None:
        super().__init__()
        self.classification_calls = 0

    async def generate(self, **kwargs: Any) -> Any:
        result = await super().generate(**kwargs)
        if kwargs["output_schema"] is not Classification:
            return result
        self.classification_calls += 1
        # Deliberately return an authority-seeking advisory classification.
        # The deterministic action-state adapter must narrow it back to a
        # read-only status/reason inquiry.
        return ProviderCallResult(
            output=Classification(
                issue_type="billing_refund",
                risk="high",
                policy_boundary="allowed",
                requested_action="refund",
                requested_concurrency_limit=None,
                needs_realtime_facts=True,
                support_subject="customer_problem",
                rationale="The provider suggests a new refund action.",
            ),
            attempts=result.attempts,
            usage=result.usage,
            trace_metadata=result.trace_metadata,
            transport=result.transport,
            transport_attempts=result.transport_attempts,
        )


class _ProhibitedRepeatClassificationFixture(DeterministicFakeProvider):
    """Model a Provider that correctly marks a repeated effect as prohibited."""

    async def generate(self, **kwargs: Any) -> Any:
        result = await super().generate(**kwargs)
        if kwargs["output_schema"] is not Classification:
            return result
        return ProviderCallResult(
            output=Classification(
                issue_type="billing_refund",
                risk="high",
                policy_boundary="prohibited",
                requested_action="refund",
                requested_concurrency_limit=None,
                needs_realtime_facts=False,
                support_subject="customer_problem",
                rationale="The same refund has already executed and cannot be repeated.",
            ),
            attempts=result.attempts,
            usage=result.usage,
            trace_metadata=result.trace_metadata,
            transport=result.transport,
            transport_attempts=result.transport_attempts,
        )


class _NoNewEffectRepeatClassificationFixture(DeterministicFakeProvider):
    """Match a safe real Provider that declines to plan a duplicate effect."""

    async def generate(self, **kwargs: Any) -> Any:
        result = await super().generate(**kwargs)
        if kwargs["output_schema"] is not Classification:
            return result
        return ProviderCallResult(
            output=Classification(
                issue_type="billing_refund",
                risk="high",
                policy_boundary="allowed",
                requested_action="none",
                requested_concurrency_limit=None,
                needs_realtime_facts=False,
                support_subject="customer_problem",
                rationale=("The same refund already executed, so no new effect is requested."),
            ),
            attempts=result.attempts,
            usage=result.usage,
            trace_metadata=result.trace_metadata,
            transport=result.transport,
            transport_attempts=result.transport_attempts,
        )


def _graph(
    provider: DeterministicFakeProvider | None = None,
) -> SupportGraph:
    return SupportGraph(
        provider=provider or DeterministicFakeProvider(),
        retrieval=None,
        gateway=ToolGateway(None),
    )


def _initial_state(message: str, action: dict[str, Any]) -> AgentState:
    return AgentState(
        tenant_id="tenant_customer",
        ticket_id="ticket_current",
        customer_id="customer_current",
        run_id="run_follow_up",
        trace_id="trace_follow_up",
        user_message=message,
        current_actions=[action],
        classification_context=[
            {
                "role": "customer",
                "content": "请处理这项申请。",
                "message_id": "message_action_request",
            },
            {
                "role": "action",
                "content": "申请状态已更新。",
                "message_id": "message_action_update",
                "approval_id": action["approval_id"],
            },
        ],
    )


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        ("pending", "这项申请现在是什么状态？", "等待独立审批"),
        ("approved", "这个申请通过了吗？", "已批准”不等于“已完成"),
        ("executing", "现在执行到哪了？", "正在安全执行"),
        (
            "verification_pending",
            "执行结果现在怎么样了？",
            "执行结果暂时还无法确认",
        ),
        ("executed", "这笔退款执行了吗？", "已确认执行完成"),
        ("rejected", "为什么拒绝了？", "不会展示审批者的内部备注"),
        ("stale", "这项申请为什么失效了？", "资源事实或版本已经变化"),
        ("withdrawn", "这项申请撤回了吗？", "已由客户撤回"),
        ("failed", "这个操作为什么失败了？", "未产生业务效果"),
        (
            "manual_takeover_legacy",
            "转入人工是什么意思？",
            "当前版本没有人工坐席",
        ),
    ],
)
@pytest.mark.asyncio
async def test_action_state_follow_up_uses_provider_semantics_and_canonical_truth(
    status: str,
    message: str,
    expected: str,
) -> None:
    output = await _graph().run(_initial_state(message, _action_state(status)))

    assert output["classification"]["requested_action"] == "none"
    assert output["action_admission"]["status"] == "none"
    assert output["action_state_query"]["grants_action_authority"] is False
    assert output["llm_calls"] == 1
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert "bill_invoice_42" in output["final"]["answer"]
    assert expected in output["final"]["answer"]
    assert output["final"]["terminal_state"] == "resolved"


@pytest.mark.asyncio
async def test_action_state_provider_advice_cannot_grant_action_authority() -> None:
    provider = _AdversarialFakeClassificationFixture()

    output = await _graph(provider).run(_initial_state("为什么拒绝了？", _action_state("rejected")))

    assert provider.mode == "fake"
    assert provider.model == "deterministic-fake"
    assert provider.tool_call_mode == "native_fixture"
    assert provider.classification_calls == 1
    assert output["classification"]["requested_action"] == "none"
    assert output["classification"]["needs_realtime_facts"] is False
    assert output["action_admission"]["status"] == "none"
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output["final"]["terminal_state"] == "resolved"
    assert "已被独立审批者拒绝" in output["final"]["answer"]


@pytest.mark.asyncio
async def test_executed_action_replay_converges_before_generic_prohibited_copy() -> None:
    output = await _graph(_ProhibitedRepeatClassificationFixture()).run(
        _initial_state(
            "请再给 bill_invoice_42 申请一次退款。",
            _action_state("executed"),
        )
    )

    assert output["action_state_query"]["query_kind"] == "repeat_request"
    assert output["action_state_query"]["reason_code"] == ("executed_action_not_repeatable")
    assert output["classification"]["policy_boundary"] == "allowed"
    assert output["classification"]["requested_action"] == "none"
    assert output["action_admission"]["status"] == "none"
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output["final"]["terminal_state"] == "resolved"
    assert "已确认执行完成" in output["final"]["answer"]
    assert "不需要再提交同一申请" in output["final"]["answer"]
    assert "其他客户" not in output["final"]["answer"]


@pytest.mark.asyncio
async def test_executed_replay_converges_when_provider_plans_no_new_effect() -> None:
    output = await _graph(_NoNewEffectRepeatClassificationFixture()).run(
        _initial_state(
            "请再给 bill_invoice_42 申请一次退款。",
            _action_state("executed"),
        )
    )

    assert output["action_state_query"]["query_kind"] == "repeat_request"
    assert output["action_state_query"]["reason_code"] == ("executed_action_not_repeatable")
    assert output["classification"]["requested_action"] == "none"
    assert output["action_admission"]["status"] == "none"
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output["final"]["terminal_state"] == "resolved"
    assert "已确认执行完成" in output["final"]["answer"]
    assert "不需要再提交同一申请" in output["final"]["answer"]


@pytest.mark.parametrize(
    ("status", "action_type", "message", "expected_reason"),
    [
        (
            "pending",
            "refund",
            "请再给 bill_invoice_42 申请一次退款。",
            "existing_action_in_progress",
        ),
        (
            "executed",
            "api_key_revocation",
            "请再次撤销 key_primary_42。",
            "executed_action_not_repeatable",
        ),
    ],
)
def test_existing_nonrepeatable_action_resolution_is_generic(
    status: str,
    action_type: str,
    message: str,
    expected_reason: str,
) -> None:
    query = AgentRuntimeServices._resolve_existing_action_replay(
        message,
        Classification(
            issue_type=("billing_refund" if action_type == "refund" else "credential_security"),
            risk="high",
            policy_boundary="prohibited",
            requested_action=action_type,
            requested_concurrency_limit=None,
            needs_realtime_facts=False,
            support_subject="customer_problem",
            rationale="Repeated effect.",
        ),
        [
            _action_state(
                status,
                action_type=action_type,
                resource_id=("bill_invoice_42" if action_type == "refund" else "key_primary_42"),
            )
        ],
    )

    assert query is not None
    assert query["reason_code"] == expected_reason
    assert query["grants_action_authority"] is False


@pytest.mark.parametrize(
    ("status", "action_type", "message", "resource_id", "expected_reason"),
    [
        (
            "executed",
            "refund",
            "请再给 bill_invoice_42 申请一次退款。",
            "bill_invoice_42",
            "executed_action_not_repeatable",
        ),
        (
            "pending",
            "api_key_revocation",
            "请再次撤销 key_primary_42。",
            "key_primary_42",
            "existing_action_in_progress",
        ),
    ],
)
def test_provider_none_replay_uses_explicit_nonrepeatable_action_only(
    status: str,
    action_type: str,
    message: str,
    resource_id: str,
    expected_reason: str,
) -> None:
    query = AgentRuntimeServices._resolve_existing_action_replay(
        message,
        Classification(
            issue_type=("billing_refund" if action_type == "refund" else "credential_security"),
            risk="high",
            policy_boundary="allowed",
            requested_action="none",
            requested_concurrency_limit=None,
            needs_realtime_facts=False,
            support_subject="customer_problem",
            rationale="No new effect should be planned for the repeated action.",
        ),
        [
            _action_state(
                status,
                action_type=action_type,
                resource_id=resource_id,
            )
        ],
    )

    assert query is not None
    assert query["reason_code"] == expected_reason
    assert query["grants_action_authority"] is False


def test_provider_none_ambiguous_action_does_not_converge_by_resource_alone() -> None:
    query = AgentRuntimeServices._resolve_existing_action_replay(
        "请给 bill_invoice_42 退款，同时撤销 key_primary_42。",
        Classification(
            issue_type="billing_refund",
            risk="high",
            policy_boundary="allowed",
            requested_action="none",
            requested_concurrency_limit=None,
            needs_realtime_facts=False,
            support_subject="customer_problem",
            rationale="Multiple actions require clarification.",
        ),
        [_action_state("executed")],
    )

    assert query is None


def test_executed_entitlement_with_new_target_is_not_collapsed_as_replay() -> None:
    query = AgentRuntimeServices._resolve_existing_action_replay(
        "请把 sub_primary_42 的并发调整为 64。",
        Classification(
            issue_type="entitlement_change",
            risk="high",
            policy_boundary="allowed",
            requested_action="entitlement_change",
            requested_concurrency_limit=64,
            needs_realtime_facts=True,
            support_subject="customer_problem",
            rationale="A new explicit entitlement target was requested.",
        ),
        [
            _action_state(
                "executed",
                action_type="entitlement_change",
                resource_id="sub_primary_42",
            )
        ],
    )

    assert query is None


def test_provider_none_does_not_collapse_new_entitlement_target_as_replay() -> None:
    query = AgentRuntimeServices._resolve_existing_action_replay(
        "请把 sub_primary_42 的并发调整为 64。",
        Classification(
            issue_type="entitlement_change",
            risk="high",
            policy_boundary="allowed",
            requested_action="none",
            requested_concurrency_limit=None,
            needs_realtime_facts=False,
            support_subject="customer_problem",
            rationale="No new effect planned.",
        ),
        [
            _action_state(
                "executed",
                action_type="entitlement_change",
                resource_id="sub_primary_42",
            )
        ],
    )

    assert query is None


def test_action_state_query_prefers_explicit_resource_and_matching_terminal() -> None:
    actions = [
        _action_state(
            "pending",
            approval_id="approval_newer",
            resource_id="bill_invoice_newer",
            updated_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        ),
        _action_state(
            "rejected",
            approval_id="approval_target",
            resource_id="bill_invoice_target",
            updated_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        ),
    ]

    query = resolve_action_state_query(
        "bill_invoice_target 为什么被拒绝了？",
        actions,
    )

    assert query is not None
    assert query["approval_id"] == "approval_target"
    assert query["query_kind"] == "reason"
    assert query["grants_action_authority"] is False


def test_action_state_resource_match_is_exact_not_substring() -> None:
    actions = [
        _action_state(
            "rejected",
            approval_id="approval_bill_1",
            resource_id="bill_1",
        ),
        _action_state(
            "rejected",
            approval_id="approval_bill_10",
            resource_id="bill_10",
        ),
    ]

    query = resolve_action_state_query(
        "bill_10 为什么被拒绝？",
        actions,
    )

    assert query is not None
    assert query["resolution"] == "selected"
    assert query["approval_id"] == "approval_bill_10"


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        (
            "bill_missing 的退款申请状态怎么样了？",
            "resource_not_in_current_action_state",
        ),
        (
            "bill_1 的 API Key 撤销申请状态怎么样了？",
            "resource_action_type_mismatch",
        ),
        (
            "key_1 的退款申请为什么失败了？",
            "resource_action_type_mismatch",
        ),
    ],
)
def test_explicit_unknown_or_cross_type_resource_never_falls_back(
    message: str,
    expected_reason: str,
) -> None:
    actions = [
        _action_state(
            "rejected",
            approval_id="approval_bill_1",
            action_type="refund",
            resource_id="bill_1",
        ),
        _action_state(
            "failed",
            approval_id="approval_key_1",
            action_type="api_key_revocation",
            resource_id="key_1",
        ),
    ]

    query = resolve_action_state_query(message, actions)

    assert query is not None
    assert query["resolution"] == "unresolved"
    assert query["reason_code"] == expected_reason
    assert "approval_id" not in query
    assert query["grants_action_authority"] is False


@pytest.mark.asyncio
async def test_unknown_explicit_resource_gets_safe_clarification_without_old_action() -> None:
    action = _action_state(
        "rejected",
        approval_id="approval_bill_1",
        resource_id="bill_1",
    )

    output = await _graph().run(_initial_state("bill_missing 的退款申请为什么被拒绝了？", action))

    assert output["action_state_query"]["resolution"] == "unresolved"
    assert "approval_id" not in output["action_state_query"]
    assert "bill_missing" in output["final"]["answer"]
    assert "bill_1" not in output["final"]["answer"]
    assert "不会用其他历史申请代替回答" in output["final"]["answer"]
    assert "没有创建审批" in output["final"]["answer"]
    assert output["classification"]["requested_action"] == "none"
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0


def test_explicit_resource_selection_is_order_invariant() -> None:
    actions = [
        _action_state(
            "rejected",
            approval_id="approval_bill_1",
            resource_id="bill_1",
        ),
        _action_state(
            "failed",
            approval_id="approval_bill_10",
            resource_id="bill_10",
        ),
    ]

    forward = resolve_action_state_query(
        "bill_10 为什么失败了？",
        actions,
    )
    reversed_order = resolve_action_state_query(
        "bill_10 为什么失败了？",
        list(reversed(actions)),
    )

    assert forward == reversed_order
    assert forward is not None
    assert forward["approval_id"] == "approval_bill_10"


def test_bare_action_reason_never_uses_ticket_wide_unique_old_action() -> None:
    query = resolve_action_state_query(
        "为什么拒绝了？",
        [_action_state("rejected", approval_id="approval_old", resource_id="bill_old")],
    )

    assert query is not None
    assert query["resolution"] == "unresolved"
    assert query["reason_code"] == "action_referent_missing"
    assert "approval_id" not in query


def test_bare_action_reason_uses_adjacent_action_message_referent_only() -> None:
    actions = [
        _action_state(
            "rejected",
            approval_id="approval_old",
            resource_id="bill_old",
        ),
        _action_state(
            "failed",
            approval_id="approval_adjacent",
            resource_id="bill_adjacent",
        ),
    ]
    context = [
        {
            "role": "action",
            "message_id": "message_action_adjacent",
            "approval_id": "approval_adjacent",
            "content": "最近一项申请执行失败。",
        }
    ]

    query = resolve_action_state_query(
        "为什么失败了？",
        actions,
        recent_action_approval_id=IntakeNodes._recent_action_message_approval_id(context),
    )

    assert query is not None
    assert query["resolution"] == "selected"
    assert query["approval_id"] == "approval_adjacent"


@pytest.mark.asyncio
async def test_natural_rejection_follow_up_uses_adjacent_api_key_action_without_leak() -> None:
    action = _action_state(
        "rejected",
        approval_id="approval_key_rejected",
        action_type="api_key_revocation",
        resource_id="key_customer_secret_42",
    )

    output = await _graph().run(_initial_state("为什么撤销申请没有通过？", action))

    assert output["action_state_query"] == {
        "schema_version": "conversation-action-state-query.v1",
        "resolution": "selected",
        "approval_id": "approval_key_rejected",
        "query_kind": "reason",
        "grants_action_authority": False,
    }
    answer = output["final"]["answer"]
    assert "已被独立审批者拒绝" in answer
    assert "本次未撤销该 API Key" in answer
    assert "未执行任何业务变更" in answer
    assert "key_customer_secret_42" not in answer
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0


@pytest.mark.parametrize(
    "message",
    [
        "那我现在还能继续查询账户状态吗？",
        "操作失败后，我仍然可以继续咨询吗？",
        "Can I still check my account status?",
    ],
)
def test_continuity_query_resolves_only_to_adjacent_action(
    message: str,
) -> None:
    query = resolve_action_state_query(
        message,
        [_action_state("failed")],
        recent_action_approval_id="approval_current",
    )

    assert query == {
        "schema_version": "conversation-action-state-query.v1",
        "resolution": "selected",
        "approval_id": "approval_current",
        "query_kind": "continuity",
        "grants_action_authority": False,
    }


@pytest.mark.asyncio
async def test_continuity_query_uses_trusted_state_without_fake_rag_evidence() -> None:
    output = await _graph().run(
        _initial_state(
            "那我现在还能继续查询账户状态吗？",
            _action_state("failed"),
        )
    )

    assert output["action_state_query"]["query_kind"] == "continuity"
    assert output["classification"]["requested_action"] == "none"
    assert output["classification"]["needs_realtime_facts"] is False
    assert output["action_admission"]["status"] == "none"
    assert output["llm_calls"] == 1
    assert output["tool_rounds"] == 0
    assert output["tool_attempts"] == 0
    assert output["evidence"] == []
    assert output["tool_observations"] == []
    assert output["final"]["terminal_state"] == "resolved"
    assert "可以" in output["final"]["answer"]
    assert "继续查询账户状态" in output["final"]["answer"]
    assert "执行失败" in output["final"]["answer"]
    assert "不会重新提交" in output["final"]["answer"]


def test_continuity_query_does_not_bind_ticket_wide_old_action() -> None:
    query = resolve_action_state_query(
        "那我现在还能继续查询账户状态吗？",
        [_action_state("failed", approval_id="approval_old")],
    )

    assert query is None


def test_explicit_repeat_action_is_not_downgraded_to_continuity_query() -> None:
    assert (
        resolve_action_state_query(
            "那我现在还能继续申请退款吗？",
            [_action_state("failed")],
            recent_action_approval_id="approval_current",
        )
        is None
    )


@pytest.mark.parametrize("phrase", ["没有通过", "没通过", "未通过"])
def test_rejection_phrasing_resolves_to_adjacent_action(
    phrase: str,
) -> None:
    query = resolve_action_state_query(
        f"为什么这项申请{phrase}？",
        [_action_state("rejected")],
        recent_action_approval_id="approval_current",
    )

    assert query is not None
    assert query["resolution"] == "selected"
    assert query["approval_id"] == "approval_current"
    assert query["query_kind"] == "reason"


def test_intervening_assistant_answer_breaks_bare_action_referent() -> None:
    context = [
        {
            "role": "action",
            "message_id": "message_action_old",
            "approval_id": "approval_old",
            "content": "申请已拒绝。",
        },
        {
            "role": "assistant",
            "message_id": "message_answer_new",
            "content": "429 与并发上限有关。",
        },
    ]

    assert IntakeNodes._recent_action_message_approval_id(context) is None


@pytest.mark.parametrize(
    "message",
    [
        "刚才 API 调用为什么失败了？",
        "之前的 HTTP 请求为什么失败了？",
        "刚才登录为什么失败了？",
    ],
)
def test_generic_failure_does_not_bind_the_only_old_action(message: str) -> None:
    assert (
        resolve_action_state_query(
            message,
            [_action_state("failed", resource_id="bill_old")],
        )
        is None
    )


@pytest.mark.asyncio
async def test_same_action_type_with_multiple_resources_requires_clarification() -> None:
    actions = [
        _action_state(
            "failed",
            approval_id="approval_bill_1",
            resource_id="bill_1",
        ),
        _action_state(
            "failed",
            approval_id="approval_bill_10",
            resource_id="bill_10",
        ),
    ]

    output = await _graph().run(
        _initial_state("这笔退款为什么失败了？", actions[0])
        | {
            "current_actions": actions,
            "classification_context": [
                {
                    "role": "customer",
                    "content": "我们先讨论 429 问题。",
                    "message_id": "message_unrelated_customer",
                },
                {
                    "role": "assistant",
                    "content": "429 与并发上限有关。",
                    "message_id": "message_unrelated_answer",
                },
            ],
        }
    )

    query = output["action_state_query"]
    assert query["resolution"] == "unresolved"
    assert query["reason_code"] == "action_referent_missing"
    assert "approval_id" not in query
    assert "还不能确定你问的是哪一项申请" in output["final"]["answer"]
    assert "完整资源引用" in output["final"]["answer"]
    assert output["classification"]["requested_action"] == "none"
    assert output["llm_calls"] == 1
    assert output["tool_attempts"] == 0


@pytest.mark.parametrize(
    ("message", "status", "expected_kind"),
    [
        ("Why was it rejected?", "rejected", "reason"),
        ("这项怎么会失效？", "stale", "reason"),
        ("What happened to this refund?", "executed", "status"),
        ("这个操作后来怎么样了？", "failed", "progress"),
    ],
)
def test_action_state_query_supports_bilingual_rewrites(
    message: str,
    status: str,
    expected_kind: str,
) -> None:
    query = resolve_action_state_query(
        message,
        [_action_state(status)],
        recent_action_approval_id="approval_current",
    )

    assert query is not None
    assert query["resolution"] == "selected"
    assert query["query_kind"] == expected_kind


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        ("rejected", "为什么拒绝了？", "没有提供可以向你说明的更具体拒绝原因"),
        ("failed", "为什么失败了？", "没有提供可以向你说明的更具体失败原因"),
        ("stale", "为什么失效了？", "资源事实或版本已经变化"),
    ],
)
@pytest.mark.asyncio
async def test_reason_follow_up_uses_safe_reason_class_without_raw_note(
    status: str,
    message: str,
    expected: str,
) -> None:
    output = await _graph().run(_initial_state(message, _action_state(status)))

    answer = output["final"]["answer"]
    assert expected in answer
    assert "raw" not in answer.casefold()
    assert "原始备注：" not in answer
    assert output["tool_attempts"] == 0


@pytest.mark.parametrize(
    "message",
    [
        "退款政策目前支持哪些场景？",
        "atlas-chat 的并发限制是什么？",
        "请重新为 bill_invoice_42 退款",
        "产品功能为什么会失败？",
    ],
)
def test_ordinary_questions_and_new_action_requests_are_not_state_queries(
    message: str,
) -> None:
    assert (
        resolve_action_state_query(
            message,
            [_action_state("rejected")],
        )
        is None
    )


@pytest.mark.asyncio
async def test_ordinary_product_question_still_uses_provider_classification() -> None:
    graph = _graph()
    update = await graph.intake_nodes.classify(
        AgentState(
            tenant_id="tenant_customer",
            ticket_id="ticket_current",
            customer_id="customer_current",
            run_id="run_product_question",
            trace_id="trace_product_question",
            redacted_message="atlas-chat 当前支持 JSON Object 吗？",
            classification_context=[],
            current_actions=[_action_state("pending")],
            llm_calls=0,
        )
    )

    assert update["llm_calls"] == 1
    assert update["classification"]["requested_action"] == "none"
    assert "action_state_query" not in update


def test_current_action_state_enters_trusted_task_state_without_authority() -> None:
    action = _action_state("approved")
    state = AgentState(
        ticket_id="ticket_current",
        customer_id="customer_current",
        classification={
            "issue_type": "billing_refund",
            "risk": "low",
            "policy_boundary": "allowed",
            "support_subject": "customer_problem",
            "requested_action": "none",
            "requested_concurrency_limit": None,
        },
        current_actions=[action],
    )

    trusted = build_trusted_task_state(state)

    assert trusted["current_actions"] == [action]
    assert trusted["current_actions_grant_action_authority"] is False
    assert trusted["current_actions"][0]["grants_action_authority"] is False


def test_active_action_context_uses_canonical_projection_not_history_inference() -> None:
    assert AgentRuntimeServices._has_active_action_context(
        AgentState(current_actions=[_action_state("verification_pending")])
    )
    assert not AgentRuntimeServices._has_active_action_context(
        AgentState(current_actions=[_action_state("executed")])
    )


@pytest.mark.asyncio
async def test_load_history_does_not_recreate_active_action_summaries() -> None:
    update = await _graph().intake_nodes.load_history(
        AgentState(
            tenant_id="tenant_customer",
            ticket_id="ticket_current",
            customer_id="customer_current",
            classification={"issue_type": "billing_refund"},
            current_actions=[_action_state("pending")],
        )
    )

    assert update["relevant_history"] == []
