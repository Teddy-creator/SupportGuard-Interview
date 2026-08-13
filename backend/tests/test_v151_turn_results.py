import json
import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

from supportguard.agent.responses import safe_failure_answer
from supportguard.services.turn_results import (
    activity_label,
    safe_stop_terminal_state,
    turn_result_for,
)


def test_turn_results_are_orthogonal_to_activity_and_legacy_terminal() -> None:
    assert turn_result_for("answered", terminal_state="resolved") == "answered"
    assert turn_result_for("evidence_freshness_insufficient") == "answered_limited"
    assert turn_result_for("applicability_condition_unresolved") == "answered_limited"
    assert turn_result_for("mixed_account_applicability_incomplete") == "answered_limited"
    assert turn_result_for("explicit_current_fact_incomplete") == "answered_limited"
    assert turn_result_for("needs_clarification") == "needs_clarification"
    assert turn_result_for("rejected", terminal_state="rejected") == "rejected"
    assert turn_result_for("rejected", terminal_state="resolved") == "refused"
    assert turn_result_for("withdrawn", terminal_state="withdrawn") == "withdrawn"
    assert turn_result_for("stale", terminal_state="stale") == "stale"
    assert turn_result_for("proposed", terminal_state="awaiting_approval") == "proposal_created"
    assert turn_result_for("manual_takeover", automation_mode="human_queue") == "human_queue"
    assert turn_result_for("manual_takeover", automation_mode="agent") == "failed"
    assert turn_result_for("provider_terminal_schema_invalid") == "failed"


def test_safe_stop_terminal_state_separates_business_conflict_from_runtime_failure() -> None:
    assert safe_stop_terminal_state("obligation_conflict") == "resolved"
    assert turn_result_for("obligation_conflict", terminal_state="resolved") == "answered"
    for reason in (
        "obligation_hard_failure",
        "provider_failed",
        "tool_failed",
        "llm_call_budget_exhausted",
        "binding_stale",
    ):
        assert safe_stop_terminal_state(reason) == "failed"


def test_activity_label_uses_frozen_priority_and_distinct_terminal_copy() -> None:
    base = {
        "lifecycle": "active",
        "automation_mode": "agent",
        "latest_result": "failed",
        "has_running": False,
        "has_queued": False,
        "has_pending_action": False,
    }
    assert activity_label(**base) == "本轮未完成"
    assert activity_label(**{**base, "latest_result": "refused"}) == "请求未执行"
    assert activity_label(**{**base, "latest_result": "needs_clarification"}) == "需要补充信息"
    assert activity_label(**{**base, "latest_result": "answered"}) == "已回答"
    assert activity_label(**{**base, "latest_result": "answered_limited"}) == "已给出有限结论"
    assert activity_label(**{**base, "has_running": True}) == "正在处理"
    assert activity_label(**{**base, "has_pending_action": True}) == "等待审批"
    assert activity_label(**{**base, "has_completed_action": True}) == "操作已完成"
    assert (
        activity_label(
            **{
                **base,
                "automation_mode": "human_queue",
                "latest_result": "human_queue",
            }
        )
        == "自动处理已停止"
    )


def test_failure_taxonomy_is_actionable_and_never_invents_human_queue() -> None:
    for reason in (
        "provider_failed",
        "provider_terminal_schema_invalid",
        "tool_failed",
        "proposal_eligibility_failed",
        "mixed_account_applicability_incomplete",
        "no_progress",
        "llm_call_budget_exhausted",
    ):
        answer = safe_failure_answer(reason)
        assert "转交人工" not in answer
        assert "已转人工" not in answer
        assert "没有执行" in answer or "尚未执行" in answer


def test_out_of_scope_response_keeps_conversation_open_without_tools() -> None:
    answer = safe_failure_answer("out_of_scope")
    assert "不在 SupportGuard" in answer
    assert "天气" in answer
    assert "没有调用业务工具" in answer
    assert "继续询问" in answer


