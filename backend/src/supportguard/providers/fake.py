from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from supportguard.agent.schemas import (
    AgentDecision,
    CandidateResponse,
    Classification,
    ProviderBoundEvidenceSynthesis,
    ReadPlan,
)
from supportguard.providers.base import (
    ProviderCallResult,
    ProviderUsage,
    RawProviderDecision,
    canonical_transport_record,
    raw_decision_from_typed,
)

OutputT = TypeVar("OutputT", bound=BaseModel)

_CONTEXTUAL_FOLLOW_UP = re.compile(
    r"(?:帮我处理|继续处理|请处理|最先|首先|第一步|先做什么|先应该|应该先|"
    r"下一步|接下来|怎么继续|do it|proceed|handle it|what should i do first|"
    r"what(?:'s| is)? next|where should i start)",
    flags=re.IGNORECASE,
)
_PRODUCT_KNOWLEDGE_QUESTION = re.compile(
    r"(?:"
    r"(?:对比|比较|区别|差异|compare|difference).{0,48}"
    r"(?:版本|修订|revision|version)|"
    r"(?:版本|修订|revision|version).{0,32}"
    r"(?:对比|比较|区别|差异|限制|上限|能力|compare|difference|limit|capability)|"
    r"(?:json|schema|上下文|context).{0,24}"
    r"(?:限制|上限|输出|能力|支持|limit|output|capability|support)"
    r")",
    flags=re.IGNORECASE,
)


