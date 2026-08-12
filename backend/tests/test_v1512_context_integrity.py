from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.agent.context import (
    ContextAssembler,
    ContextBudget,
    ContextBudgetExceeded,
)
from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.agent.nodes.intake import IntakeNodes
from supportguard.agent.schemas import Classification
from supportguard.db.models import (
    ApprovalRequest,
    SupportTicket,
    TicketMessage,
    TicketSummary,
)
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.tools.gateway import ToolGateway


def _message(
    index: int,
    role: str,
    content: str,
) -> dict[str, Any]:
    return {
        "history_kind": "message",
        "message_id": f"msg-{index:02d}",
        "role": role,
        "content": content,
        "historical": True,
        "trusted": False,
    }


def _assemble(
    history: list[dict[str, Any]],
    *,
    history_tokens: int = 520,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    packet = ContextAssembler(
        ContextBudget(
            max_input_tokens=2_400,
            output_reserve=400,
            evidence_tokens=100,
            tool_tokens=100,
            history_tokens=history_tokens,
        )
    ).assemble(
        run_id="run-context-v1512",
        step_index=7,
        user_goal="这两个版本的限制分别是什么？",
        trusted_task_state={
            "ticket_id": "ticket-context-v1512",
            "customer_id": "cust-demo",
            "current_actions": [
                {
                    "schema_version": "conversation-action-state.v1",
                    "approval_id": "approval-current",
                    "resource_id": "bill_demo_duplicate",
                    "projection_status": "executed",
                    "grants_action_authority": False,
                }
            ],
        },
        tools=[],
        latest_observations=[],
        evidence=[],
        history=history,
        remaining_budget={"llm_calls": 2, "tool_rounds": 1, "tool_attempts": 3},
    )
    return json.loads(packet.content), packet.manifest.omitted


class _RecordingDeterministicFakeProvider(DeterministicFakeProvider):
    """Records the Provider contract while remaining explicitly fake."""

    def __init__(self) -> None:
        super().__init__()
        self.classification_payloads: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> Any:
        if kwargs.get("output_schema") is Classification:
            self.classification_payloads.append(json.loads(str(kwargs["user"])))
        return await super().generate(**kwargs)


def test_v1512_legacy_checkpoint_history_is_budgeted_per_message() -> None:
    messages = [
        _message(
            index,
            "customer" if index % 2 == 0 else "assistant",
            ("旧消息" * 180) if index < 4 else f"第 {index // 2 + 1} 轮版本讨论",
        )
        for index in range(12)
    ]

    payload, omitted = _assemble(
        [{"current_conversation_recent_messages": messages}],
        history_tokens=360,
    )

    selected_ids = {item["message_id"] for item in payload["relevant_history"]}
    assert {"msg-10", "msg-11"} <= selected_ids
    assert not any(
        "current_conversation_recent_messages" in item for item in payload["relevant_history"]
    )
    omitted_messages = [item for item in omitted if item["section"] == "relevant_history"]
    assert omitted_messages
    assert all(item.get("message_id") for item in omitted_messages)
    assert any(
        item["reason"] == "history_section_budget_older_message" for item in omitted_messages
    )


def test_v1512_current_goal_action_truth_and_latest_pair_are_invariants() -> None:
    history = [
        *[
            _message(
                index,
                "customer" if index % 2 == 0 else "assistant",
                "无关的超长历史内容" * 100,
            )
            for index in range(10)
        ],
        _message(10, "customer", "atlas-chat 旧版和新版的 JSON Object 有什么差异？"),
        _message(11, "assistant", "旧版和新版都支持对象输出，但限制不同。"),
        {
            "history_kind": "ticket_summary",
            "history_item_id": "summary-current",
            "current_ticket": True,
            "issue_type": "product_capability",
            "open_questions": ["比较这两个版本"],
            "historical": True,
            "trusted": False,
        },
    ]

    payload, _ = _assemble(history)

    assert payload["user_goal"] == "这两个版本的限制分别是什么？"
    assert payload["trusted_task_state"]["current_actions"][0]["projection_status"] == "executed"
    selected_ids = {item.get("message_id") for item in payload["relevant_history"]}
    assert {"msg-10", "msg-11"} <= selected_ids
    assert any(
        item.get("history_item_id") == "summary-current" for item in payload["relevant_history"]
    )


@pytest.mark.parametrize("shape", ["flat", "legacy_checkpoint"])
def test_v1512_seven_turn_pronoun_anchor_is_checkpoint_metamorphic(
    shape: str,
) -> None:
    messages: list[dict[str, Any]] = []
    for turn in range(7):
        subject = "atlas-chat v2.1 与 v2.2" if turn == 0 else "这两个版本"
        messages.extend(
            (
                _message(turn * 2, "customer", f"第 {turn + 1} 轮：{subject} 的限制呢？"),
                _message(
                    turn * 2 + 1,
                    "assistant",
                    f"第 {turn + 1} 轮仍在比较 atlas-chat v2.1 与 v2.2。",
                ),
            )
        )
    history = messages if shape == "flat" else [{"current_conversation_recent_messages": messages}]

    payload, _ = _assemble(history, history_tokens=420)

    latest_pair = [
        item for item in payload["relevant_history"] if item.get("retention") == "latest_pair"
    ]
    assert [item["message_id"] for item in latest_pair] == ["msg-12", "msg-13"]
    assert "这两个版本" in latest_pair[0]["content"]
    assert "atlas-chat v2.1 与 v2.2" in latest_pair[1]["content"]


def test_v1512_oversized_summary_cannot_evict_recent_pair() -> None:
    pair = [
        _message(20, "customer", "上一轮仍在讨论 atlas-chat 两个版本。"),
        _message(21, "action", "版本资料检查已完成，未执行任何业务动作。"),
    ]
    baseline, _ = _assemble(pair, history_tokens=300)
    mutated, omitted = _assemble(
        [
            {
                "history_kind": "ticket_summary",
                "history_item_id": "summary-oversized",
                "current_ticket": True,
                "confirmed_facts": [{"value": "旧摘要" * 500}],
            },
            *pair,
        ],
        history_tokens=300,
    )

    def protected(payload: dict[str, Any]) -> list[tuple[str, str]]:
        return [
            (str(item["message_id"]), str(item["content"]))
            for item in payload["relevant_history"]
            if item.get("retention") == "latest_pair"
        ]

    assert protected(mutated) == protected(baseline)
    assert {
        "section": "relevant_history",
        "history_kind": "ticket_summary",
        "summary_id": "summary-oversized",
        "reason": "history_section_budget_summary",
    } in omitted
    assert not any(item.get("message_id") == "summary-oversized" for item in omitted)


def test_v1512_unknown_history_omission_keeps_non_message_identity() -> None:
    omission = ContextAssembler._history_omission(
        {
            "history_kind": "legacy_history_item",
            "history_item_id": "legacy-item-1",
        },
        reason="history_section_budget_summary",
    )

    assert omission == {
        "section": "relevant_history",
        "history_kind": "legacy_history_item",
        "history_item_id": "legacy-item-1",
        "reason": "history_section_budget_summary",
    }
    assert "message_id" not in omission
    assert "summary_id" not in omission


@pytest.mark.asyncio
async def test_v1512_classification_context_prioritizes_latest_pair_after_seven_rounds() -> None:
    rows = [
        _message(
            index,
            "customer" if index % 2 == 0 else "assistant",
            (
                "很早以前的超长无关上下文" * 200
                if index < 10
                else "atlas-chat v2.1 和 v2.2 仍是当前比较对象"
            ),
        )
        for index in range(14)
    ]
    state = AgentState(classification_context=rows)
    sessionless = cast(SupportGraph, type("SessionlessGraph", (), {"session": None})())

    bounded = await IntakeNodes(cast(Any, sessionless))._load_classification_context(state)

    assert [item["message_id"] for item in bounded[-2:]] == ["msg-12", "msg-13"]
    assert all("atlas-chat v2.1 和 v2.2" in item["content"] for item in bounded[-2:])
    assert sum(len(item["content"]) for item in bounded) <= 3_600


@pytest.mark.asyncio
async def test_v1512_seven_round_provider_contract_keeps_prior_entity_and_is_honest_fake() -> None:
    provider = _RecordingDeterministicFakeProvider()
    graph = SupportGraph(
        provider=provider,
        retrieval=None,
        gateway=ToolGateway(None),
    )
    customer_messages = [
        "请比较 atlas-chat v2.1 和 v2.2 的 JSON Object 限制。",
        "它们在对象嵌套上有什么差异？",
        "What about the older one?",
        "那新版呢？",
        "Does that limitation apply to both?",
        "旧的那个还支持吗？",
        "最后总结一下这两个版本。",
    ]
    history: list[dict[str, Any]] = []
    for round_index, current_message in enumerate(customer_messages):
        update = await graph.intake_nodes.classify(
            AgentState(
                tenant_id="tenant_customer",
                ticket_id="ticket_seven_rounds",
                customer_id="customer_current",
                run_id=f"run_round_{round_index}",
                trace_id=f"trace_round_{round_index}",
                redacted_message=current_message,
                classification_context=list(history),
                current_actions=[],
                llm_calls=0,
            )
        )
        assert update["llm_calls"] == 1
        history.extend(
            (
                _message(round_index * 2, "customer", current_message),
                _message(
                    round_index * 2 + 1,
                    "assistant",
                    "我会沿用上一轮明确的比较对象继续回答。",
                ),
            )
        )

    assert provider.mode == "fake"
    assert provider.model == "deterministic-fake"
    assert provider.tool_call_mode == "native_fixture"
    assert len(provider.classification_payloads) == 7
    final_payload = provider.classification_payloads[-1]
    assert final_payload["current_turn"] == "最后总结一下这两个版本。"
    assert any(
        "atlas-chat v2.1 和 v2.2" in item["content"]
        for item in final_payload["recent_conversation"]
    )
    assert all(
        item["historical"] is True and item["trusted"] is False
        for item in final_payload["recent_conversation"]
    )


@pytest.mark.asyncio
async def test_v1512_classification_omission_manifest_is_returned_and_ledger_bound() -> None:
    provider = _RecordingDeterministicFakeProvider()
    graph = SupportGraph(
        provider=provider,
        retrieval=None,
        gateway=ToolGateway(None),
    )
    manifests: list[dict[str, Any]] = []

    async def capture_context_manifest(*_args: Any, **kwargs: Any) -> None:
        manifests.append(dict(kwargs["component_manifest"]))
        return None

    graph.runtime._persist_context_ledger = capture_context_manifest  # type: ignore[method-assign]
    rows = [
        _message(80, "customer", "很早的完整问题" * 600),
        _message(81, "assistant", "很早的完整回答" * 600),
        _message(82, "customer", "这两个版本现在呢？"),
        _message(83, "assistant", "仍在比较前文明确的两个版本。"),
    ]

    update = await graph.intake_nodes.classify(
        AgentState(
            tenant_id="tenant_customer",
            ticket_id="ticket_manifest",
            customer_id="customer_current",
            run_id="run_manifest",
            trace_id="trace_manifest",
            redacted_message="继续说说它们的限制。",
            classification_context=rows,
            current_actions=[],
            llm_calls=0,
        )
    )

    omissions = update["classification_context_omissions"]
    assert {
        "section": "classification_history",
        "message_id": "msg-80",
        "reason": "classification_budget_older_message",
    } in omissions
    assert {
        "section": "classification_history",
        "message_id": "msg-81",
        "reason": "classification_budget_older_message",
    } in omissions
    selected_ids = {item["message_id"] for item in update["classification_context"]}
    assert {"msg-82", "msg-83"} <= selected_ids
    assert manifests
    persisted = manifests[-1]["classification_history"]
    assert persisted["omitted"] == omissions
    assert persisted["protected_latest_pair_message_ids"] == ["msg-82", "msg-83"]


@pytest.mark.asyncio
async def test_v1512_classification_history_keeps_whole_messages() -> None:
    complete_old_message = "完整分类历史" * 120
    state = AgentState(
        classification_context=[
            _message(60, "assistant", complete_old_message),
            _message(61, "customer", "最近问题"),
            _message(62, "assistant", "最近完整回答"),
        ]
    )
    sessionless = cast(SupportGraph, type("SessionlessGraph", (), {"session": None})())

    bounded = await IntakeNodes(cast(Any, sessionless))._load_classification_context(state)

    old_message = next(item for item in bounded if item["message_id"] == "msg-60")
    assert old_message["content"] == complete_old_message
    assert "content_truncated" not in old_message


@pytest.mark.asyncio
async def test_v1512_oversized_classification_pair_fails_closed() -> None:
    state = AgentState(
        classification_context=[
            _message(70, "customer", "完整客户消息" * 400),
            _message(71, "assistant", "完整助手回答" * 400),
        ]
    )
    sessionless = cast(SupportGraph, type("SessionlessGraph", (), {"session": None})())

    with pytest.raises(
        ContextBudgetExceeded,
        match="protected classification history pair exceeds budget",
    ):
        await IntakeNodes(cast(Any, sessionless))._load_classification_context(state)


@pytest.mark.asyncio
async def test_v1512_legacy_manual_takeover_without_binding_remains_readable_history() -> None:
    state = AgentState(
        classification_context=[
            {
                "role": "customer",
                "content": "之前显示转人工，这是什么意思？",
                "legacy_checkpoint": True,
            },
            {
                "role": "action",
                "content": ("历史自动处理已停止；当前版本没有人工坐席收件、回复或完成闭环。"),
                "legacy_checkpoint": True,
                # Historical fixture deliberately has no Approval/resource binding.
            },
        ]
    )
    sessionless = cast(SupportGraph, type("SessionlessGraph", (), {"session": None})())

    bounded, omissions = await IntakeNodes(cast(Any, sessionless))._select_classification_context(
        state,
    )

    assert omissions == []
    assert [item["role"] for item in bounded] == ["customer", "action"]
    assert all(item["legacy_checkpoint"] is True for item in bounded)
    assert all(item["synthetic_message_id"] is True for item in bounded)
    assert all(item["trusted"] is False for item in bounded)
    assert "没有人工坐席" in bounded[-1]["content"]


def test_v1512_latest_action_update_is_part_of_recent_pair() -> None:
    messages = [
        _message(30, "customer", "这个退款申请现在怎么样？"),
        _message(31, "action", "退款已经执行完成。"),
    ]

    IntakeNodes._mark_latest_history_pair(messages)

    assert [item["retention"] for item in messages] == ["latest_pair", "latest_pair"]


def test_v1512_history_messages_are_never_silently_truncated() -> None:
    long_content = "完整历史语义" * 120
    history = [
        _message(40, "customer", long_content),
        _message(41, "assistant", "旧问题的完整回答"),
        _message(42, "customer", "最近问题"),
        _message(43, "assistant", "最近回答"),
    ]

    payload, omitted = _assemble(history, history_tokens=280)

    assert all(item.get("content") != long_content for item in payload["relevant_history"])
    assert {
        "section": "relevant_history",
        "history_kind": "message",
        "message_id": "msg-40",
        "reason": "history_section_budget_older_message",
    } in omitted
    assert not any("truncated" in item["reason"] for item in omitted)


def test_v1512_seventeen_plus_messages_have_complete_omission_accounting() -> None:
    history = [
        _message(
            index,
            "customer" if index % 2 == 0 else "assistant",
            (
                f"较早的完整消息 {index} " + "不会被静默丢弃" * 80
                if index < 16
                else f"最新完整消息 {index}"
            ),
        )
        for index in range(18)
    ]

    payload, omitted = _assemble(history, history_tokens=260)

    selected_ids = {str(item["message_id"]) for item in payload["relevant_history"]}
    omitted_messages = [
        item
        for item in omitted
        if item["section"] == "relevant_history"
        and item["reason"] == "history_section_budget_older_message"
    ]
    omitted_ids = {item["message_id"] for item in omitted_messages}
    expected_ids = {f"msg-{index:02d}" for index in range(18)}
    assert selected_ids.isdisjoint(omitted_ids)
    assert selected_ids | omitted_ids == expected_ids
    assert {"msg-16", "msg-17"} <= selected_ids
    assert all(item["message_id"] != "history-item-unknown" for item in omitted_messages)


@pytest.mark.asyncio
async def test_v1512_more_than_one_hundred_history_messages_fails_closed() -> None:
    state = AgentState(
        classification_context=[
            _message(
                index,
                "customer" if index % 2 == 0 else "assistant",
                f"完整消息 {index}",
            )
            for index in range(101)
        ]
    )
    sessionless = cast(SupportGraph, type("SessionlessGraph", (), {"session": None})())

    with pytest.raises(
        ContextBudgetExceeded,
        match="classification history exceeds explicit message bound",
    ):
        await IntakeNodes(cast(Any, sessionless))._select_classification_context(state)


def test_v1512_oversized_protected_pair_fails_closed_instead_of_truncating() -> None:
    history = [
        _message(50, "customer", "客户完整长消息" * 200),
        _message(51, "assistant", "助手完整长回答" * 200),
    ]

    with pytest.raises(
        ContextBudgetExceeded,
        match="protected recent history pair exceeds budget",
    ):
        _assemble(history, history_tokens=200)


def test_v1512_new_flat_history_requires_canonical_message_id() -> None:
    with pytest.raises(
        ContextBudgetExceeded,
        match="history message is missing canonical message_id",
    ):
        _assemble(
            [
                {
                    "history_kind": "message",
                    "role": "customer",
                    "content": "缺少持久化消息身份",
                }
            ]
        )


@pytest.mark.asyncio
async def test_v1512_graph_history_adapter_is_flat_and_reads_current_summary(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    current_message = await db_session.get(TicketMessage, "message_demo")
    ticket = await db_session.get(SupportTicket, "ticket_demo")
    assert current_message is not None
    assert ticket is not None
    current_message.conversation_sequence = 20
    ticket.next_message_sequence = 22
    db_session.add(
        ApprovalRequest(
            id="approval-context-referent",
            tenant_id="tenant_demo",
            ticket_id="ticket_demo",
            customer_id="cust_demo",
            run_id="run_demo",
            checkpoint_id="checkpoint_demo",
            action_type="refund",
            resource_type="billing_record_id",
            resource_id="bill_duplicate",
            origin_turn_id="turn_demo",
            action_payload={"billing_record_id": "bill_duplicate"},
            review_context={},
            action_hash="a" * 64,
            business_version=2,
            status="rejected",
            idempotency_key="idem-context-referent",
        )
    )
    for index in range(2, 20):
        role = "user" if index % 2 == 0 else "assistant"
        kind = "customer" if role == "user" else "assistant"
        if index == 19:
            role = "action"
            kind = "action_update"
        db_session.add(
            TicketMessage(
                id=f"db-msg-{index:02d}",
                tenant_id="tenant_demo",
                ticket_id="ticket_demo",
                role=role,
                message_kind=kind,
                conversation_sequence=index,
                approval_id=("approval-context-referent" if index == 19 else None),
                content=("这两个版本仍是当前讨论主题" if index >= 18 else f"历史对话第 {index} 条"),
            )
        )
    db_session.add_all(
        [
            TicketMessage(
                id="db-msg-future-customer",
                tenant_id="tenant_demo",
                ticket_id="ticket_demo",
                role="user",
                message_kind="customer",
                conversation_sequence=21,
                content="这条消息是在当前 Run 之后排队的，不能进入当前上下文。",
            ),
            TicketMessage(
                id="db-msg-future-assistant",
                tenant_id="tenant_demo",
                ticket_id="ticket_demo",
                role="assistant",
                message_kind="assistant",
                conversation_sequence=22,
                content="这条未来回答也不能进入较早 Run。",
            ),
        ]
    )
    db_session.add(
        TicketSummary(
            id="summary-current-ticket",
            tenant_id="tenant_demo",
            ticket_id="ticket_demo",
            customer_id="cust_demo",
            issue_type="product_capability",
            confirmed_facts=[
                {
                    "fact_type": "knowledge_evidence",
                    "source_type": "knowledge_chunk",
                    "source_id": "product-versions:c1",
                    "resource_version": "2.2",
                    "status": "active",
                    "value": "must not be copied into context projection",
                }
            ],
            attempted_actions=[],
            open_questions=["比较这两个版本"],
            source_refs=[],
            source_run_id="run_demo",
            canonical_checkpoint_hash="c" * 64,
            event_watermark=12,
            freshness_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        session=db_session,
    )
    state = AgentState(
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        run_id="run_demo",
        customer_message_id="message_demo",
        classification={"issue_type": "product_capability"},
    )

    classification_context, _ = await graph.intake_nodes._select_classification_context(state)
    admission_context = await graph.intake_nodes._load_admission_context(
        state,
        current_message_id="message_demo",
    )
    update = await graph.intake_nodes.load_history(state)

    history = update["relevant_history"]
    assert "db-msg-future-customer" not in {item["message_id"] for item in classification_context}
    assert "db-msg-future-assistant" not in {item["message_id"] for item in classification_context}
    assert (
        IntakeNodes._recent_action_message_approval_id(classification_context)
        == "approval-context-referent"
    )
    assert "db-msg-future-customer" not in {item["message_id"] for item in admission_context}
    assert all("current_conversation_recent_messages" not in item for item in history)
    protected = [item for item in history if item.get("retention") == "latest_pair"]
    message_history = [item for item in history if item.get("history_kind") == "message"]
    assert len(message_history) == 18
    assert [item["message_id"] for item in protected] == ["db-msg-18", "db-msg-19"]
    assert protected[-1]["role"] == "action"
    current_summary = next(
        item for item in history if item.get("history_item_id") == "summary-current-ticket"
    )
    assert current_summary["current_ticket"] is True
    assert current_summary["issue_type"] == "product_capability"
    assert current_summary["confirmed_facts"][0]["source_id"] == "product-versions:c1"
    assert "value" not in current_summary["confirmed_facts"][0]
    assert not any(
        item.get("message_id") in {"db-msg-future-customer", "db-msg-future-assistant"}
        for item in history
    )


@pytest.mark.asyncio
async def test_v1512_canonical_current_message_missing_sequence_fails_closed(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    current_message = await db_session.get(TicketMessage, "message_demo")
    assert current_message is not None
    current_message.conversation_sequence = None
    await db_session.flush()
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        session=db_session,
    )
    state = AgentState(
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        run_id="run_demo",
        customer_message_id="message_demo",
        classification={"issue_type": "product_capability"},
    )

    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message is missing conversation sequence",
    ):
        await graph.intake_nodes._select_classification_context(state)
    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message is missing conversation sequence",
    ):
        await graph.intake_nodes._load_admission_context(
            state,
            current_message_id="message_demo",
        )
    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message is missing conversation sequence",
    ):
        await graph.intake_nodes.load_history(state)


@pytest.mark.asyncio
async def test_v1512_canonical_current_message_identity_conflict_fails_closed(
    db_session: AsyncSession,
) -> None:
    await seed_business_facts(db_session)
    graph = SupportGraph(
        provider=DeterministicFakeProvider(),
        retrieval=None,
        gateway=cast(ToolGateway, object()),
        session=db_session,
    )
    state = AgentState(
        tenant_id="tenant_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        run_id="run_demo",
        customer_message_id="message_from_another_run",
    )

    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message identity conflicts",
    ):
        await graph.intake_nodes._select_classification_context(state)
    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message identity conflicts",
    ):
        await graph.intake_nodes._load_admission_context(
            state,
            current_message_id="message_from_another_run",
        )
    with pytest.raises(
        ContextBudgetExceeded,
        match="canonical current message identity conflicts",
    ):
        await graph.intake_nodes.load_history(state)