def test_prohibited_response_names_the_boundary_and_zero_effect() -> None:
    answer = safe_failure_answer("prohibited")
    assert "其他客户" in answer
    assert "安全拒绝" in answer
    assert "现行安全策略" in answer
    assert "独立审批" in answer
    assert "没有调用业务工具" in answer
    assert "执行变更" in answer


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("mcp_rehandshake_failed", "读取能力"),
        ("tool_transport_budget_exhausted", "有限重试"),
        ("semantic_no_progress", "没有获得能继续推进的新增事实"),
    ],
)
def test_read_failure_copy_names_checked_boundary_and_next_step(
    reason: str,
    expected: str,
) -> None:
    answer = safe_failure_answer(reason)

    assert expected in answer
    assert "本次没有创建审批" in answer
    assert "没有执行任何变更" in answer
    assert "Request ID" in answer or "资源引用" in answer
    assert "转交人工" not in answer


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("tool_timeout", "限定时间内没有完成"),
        ("tool_unavailable", "查询服务目前不可用"),
        ("tool_output_schema_invalid", "没有通过完整性校验"),
    ],
)
def test_typed_mcp_failure_copy_preserves_the_actual_failure_category(
    reason: str,
    expected: str,
) -> None:
    answer = safe_failure_answer(reason)

    assert expected in answer
    assert "没有创建审批" in answer
    assert "没有执行任何变更" in answer
    assert "重试" in answer
    assert "转交人工" not in answer


@pytest.mark.parametrize(
    "reason",
    ["provider_decision_invalid", "provider_terminal_schema_invalid"],
)
def test_invalid_provider_result_is_actionable_without_internal_schema_terms(
    reason: str,
) -> None:
    answer = safe_failure_answer(reason)

    assert "分析结果不完整" in answer
    assert "可靠结论" in answer
    assert "没有创建审批" in answer
    assert "没有执行任何变更" in answer
    assert "重试" in answer
    assert "模型返回" not in answer
    assert "格式校验" not in answer


@pytest.mark.parametrize(
    "reason",
    [
        "billing_scope_violation",
        "cross_tenant_argument",
        "observation_scope_mismatch",
    ],
)
def test_scope_failure_copy_does_not_confirm_another_tenant_resource(
    reason: str,
) -> None:
    answer = safe_failure_answer(reason)

    assert "当前登录账户" in answer
    assert "不代表该资源在其他账户中存在" in answer
    assert "没有创建审批" in answer
    assert "没有执行变更" in answer


def test_billing_scope_failure_is_account_safe_and_actionable() -> None:
    answer = safe_failure_answer("billing_scope_violation")

    assert "当前账户中找不到或无法访问" in answer
    assert "账单编号" in answer
    assert "确认当前登录账户" in answer
    assert "不代表该资源在其他账户中存在" in answer
    assert "没有创建审批" in answer
    assert "没有执行变更" in answer
    assert "Request ID" not in answer
    assert "区域" not in answer


def test_failure_next_steps_are_typed_instead_of_one_mechanical_checklist() -> None:
    provider = safe_failure_answer("provider_failed")
    read_tool = safe_failure_answer("tool_failed")
    scope = safe_failure_answer("billing_scope_violation")
    stale = safe_failure_answer("binding_stale")

    assert "稍后重试" in provider
    assert "Request ID" not in provider
    assert "发生区域" not in provider
    assert "账单、Key 或订阅" in read_tool
    assert "管理员授权流程" in scope
    assert "重新核验" in stale
    assert len({provider, read_tool, scope, stale}) == 4


def test_knowledge_read_failure_names_the_customer_capability_not_runtime_internals() -> None:
    answer = safe_failure_answer(
        "tool_transport_budget_exhausted",
        failure_domain="knowledge",
    )

    assert "产品知识查询暂时不可用" in answer
    assert "产品能力、限制或文档事实" in answer
    assert "没有创建审批" in answer
    assert "没有执行任何变更" in answer
    assert "正式文档" in answer
    assert "Request ID" not in answer
    assert "MCP" not in answer
    assert "Worker" not in answer
    assert "Fence" not in answer


