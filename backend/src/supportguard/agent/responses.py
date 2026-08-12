from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from supportguard.agent.obligations import TerminalBusinessOutcome

_SAFE_RESOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SAFE_AMOUNT = re.compile(r"^\d{1,12}(?:\.\d{1,2})?$")


@dataclass(frozen=True)
class TerminalOutcomeRendering:
    answer: str
    material_claim: str | None


def render_executed_action_update(
    action_type: str,
    *,
    resource_id: str,
    result: dict[str, Any],
) -> str:
    """Render a committed action from its customer-safe, authoritative result."""

    safe_resource = resource_id if _SAFE_RESOURCE_REF.fullmatch(resource_id) else "当前资源"
    if action_type == "refund":
        amount = result.get("amount")
        currency = result.get("currency")
        if (
            isinstance(amount, str)
            and _SAFE_AMOUNT.fullmatch(amount)
            and isinstance(currency, str)
            and _SAFE_CURRENCY.fullmatch(currency)
        ):
            return (
                f"账单 {safe_resource} 的退款已经安全执行完成，"
                f"退款金额为 {amount} {currency}，账单状态已更新。"
            )
        return f"账单 {safe_resource} 的退款已经安全执行完成，账单状态已更新。"
    if action_type == "api_key_revocation":
        return f"API Key {safe_resource} 已安全吊销，旧密钥不再可用。"
    if action_type == "entitlement_change":
        before = result.get("before")
        after = result.get("after")
        if isinstance(after, dict):
            for field, label in (
                ("concurrency_limit", "并发上限"),
                ("rpm_limit", "RPM 上限"),
            ):
                proposed = after.get(field)
                current = before.get(field) if isinstance(before, dict) else None
                if (
                    isinstance(proposed, int)
                    and not isinstance(proposed, bool)
                    and 0 <= proposed <= 1_000_000
                ):
                    if (
                        isinstance(current, int)
                        and not isinstance(current, bool)
                        and 0 <= current <= 1_000_000
                    ):
                        return (
                            f"订阅 {safe_resource} 的{label}已经安全地"
                            f"从 {current} 调整为 {proposed}。"
                        )
                    return f"订阅 {safe_resource} 的{label}已经安全地调整为 {proposed}。"
            plan = after.get("plan")
            current_plan = before.get("plan") if isinstance(before, dict) else None
            if isinstance(plan, str) and _SAFE_RESOURCE_REF.fullmatch(plan):
                if isinstance(current_plan, str) and _SAFE_RESOURCE_REF.fullmatch(current_plan):
                    return f"订阅 {safe_resource} 的套餐已经安全地从 {current_plan} 调整为 {plan}。"
                return f"订阅 {safe_resource} 的套餐已经安全地调整为 {plan}。"
        return f"订阅 {safe_resource} 的套餐或配额变更已经安全执行完成。"
    return "操作已经安全执行完成。"


def _public_value(value: object, *, fallback: str = "未知") -> str:
    normalized = " ".join(str(value).split())[:128]
    return normalized or fallback