class DeterministicFakeProvider:
    """Schema-valid offline model fixture driven only by explicit input fields."""

    mode = "fake"
    model = "deterministic-fake"
    tool_call_mode = "native_fixture"

    def __init__(
        self,
        *,
        delay_seconds: float = 0,
        max_input_tokens: int = 16_000,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.max_input_tokens = max_input_tokens

    async def _delay(self) -> None:
        delay_seconds = getattr(self, "delay_seconds", 0)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

    async def decide(
        self,
        *,
        system: str,
        context: str,
        tools: list[dict[str, Any]],
        prior_turns: list[dict[str, Any]],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[RawProviderDecision]:
        await self._delay()
        transport = canonical_transport_record(
            {
                "system": system,
                "context": context,
                "tools": tools,
                "prior_turns": prior_turns,
                "trace_metadata": trace_metadata,
            }
        )
        payload = json.loads(context)
        allowed = {str(item["function"]["name"]) for item in tools}
        observations = payload.get("latest_observations", [])
        boundary = str(payload.get("trusted_task_state", {}).get("policy_boundary", "allowed"))
        if boundary in {"out_of_scope", "prohibited"}:
            output = AgentDecision.model_validate(
                {
                    "decision_type": "final_candidate",
                    "decision_summary": "Respect the deterministic support and tenant boundary.",
                    "candidate": {
                        "answer": "The request is outside the permitted support boundary.",
                        "action": "reject",
                        "knowledge_chunk_ids": [],
                        "business_source_ids": [],
                        "proposed_arguments": {},
                    },
                }
            )
            return ProviderCallResult(
                raw_decision_from_typed(output),
                1,
                ProviderUsage(),
                dict(trace_metadata),
                transport,
            )
        if not observations:
            message = str(payload["user_goal"])
            knowledge_message = message
            if _CONTEXTUAL_FOLLOW_UP.search(message):
                recent_customer_turns = [
                    str(turn.get("content", "")).strip()
                    for item in payload.get("relevant_history", [])
                    if isinstance(item, dict)
                    for turn in (
                        [item]
                        if item.get("history_kind") == "message"
                        else item.get("current_conversation_recent_messages", [])
                    )
                    if isinstance(turn, dict)
                    and turn.get("role") == "customer"
                    and str(turn.get("content", "")).strip()
                ]
                if recent_customer_turns:
                    knowledge_message = f"{recent_customer_turns[-1]}\n{message}"[-500:]
            names = ["search_knowledge"]
            issue = payload["trusted_task_state"]["issue_type"]
            if issue == "unknown" and any(
                token in message.lower() for token in ("账号", "套餐", "account", "plan")
            ):
                names.append("query_account")
            if issue == "api_diagnostics":
                names.extend(["query_subscription", "query_api_usage"])
            if issue == "billing_refund":
                names.append("query_billing_record")
            if issue == "credential_security":
                names.append("query_api_key_metadata")
            if issue == "entitlement_change":
                names.append("query_subscription")
            if issue == "incident_support":
                names.extend(["query_request_trace", "query_incident_impact"])
            calls = []
            for index, name in enumerate(name for name in names if name in allowed):
                arguments: dict[str, Any] = {}
                if name == "search_knowledge":
                    knowledge_queries = {
                        "billing_refund": "显式重复关系 等额退款提案 等待审批 标准处理",
                        "credential_security": "API Key 泄露后的撤销、审计与人工审批政策",
                        "entitlement_change": "提升并发 优化方案 按政策提案 标准处理",
                    }
                    arguments = {"query": knowledge_queries.get(str(issue), knowledge_message)}
                elif name == "query_api_usage":
                    arguments = {"window": "1m"}
                elif name == "query_billing_record":
                    match = re.search(r"bill_[A-Za-z0-9_-]+", message)
                    arguments = {
                        "billing_record_id": match.group(0) if match else "bill_demo_duplicate"
                    }
                elif name == "query_api_key_metadata":
                    match = re.search(r"key_[A-Za-z0-9_-]+", message)
                    arguments = {"api_key_ref": match.group(0) if match else "key_demo_leaked"}
                elif name in {"query_request_trace", "query_incident_impact"}:
                    match = re.search(r"req_[A-Za-z0-9_-]+", message)
                    arguments = {"request_id": match.group(0) if match else "req_demo_429"}
                elif name == "check_service_status":
                    arguments = {"model": "atlas-chat", "region": "us-east-1"}
                calls.append(
                    {
                        "tool_call_id": f"fake_tool_{len(prior_turns)}_{index}",
                        "call": {"name": name, "arguments": arguments},
                    }
                )
            output = AgentDecision.model_validate(
                {
                    "decision_type": "tool_calls",
                    "decision_summary": "Collect current scoped facts and knowledge evidence.",
                    "tool_calls": calls,
                }
            )
            return ProviderCallResult(
                raw_decision_from_typed(output),
                1,
                ProviderUsage(),
                dict(trace_metadata),
                transport,
            )
        issue = payload["trusted_task_state"]["issue_type"]
        message = str(payload["user_goal"])
        if issue == "unknown":
            output = AgentDecision.model_validate(
                {
                    "decision_type": "needs_clarification",
                    "decision_summary": "Ask for the missing product or failure identity.",
                    "clarification_question": (
                        "请补充产品或模型名称、错误码、请求 ID，或你希望确认的具体能力。"
                    ),
                }
            )
            return ProviderCallResult(
                raw_decision_from_typed(output),
                1,
                ProviderUsage(),
                dict(trace_metadata),
                transport,
            )
        if (
            issue == "api_diagnostics"
            and any(token in message.lower() for token in ("服务", "事故", "status"))
            and "check_service_status" in allowed
            and not any(item.get("tool_name") == "check_service_status" for item in observations)
        ):
            output = AgentDecision.model_validate(
                {
                    "decision_type": "tool_calls",
                    "decision_summary": "Replan after first observations and check service status.",
                    "tool_calls": [
                        {
                            "tool_call_id": f"fake_tool_{len(prior_turns)}_status",
                            "call": {
                                "name": "check_service_status",
                                "arguments": {"model": "atlas-chat", "region": "us-east-1"},
                            },
                        }
                    ],
                }
            )
            return ProviderCallResult(
                raw_decision_from_typed(output),
                1,
                ProviderUsage(),
                dict(trace_metadata),
                transport,
            )
        # Mirror the production citation boundary: background-only chunks may
        # remain visible for context, but a candidate must not cite them as
        # material support.  The deterministic provider therefore selects only
        # spans that the retrieval runtime marked claim-eligible.
        evidence = [
            item
            for item in payload.get("retrieved_evidence", [])
            if item.get("supporting_span_eligible") is True
        ]
        citation_bindings = [
            {"citation_binding_id": item["citation_binding_id"]} for item in evidence
        ]
        source_ids = [
            source["source_id"]
            for observation in observations
            if observation.get("tool_name") != "search_knowledge"
            for source in observation.get("source_refs", [])
        ]
        if issue == "billing_refund":
            billing = next(
                (
                    item
                    for item in observations
                    if item.get("tool_name") == "query_billing_record"
                    and item.get("status") == "ok"
                ),
                None,
            )
            if billing is not None and billing.get("data", {}).get("duplicate_of"):
                candidate = {
                    "answer": "已核验重复扣费事实，生成退款提案并等待人工审批。",
                    "action": "refund_proposal",
                    "knowledge_chunk_ids": [item["chunk_id"] for item in evidence],
                    "knowledge_citations": citation_bindings,
                    "business_source_ids": source_ids,
                    "proposed_arguments": {
                        "billing_record_id": billing["data"]["billing_record_id"],
                        "refund_reason": "Explicit duplicate billing relation verified.",
                    },
                }
            else:
                candidate = self._manual_candidate("账单证据不足，已转人工处理。")
        elif issue == "credential_security":
            metadata = next(
                (
                    item
                    for item in observations
                    if item.get("tool_name") == "query_api_key_metadata"
                    and item.get("status") == "ok"
                    and item.get("data", {}).get("status") == "active"
                ),
                None,
            )
            if metadata is None:
                candidate = self._manual_candidate("密钥元数据不足，已转人工处理。")
            else:
                candidate = {
                    "answer": "已核验当前 Key 元数据，生成撤销提案并等待人工审批。",
                    "action": "api_key_revocation_proposal",
                    "knowledge_chunk_ids": [item["chunk_id"] for item in evidence],
                    "knowledge_citations": citation_bindings,
                    "business_source_ids": source_ids,
                    "proposed_arguments": {
                        "api_key_id": metadata["data"]["api_key_id"],
                        "reason": "Customer reported a suspected credential exposure.",
                    },
                }
        elif issue == "entitlement_change":
            subscription = next(
                (
                    item
                    for item in observations
                    if item.get("tool_name") == "query_subscription" and item.get("status") == "ok"
                ),
                None,
            )
            target_match = re.search(
                r"(?:并发|concurrency).*?(?:提升到|提高到|increase(?:\s+to)?)[^0-9]{0,4}([0-9]+)",
                message.lower(),
            )
            if subscription is None or target_match is None:
                candidate = self._manual_candidate("缺少明确且可校验的目标配额，已转人工处理。")
            else:
                candidate = {
                    "answer": "已核验订阅与目标值，生成配额变更提案并等待人工审批。",
                    "action": "entitlement_change_proposal",
                    "knowledge_chunk_ids": [item["chunk_id"] for item in evidence],
                    "knowledge_citations": citation_bindings,
                    "business_source_ids": source_ids,
                    "proposed_arguments": {
                        "subscription_id": subscription["data"]["subscription_id"],
                        "change_type": "quota_change",
                        "target": {"concurrency_limit": int(target_match.group(1))},
                        "reason": "Explicit customer concurrency target validated against policy.",
                    },
                }
        elif evidence:
            candidate = {
                "answer": self._grounded_answer(
                    issue=str(issue),
                    message=message,
                    observations=observations,
                    evidence=evidence,
                ),
                "action": "answer",
                "knowledge_chunk_ids": [item["chunk_id"] for item in evidence],
                "knowledge_citations": citation_bindings,
                "business_source_ids": source_ids,
                "proposed_arguments": {},
            }
        else:
            candidate = self._manual_candidate("当前证据不足，已转人工处理。")
        if candidate["action"] != "manual_takeover":
            candidate["material_claims"] = [
                {
                    "text": candidate["answer"],
                    "knowledge_locator_hashes": [item["source_locator_hash"] for item in evidence],
                    "citation_binding_ids": [item["citation_binding_id"] for item in evidence],
                    "observation_source_ids": source_ids,
                }
            ]
        output = AgentDecision.model_validate(
            {
                "decision_type": "final_candidate",
                "decision_summary": "Produced a grounded candidate from current observations.",
                "candidate": candidate,
            }
        )
        return ProviderCallResult(
            raw_decision_from_typed(output),
            1,
            ProviderUsage(),
            dict(trace_metadata),
            transport,
        )

    @staticmethod
    def _grounded_answer(
        *,
        issue: str,
        message: str,
        observations: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> str:
        if issue == "api_diagnostics":
            usage: dict[str, Any] = next(
                (
                    item.get("data", {})
                    for item in observations
                    if item.get("tool_name") == "query_api_usage" and item.get("status") == "ok"
                ),
                {},
            )
            balance = usage.get("remaining_balance")
            currency = usage.get("balance_currency", "USD")
            current = usage.get("concurrency_current")
            peak = usage.get("concurrency_peak")
            if "concurrency" in message.lower():
                return (
                    "这不是余额不足：余额与并发限制是两套独立控制。"
                    "concurrency_limit_exceeded 表示请求发生时同时执行的请求数达到套餐并发上限。"
                    f"当前观测到余额 {balance} {currency}、并发 {current}（本窗口峰值 {peak}）；"
                    "该快照不等于报错瞬间，因此只能确认当前状态，不能反推当时的精确并发。"
                    "请降低并行请求、采用带抖动的退避重试；若要定位单次失败，"
                    "请补充请求 ID 和发生时间。"
                )
            return (
                "429 表示请求受到速率或并发限制，并不等同于余额不足。"
                f"当前观测到余额 {balance} {currency}、本窗口请求数 "
                f"{usage.get('request_count')}、并发峰值 {peak}。"
                "请按错误子码区分 RPM 与并发限制，并使用退避重试；若需确认历史触发原因，"
                "还需要失败请求 ID 与发生时间。"
            )
        spans = [
            str(item.get("supporting_span", "")).strip()
            for item in evidence
            if str(item.get("supporting_span", "")).strip()
        ]
        if spans:
            return f"根据当前有效产品文档：{spans[0]}"
        return "已找到当前有效产品文档，但缺少足以形成具体结论的支持片段。"

    @staticmethod
    def _manual_candidate(answer: str) -> dict[str, Any]:
        return {
            "answer": answer,
            "action": "manual_takeover",
            "knowledge_chunk_ids": [],
            "business_source_ids": [],
            "proposed_arguments": {},
        }

    async def generate(
        self,
        *,
        system: str,
        user: str,
        output_schema: type[OutputT],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[OutputT]:
        await self._delay()
        transport = canonical_transport_record(
            {
                "system": system,
                "user": user,
                "output_schema": output_schema.model_json_schema(),
                "trace_metadata": trace_metadata,
            }
        )
        payload = json.loads(user)
        if output_schema is Classification:
            text = str(payload.get("current_turn", payload.get("ticket", ""))).lower()
            history_text = "\n".join(
                str(item.get("content", "")).lower()
                for item in payload.get("recent_conversation", [])
                if isinstance(item, dict)
            )
            contextual_text = text
            if len(text) <= 80 and _CONTEXTUAL_FOLLOW_UP.search(text):
                contextual_text = f"{history_text}\n{text}"
            support_subject = "customer_problem"
            if re.search(
                r"(?:你是谁|什么助手|who are you|supportguard.*(?:是谁|是什么))",
                text,
                flags=re.IGNORECASE,
            ):
                issue, risk, realtime = "product_knowledge", "low", False
                support_subject = "supportguard_identity"
            elif re.search(
                r"(?:supportguard.*(?:能力|功能)|你能做什么|你的能力)",
                f"{history_text}\n{text}",
                flags=re.IGNORECASE | re.DOTALL,
            ) or ("产品能力" in text and "supportguard" in history_text):
                issue, risk, realtime = "product_knowledge", "low", False
                support_subject = "supportguard_capabilities"
            elif any(token in contextual_text for token in ("退款", "扣费", "账单")):
                issue, risk, realtime = "billing_refund", "high", True
            elif any(token in contextual_text for token in ("429", "rate_limit")):
                issue, risk, realtime = "api_diagnostics", "low", True
            elif any(token in contextual_text for token in ("401", "api key", "密钥")):
                issue, risk, realtime = "credential_security", "high", True
            elif any(token in contextual_text for token in ("事故", "500", "status")):
                issue, risk, realtime = "incident_support", "medium", True
            elif any(
                token in contextual_text for token in ("配额", "提升并发", "套餐变更", "quota")
            ):
                issue, risk, realtime = "entitlement_change", "high", True
            elif any(
                token in text
                for token in ("json object", "是否支持", "产品能力", "功能限制", "文档")
            ) or _PRODUCT_KNOWLEDGE_QUESTION.search(text):
                issue, risk, realtime = "product_knowledge", "low", False
            else:
                issue, risk, realtime = "unknown", "low", False
            prohibited_markers = (
                "忽略系统",
                "绕过权限",
                "绕过审批",
                "越权",
                "其他客户数据",
                "其他租户数据",
                "导出其他客户",
                "导出其他租户",
                "cross-tenant",
                "cross tenant",
                "bypass policy",
                "bypass approval",
                "exfiltrate",
            )
            policy_boundary = (
                "prohibited" if any(marker in text for marker in prohibited_markers) else "allowed"
            )
            output = output_schema.model_validate(
                {
                    "issue_type": issue,
                    "risk": risk,
                    "policy_boundary": policy_boundary,
                    "requested_action": (
                        "refund"
                        if issue == "billing_refund"
                        and (
                            "退款" in text
                            or bool(
                                re.search(
                                    r"(?:重复扣费|重复收费|duplicate\s+charge).{0,24}"
                                    r"(?:按.{0,6}政策处理|处理|refund)",
                                    text,
                                    flags=re.IGNORECASE,
                                )
                            )
                            or (
                                contextual_text != text
                                and not any(
                                    marker in history_text
                                    for marker in ("等待人工审批", "申请已生成", "pending")
                                )
                            )
                        )
                        else "api_key_revocation"
                        if issue == "credential_security"
                        and any(token in text for token in ("撤销", "吊销", "revoke"))
                        else "entitlement_change"
                        if issue == "entitlement_change"
                        else "none"
                    ),
                    "requested_concurrency_limit": (
                        int(match.group(1))
                        if issue == "entitlement_change"
                        and (match := re.search(r"(?:并发|concurrency).*?([0-9]+)", text))
                        else None
                    ),
                    "needs_realtime_facts": realtime,
                    "support_subject": support_subject,
                    "rationale": "deterministic offline classification",
                }
            )
            return ProviderCallResult(output, 1, ProviderUsage(), dict(trace_metadata), transport)
        if output_schema is ReadPlan:
            issue = payload["classification"]["issue_type"]
            calls: list[dict[str, object]] = []
            if issue in {"api_diagnostics", "credential_security", "billing_refund"}:
                calls.append({"name": "query_account", "arguments": {}})
            if issue == "api_diagnostics":
                calls.append({"name": "query_api_usage", "arguments": {}})
            output = output_schema.model_validate({"calls": calls})
            return ProviderCallResult(output, 1, ProviderUsage(), dict(trace_metadata), transport)
        if output_schema is ProviderBoundEvidenceSynthesis:
            context_payload = payload
            if isinstance(payload.get("same_redacted_context"), str):
                context_payload = json.loads(payload["same_redacted_context"])
            evidence = [
                item
                for item in context_payload.get("retrieved_evidence", [])
                if item.get("supporting_span_eligible") is True
            ]
            observations = [
                item
                for item in context_payload.get("latest_observations", [])
                if item.get("tool_name") != "search_knowledge" and item.get("status") == "ok"
            ]
            citation_ids = [
                str(item["citation_binding_id"])
                for item in evidence
                if item.get("citation_binding_id")
            ]
            source_ids = [
                str(source["source_id"])
                for observation in observations
                for source in observation.get("source_refs", [])
                if source.get("source_id")
            ]
            issue = str(
                context_payload.get("trusted_task_state", {}).get(
                    "issue_type",
                    "unknown",
                )
            )
            answer = {
                "billing_refund": (
                    "已核验当前账单与重复扣费退款政策；系统将按确定性规则形成提案并等待人工审批。"
                ),
                "credential_security": (
                    "已核验当前 API Key 元数据与撤销政策；"
                    "系统将按确定性规则形成提案并等待人工审批。"
                ),
                "entitlement_change": (
                    "已核验当前订阅与配额政策；系统将按确定性规则形成提案并等待人工审批。"
                ),
            }.get(issue, "已依据当前业务事实与产品政策完成核验。")
            output = output_schema.model_validate(
                {
                    "schema_version": "bound-evidence-synthesis.v1",
                    "answer": answer,
                    "material_claims": [
                        {
                            "text": answer,
                            "citation_binding_ids": citation_ids,
                            "observation_source_ids": source_ids,
                        }
                    ],
                }
            )
            return ProviderCallResult(
                output,
                1,
                ProviderUsage(),
                dict(trace_metadata),
                transport,
            )
        if output_schema is CandidateResponse:
            evidence = payload.get("evidence", [])
            observations = payload.get("tool_observations", [])
            chunk_ids = [item["chunk_id"] for item in evidence]
            source_ids = [
                source["source_id"]
                for observation in observations
                for source in observation.get("source_refs", [])
            ]
            if payload["classification"]["issue_type"] == "billing_refund":
                match = re.search(r"bill_[A-Za-z0-9_-]+", str(payload["ticket"]))
                billing_id = match.group(0) if match else "bill_demo_duplicate"
                action = "refund_proposal"
                answer = "检测到显式重复扣费事实，已生成退款提案，等待人工审批。"
                proposed = {
                    "billing_record_id": billing_id,
                    "refund_reason": (
                        "Explicit duplicate billing relation requires human approval."
                    ),
                }
            elif not chunk_ids:
                action = "manual_takeover"
                answer = "现有证据不足，已保守转人工处理。"
                proposed = {}
            else:
                action = "answer"
                answer = "已结合当前套餐、用量与产品文档定位问题；请按引用步骤处理。"
                proposed = {}
            output = output_schema.model_validate(
                {
                    "answer": answer,
                    "action": action,
                    "knowledge_chunk_ids": chunk_ids,
                    "business_source_ids": source_ids,
                    "proposed_arguments": proposed,
                }
            )
            return ProviderCallResult(output, 1, ProviderUsage(), dict(trace_metadata), transport)
        raise TypeError(f"unsupported fake schema: {output_schema.__name__}")