@pytest.mark.postgres
async def test_postgres_redaction_receipt_and_failed_turn_projection_are_truthful() -> None:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required")
    api_url = make_url(raw_url).set(
        username="supportguard_api",
        password="supportguard_api",  # noqa: S106
    )
    api_engine = create_async_engine(api_url)
    admin_engine = create_async_engine(raw_url)
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_v151_{suffix}"
    run_id = f"run_v151_{suffix}"
    message_id = f"msg_v151_{suffix}"
    fingerprint = "0123456789abcdef"
    request = {
        "schema_version": "api-accept-ticket.v1",
        "customer_id": "cust_demo",
        "principal_id": "user_customer_demo",
        "idempotency_key": f"idem-v151-{suffix}",
        "message": "我的 Key 是 [REDACTED_API_KEY]，请检查",
        "trace_id": f"trace-v151-{suffix}",
        "idempotency_id": f"idem_v151_{suffix}",
        "ticket_id": ticket_id,
        "message_id": message_id,
        "run_id": run_id,
        "job_id": f"job_v151_{suffix}",
        "outbox_id": f"outbox_v151_{suffix}",
        "delivery_id": f"delivery_v151_{suffix}",
        "audit_id": f"audit_v151_{suffix}",
        "model": "deterministic-fake",
        "provider_mode": "fake",
        "tool_call_mode": "native_fixture",
        "prompt_version": "agent_decide.v3",
        "agent_schema_version": "agent.v1",
        "context_version": "context.v1",
    }
    try:
        async with api_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(
                text("SELECT set_config('app.principal_id','user_customer_demo',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.principal_role','customer_admin',true)")
            )
            await connection.execute(
                text("SELECT set_config('app.ingress_redaction_receipt',:receipt,true)"),
                {
                    "receipt": json.dumps(
                        [
                            {
                                "kind": "ingress_redaction_receipt",
                                "count": 1,
                                "rule_ids": ["secret.api_key.v1"],
                                "secret_fingerprints": [fingerprint],
                            }
                        ]
                    )
                },
            )
            accepted = await connection.scalar(
                text("SELECT supportguard_api_accept_ticket(CAST(:request AS jsonb))"),
                {"request": json.dumps(request, sort_keys=True, separators=(",", ":"))},
            )
            assert isinstance(accepted, dict) and not accepted.get("error_code")
        async with admin_engine.begin() as connection:
            receipt = await connection.scalar(
                text("SELECT source_refs FROM ticket_messages WHERE id=:message"),
                {"message": message_id},
            )
            assert receipt[0]["kind"] == "ingress_redaction_receipt"
            assert receipt[0]["secret_fingerprints"] == [fingerprint]
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='completed',"
                    "agent_finish_reason='provider_failed' "
                    "WHERE id=:run"
                ),
                {"run": run_id},
            )
        async with api_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id','tenant_demo',true)"))
            detail = await connection.scalar(
                text("SELECT supportguard_api_get_conversation_page(:customer,:ticket,NULL,50)"),
                {"customer": "cust_demo", "ticket": ticket_id},
            )
            listing = await connection.scalar(
                text("SELECT supportguard_api_list_conversations(:customer,NULL,NULL,30)"),
                {"customer": "cust_demo"},
            )
        assert detail["turns"][0]["result_state"] == "failed"
        assert detail["activity_label"] == "本轮未完成"
        projected = next(item for item in listing["items"] if item["id"] == ticket_id)
        assert projected["activity_label"] == "本轮未完成"
        assert fingerprint not in json.dumps(detail, ensure_ascii=False)
    finally:
        await api_engine.dispose()
        await admin_engine.dispose()
