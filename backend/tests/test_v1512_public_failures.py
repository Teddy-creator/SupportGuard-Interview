from supportguard.contracts.public_failures import (
    classify_public_failure,
    public_failure_reply,
)


def test_private_failures_collapse_to_bounded_public_categories() -> None:
    assert classify_public_failure("api_request_trace_missing:secret") == "api_request"
    assert classify_public_failure("ProviderTimeout:upstream-secret") == "provider"
    assert classify_public_failure("mcp_rehandshake_failed:private") == "tool"
    assert classify_public_failure("database_commit_unknown:private") == "runtime"
    assert classify_public_failure(None) is None


def test_customer_failure_reply_has_all_five_recovery_parts() -> None:
    reply = public_failure_reply("tool")

    assert "系统已检查本轮请求" in reply
    assert "已确认本轮没有形成可验证的完整结果" in reply
    assert "无法确认所需业务数据是否完整返回" in reply
    assert "没有执行新的高风险操作" in reply
    assert "请核对账单、API Key 或订阅编号后重试" in reply
    assert "private" not in reply