def render_terminal_business_outcome(
    outcome: TerminalBusinessOutcome,
) -> TerminalOutcomeRendering:
    """Render only registry-selected, validated facts without model free text."""

    facts = outcome.observed_facts
    resource_ref = _public_value(
        outcome.resource_ref
        or facts.get("billing_record_id")
        or facts.get("api_key_id")
        or facts.get("subscription_id"),
        fallback="当前资源",
    )
    status = _public_value(facts.get("status"), fallback="不可操作")
    key = outcome.customer_message_key
    if key == "refund_resource_not_available":
        return TerminalOutcomeRendering(
            answer=(
                f"我在当前账户范围内无法定位账单 {resource_ref}。请核对 Billing ID 后重试；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=None,
        )
    if key == "refund_status_not_actionable":
        state = "已经退款" if status == "refunded" else f"当前状态为 {status}"
        claim = f"账单 {resource_ref} {state}。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}因此不会再次创建退款申请。"
                "如果你要查询退款到账进度，可以继续提供支付渠道或退款时间；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    if key == "refund_duplicate_relation_unconfirmed":
        claim = f"账单 {resource_ref} 的当前记录未显示可用于自动退款的重复扣费关系。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}请核对原始账单和疑似重复账单的 Billing ID 后继续；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    if key == "api_key_resource_not_available":
        return TerminalOutcomeRendering(
            answer=(
                f"我在当前账户范围内无法定位 API Key 引用 {resource_ref}。"
                "请只核对 Key Reference，不要发送完整密钥；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=None,
        )
    if key == "api_key_status_not_actionable":
        state = (
            "已经撤销" if status in {"revoked", "disabled", "inactive"} else f"当前状态为 {status}"
        )
        claim = f"API Key {resource_ref} {state}。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}因此不会创建重复撤销申请。"
                "如需恢复调用，请创建并安全分发新的 Key；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    if key == "subscription_resource_not_available":
        return TerminalOutcomeRendering(
            answer=(
                "我在当前账户范围内无法定位可变更的订阅。请先核对账户与订阅状态；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=None,
        )
    if key == "subscription_status_not_actionable":
        claim = f"订阅 {resource_ref} 当前状态为 {status}。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}该状态暂不允许提交配额或套餐变更。"
                "请先恢复或核对订阅状态；本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    if key == "entitlement_target_unsupported":
        change_type = _public_value(facts.get("requested_change_type"))
        claim = f"订阅 {resource_ref} 当前未发布 {change_type} 这一变更能力。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}请改为账户已发布的配额或套餐变更类型；"
                "本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    if key == "entitlement_target_noop":
        requested = next(
            (
                _public_value(value)
                for name, value in facts.items()
                if name.startswith("requested_target_")
            ),
            "当前值",
        )
        claim = f"订阅 {resource_ref} 的当前配置已经是请求值 {requested}。"
        return TerminalOutcomeRendering(
            answer=(
                f"{claim}无需创建重复变更申请。"
                "如需调整，请提供不同的目标值；本次没有创建审批，也没有执行任何变更。"
            ),
            material_claim=claim,
        )
    raise ValueError("unknown terminal business outcome message key")


def safe_clarification_answer(question: str | None) -> str:
    """Render one bounded provider question without replacing its missing field."""
    normalized = " ".join((question or "").split())
    if not normalized or len(normalized) > 500:
        return safe_failure_answer("needs_clarification")
    return f"{normalized}\n在你补充这些信息前，我不会创建审批，也不会执行任何变更。"


def safe_applicability_condition_answer(conditions: list[str]) -> str:
    """Fail closed without turning a customer-supplied condition into a fact."""

    label = "、".join(dict.fromkeys(conditions))
    return (
        f"关于 {label} 的上下文限制或能力适用性，当前可用证据没有形成可直接确认"
        "该条件的完整结论，因此我不能断言它与其他区域或套餐完全相同。"
        "请核对该区域或套餐对应的正式兼容性资料后重试；本次没有创建审批，"
        "也没有执行任何变更。"
    )


def safe_failure_answer(
    reason: str,
    *,
    missing_groups: list[str] | None = None,
    failure_domain: str | None = None,
) -> str:
    """Return actionable customer copy without exposing internals or inventing handoff."""
    missing = "、".join(missing_groups or [])
    if failure_domain == "knowledge" and (
        reason
        in {
            "tool_failed",
            "tool_timeout",
            "tool_unavailable",
            "obligation_hard_failure",
            "mcp_rehandshake_failed",
            "read_capability_failed",
            "tool_transport_budget_exhausted",
        }
        or "retry_exhausted" in reason
    ):
        return (
            "产品知识查询暂时不可用，因此我现在不能可靠回答这项产品能力、限制或文档事实。"
            "本次没有创建审批，也没有执行任何变更。请稍后重试，或查阅对应产品的正式文档；"
            "如果仍无法确认，请联系产品支持。"
        )
    if reason == "human_handoff_unavailable":
        return (
            "我已检查当前请求，但 SupportGuard 目前没有可接收并回复消息的人工坐席闭环，"
            "因此不会把这次请求标记为“有人正在处理”。本次没有创建人工队列或操作审批，"
            "也没有执行任何变更。请继续说明具体产品、资源引用或错误现象，我会在当前"
            "自动支持范围内继续核验。"
        )
    if reason == "evidence_conflict":
        return (
            "我已检查当前可用资料，但发现支持结论的已发布证据存在冲突，"
            "因此现在不能安全给出确定结论。本次没有创建人工队列或操作审批，"
            "也没有执行任何变更。请补充你使用的产品版本和发生时间，"
            "或在资料更新后重新查询。"
        )
    if reason == "comparison_evidence_incomplete":
        return (
            "我已检查当前版本与历史版本资料，但没有同时获得两组可追溯的已发布证据，"
            "因此现在不能可靠说明版本差异。本次没有创建审批，也没有执行任何变更。"
            "请明确产品名称和要比较的两个版本后重试；如果版本号未知，可以说明大致时间。"
        )
    if reason in {"comparison_citation_incomplete", "comparison_transition_incomplete"}:
        return (
            "我已取得当前版本与历史版本资料，但本次答复没有完整覆盖两组来源及其中的"
            "关键变化，因此不会把不完整的版本对比直接提供给你。本次没有创建审批，"
            "也没有执行任何变更。请保留当前问题后重试，我会重新核验并给出包含版本差异"
            "和对应依据的答复。"
        )
    if reason == "action_state_unavailable":
        return (
            "我已收到这条消息，但暂时无法从当前记录中确认相关申请的状态，"
            "因此不会猜测它是否已批准或执行。本次没有创建新的人工队列或操作审批，"
            "也没有执行任何变更。请稍后刷新后重试，或在下一条消息中附上对应资源引用。"
        )
    if reason == "needs_clarification":
        return (
            "我还缺少能唯一定位当前问题的信息。请补充你正在使用的产品或版本、"
            "要核验的资源引用，或完整错误现象；只有在问题确实来自某次 API 请求时，"
            "Request ID 和发生时间才有帮助。在确认前不会创建审批或执行任何变更。"
        )
    if reason in {"provider_decision_invalid", "provider_terminal_schema_invalid"}:
        return (
            "本次分析结果不完整，暂时不能据此给出可靠结论。"
            "本次没有创建审批，也没有执行任何变更。"
            "请稍后重试；如果问题持续出现，请保留当前问题和资源引用后重新发送。"
        )
    if reason == "provider_failed":
        return (
            "本次分析服务暂时不可用，尚未执行任何操作，也没有创建审批。"
            "请稍后重试；你已经提供的产品和资源信息会保留在当前对话中。"
        )
    if reason == "credential_redaction_guidance":
        return (
            "出于安全考虑，我已隐藏你提交的完整密钥，也不会要求你再次发送。"
            "请立即在提供商控制台禁用或撤销该密钥并完成轮换，"
            "从代码、日志和共享记录中移除旧值，同时检查近期调用记录。"
            "如需继续核验，请只提供不含密钥内容的 Key Reference。"
        )
    if reason == "tool_failed":
        return (
            "我已尝试核验与你的问题相关的当前业务状态，但读取服务暂时没有返回可验证结果。"
            "因此现在不能确认实时状态；本次没有创建审批，也没有执行任何变更。"
            "请稍后重试；如果你在查询某个账单、Key 或订阅，请核对对应资源引用。"
        )
    if reason == "tool_timeout":
        return (
            "我已尝试查询与你的问题相关的当前业务状态，但查询在限定时间内没有完成，"
            "因此现在不能确认实时状态。本次没有创建审批，也没有执行任何变更。"
            "请稍后使用同一资源引用重试；若问题来自一次 API 请求，再补充 Request ID 和发生时间。"
        )
    if reason == "tool_unavailable":
        return (
            "我已尝试查询与你的问题相关的当前业务状态，但查询服务目前不可用，"
            "因此现在不能确认实时状态。本次没有创建审批，也没有执行任何变更。"
            "请稍后重试，并保留当前账单、Key 或订阅的完整资源引用。"
        )
    if reason == "tool_output_schema_invalid":
        return (
            "我已收到业务状态查询结果，但它没有通过完整性校验，"
            "因此不能把其中内容当作可靠业务信息。本次没有创建审批，也没有执行任何变更。"
            "请稍后使用同一资源引用重试；系统会重新读取，而不会沿用这次无效结果。"
        )
    if reason == "obligation_hard_failure":
        return (
            "我已检查本轮自动处理所需的只读事实，但其中至少一项没有通过可靠性校验，"
            "因此当前无法确认结论。本次没有创建审批，也没有执行任何变更。"
            "请核对资源引用后重试；如果问题持续出现，请保留当前资源引用和发生时间。"
        )
    if (
        reason
        in {
            "mcp_rehandshake_failed",
            "read_capability_failed",
            "tool_transport_budget_exhausted",
        }
        or "retry_exhausted" in reason
    ):
        return (
            "我已尝试读取与你的问题相关的当前业务事实，但该读取能力在有限重试后仍不可用。"
            "目前无法确认实时状态；本次没有创建审批，也没有执行任何变更。"
            "请稍后重试。只有当问题确实来自一次 API 请求时，"
            "再补充 Request ID、发生时间和区域。"
        )
    if reason == "billing_scope_violation":
        return (
            "我已核对当前登录账户，但在当前账户中找不到或无法访问这条账单记录。"
            "为保护账户隔离，这不代表该资源在其他账户中存在；本次已停止读取，"
            "没有创建审批，也没有执行变更。请检查账单编号，并确认当前登录账户是否正确；"
            "如仍需访问，请通过正式管理员授权流程处理。"
        )
    if reason in {
        "ticket_scope_violation",
        "cross_tenant_argument",
        "cross_tenant_observation",
        "observation_scope_mismatch",
        "forbidden_surface",
        "forbidden_tool_surface",
    }:
        return (
            "我已核对当前登录账户的访问范围，但无法在该范围内安全验证你提到的资源。"
            "这不代表该资源在其他账户中存在；本次已停止读取，没有创建审批，也没有执行变更。"
            "请确认当前账户和资源引用，或通过正式管理员授权流程处理访问范围。"
        )
    if reason in {
        "semantic_no_progress",
        "tool_not_allowlisted",
    }:
        return (
            "我已检查当前可用的知识和业务来源，但没有获得能继续推进的新增事实。"
            "因此不会猜测结论；本次没有创建审批，也没有执行任何变更。"
            "请明确你要确认的结论，并补充相关产品版本或资源引用；"
            "若是一次 API 请求失败，再补充 Request ID 和发生时间。"
        )
    if reason == "proposal_not_durable":
        return (
            "本次未能形成可验证的操作申请，因此没有进入审批，也没有执行任何变更。"
            "请稍后重试；系统只会在申请记录完整保存后显示待审批状态。"
        )
    if reason == "citation_binding_incomplete":
        return (
            "我已检查当前证据，但结论与可追溯来源的绑定没有通过完整校验，"
            "因此不会发布未经支持的回答。本次没有创建人工队列或操作审批，"
            "也没有执行任何变更。请缩小到一个具体问题后重试，"
            "或补充产品版本、资源引用和发生时间。"
        )
    if reason == "mixed_account_applicability_incomplete":
        return (
            "我已查到相关产品要求，但本次答复没有同时完成当前账户事实的可追溯核验，"
            "因此现在不能判断你的账户是否满足这些要求。本次没有创建审批，也没有执行"
            "任何变更。请稍后在当前对话中重试，我会重新核对产品资料与账户状态。"
        )
    if reason == "explicit_current_fact_incomplete":
        return (
            "我已读取当前账户的相关业务状态，但本次答复没有把你明确询问的每一项"
            "当前值都与对应来源完整绑定，因此不会用缺项或历史信息补齐结论。"
            "本次没有创建审批，也没有执行任何变更。请稍后在当前对话中重试，"
            "系统会重新核对这些实时字段。"
        )
    if reason == "proposal_eligibility_failed":
        return (
            "相关事实已完成部分核验，但操作申请所需的证据或资源绑定没有通过校验。"
            "本次没有创建审批，也没有执行变更；请核对资源信息后重试。"
        )
    if reason in {"insufficient_evidence", "no_progress"}:
        detail = f"目前还缺少：{missing}。" if missing else "当前事实还不足以支持确定结论。"
        return (
            f"{detail}本次没有创建审批，也没有执行任何操作。"
            "请补充与当前问题直接相关的产品版本、资源引用或期望结论后继续。"
        )
    if reason == "context_budget_exhausted":
        return (
            "我已收到并保留这条消息，但当前问题与必须保留的最近上下文合在一起过长，"
            "本轮无法在不丢失关键含义的情况下可靠处理。本次没有创建人工队列或操作审批，"
            "也没有执行任何变更。请把问题拆成一个目标，并保留相关产品名或资源引用后重试。"
        )
    if "budget" in reason:
        return (
            "我已完成本轮可用范围内的检查，但仍无法安全确认结论。"
            "本次没有创建审批，也没有执行任何操作；请把问题拆成一个明确目标后继续，"
            "或稍后重试。"
        )
    if reason == "out_of_scope":
        return (
            "这个请求不在 SupportGuard 的产品支持范围内，因此没有调用业务工具或执行任何操作。"
            "你可以继续询问产品能力、请求诊断、计费、API Key 或配额问题。"
        )
    if reason == "prohibited":
        return (
            "为保护账户与租户数据，我不能访问、导出或操作其他客户的资源，也不能根据对话中的"
            "权限声明绕过安全策略。本次请求已安全拒绝，没有调用业务工具、创建操作提案或执行变更。"
            "你可以继续询问当前账户下的资源，或通过正式的管理员授权流程处理权限问题。"
        )
    if reason == "rejected":
        return (
            "本轮没有形成可验证的支持结论，也没有调用写入能力或执行任何变更。"
            "你可以换一种方式描述当前问题，或补充相关产品、错误码和资源引用后继续。"
        )
    if reason == "requested_action_unresolved":
        return (
            "我已识别到你的操作请求，但当前证据尚未形成可安全提交的操作申请。"
            "本次没有创建审批，也没有执行任何变更；请核对资源引用后继续。"
        )
    if reason in {"binding_stale", "logical_degradation"}:
        return (
            "审批后的资源状态已经变化，系统已停止执行以避免使用过期信息。"
            "没有重复执行操作；请重新核验后创建新的申请。"
        )
    return (
        "本轮未能完成可靠核验，且没有执行任何操作。"
        "请稍后重试，或补充与当前问题直接相关的产品版本、资源引用或错误现象后继续。"
    )
