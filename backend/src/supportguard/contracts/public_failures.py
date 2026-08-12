from __future__ import annotations

from typing import Literal

PublicFailureCategory = Literal["api_request", "provider", "tool", "runtime"]


def classify_public_failure(value: object) -> PublicFailureCategory | None:
    """Collapse private failure detail into one stable customer-safe category."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value.lower()
    if normalized.startswith(("api_request", "request_trace", "api_gateway", "request_id")):
        return "api_request"
    if any(token in normalized for token in ("provider", "llm", "model", "upstream")):
        return "provider"
    if any(token in normalized for token in ("mcp", "tool", "capability", "observation")):
        return "tool"
    return "runtime"


def public_failure_unknown_phrase(category: PublicFailureCategory) -> str:
    """Return the category-specific unknown fact safe for public projection."""

    return {
        "api_request": "当前还无法把问题与具体 API 请求记录关联",
        "provider": "当前还无法确认模型服务是否完成了本轮推理",
        "tool": "当前还无法确认所需业务数据是否完整返回",
        "runtime": "当前还无法确认自动流程完成到了哪一个可恢复步骤",
    }[category]


def public_failure_next_step(category: PublicFailureCategory) -> str:
    """Return the category-specific actionable recovery step."""

    return {
        "api_request": "请重试；若仍失败，请补充 Request ID 和发生时间",
        "provider": "请稍后重试；若仍失败，请保留发生时间和相关资源编号",
        "tool": "请核对账单、API Key 或订阅编号后重试",
        "runtime": "请稍后重试，或发送一条新消息重新开始本轮处理",
    }[category]


def public_failure_reply(category: PublicFailureCategory) -> str:
    """Return the five-part recovery contract shown to a customer."""

    return "".join(
        (
            "系统已检查本轮请求并尝试完成自动诊断。",
            "已确认本轮没有形成可验证的完整结果。",
            f"{public_failure_unknown_phrase(category)}。",
            "本轮没有执行新的高风险操作，相关申请仍以当前持久化状态为准。",
            f"{public_failure_next_step(category)}。",
        )
    )
