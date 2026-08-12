from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from supportguard.agent.evidence import observation_is_fresh
from supportguard.agent.schemas import CandidateResponse
from supportguard.tools.gateway import ReadToolName

CurrentFactRequirements = dict[str, tuple[ReadToolName, tuple[str, ...]]]


@dataclass(frozen=True)
class ReferentialBillingResolution:
    """Customer-owned resource reference for a read-only billing follow-up."""

    status: Literal["resolved", "unresolved", "not_applicable"]
    reason_code: Literal[
        "current_message_reference",
        "history_unique_reference",
        "current_message_ambiguous",
        "history_ambiguous",
        "history_reference_missing",
        "not_referential_billing_question",
        "unsafe_classification",
    ]
    billing_record_id: str | None = None


_EXPLICIT_QUERY = re.compile(
    r"(?:"
    r"请(?:告诉|查询|查看|显示|确认|核对)|"
    r"帮我(?:查|看|确认|核对)|"
    r"(?:多少|是什么|怎么样|是否|是不是|有没有|达到|打满)|"
    r"\b(?:tell me|show me|check my|what is|how much|whether|am i)\b"
    r")",
    re.I,
)
_POLICY_OR_OPERATIONAL_REQUEST = re.compile(
    r"(?:"
    r"政策|规则|依据|条件|原因|为什么|如何|怎么|建议|方案|流程|"
    r"调整|变更|提升|降低|扩容|优化|申请|支持吗|能否|可以吗|"
    r"\b(?:policy|rule|requirement|why|how|recommend|procedure|"
    r"change|increase|decrease|upgrade|optimi[sz]e|apply|supported)\b"
    r")",
    re.I,
)
_FIELDS: tuple[tuple[str, re.Pattern[str], ReadToolName, tuple[str, ...]], ...] = (
    (
        "account_status",
        re.compile(r"(?:账户状态|账号状态|\baccount status\b)", re.I),
        "query_account",
        ("account_status",),
    ),
    (
        "security_status",
        re.compile(r"(?:安全状态|风控状态|\bsecurity status\b)", re.I),
        "query_account",
        ("security_status",),
    ),
    (
        "region",
        re.compile(
            r"(?:账户区域|账号区域|所在区域|部署区域|\b(?:account )?region\b)",
            re.I,
        ),
        "query_account",
        ("region",),
    ),
    (
        "remaining_balance",
        re.compile(r"(?:余额|可用金额|\b(?:remaining )?balance\b)", re.I),
        "query_api_usage",
        ("remaining_balance", "balance_currency"),
    ),
    (
        "concurrency_limit",
        re.compile(
            r"(?:并发(?:上限|限制|额度)|套餐并发|\bconcurrency (?:limit|quota)\b)",
            re.I,
        ),
        "query_subscription",
        ("concurrency_limit",),
    ),
    (
        "rpm_limit",
        re.compile(
            r"(?:每分钟请求(?:上限|限制|额度)?|RPM(?:\s*(?:上限|限制))?|"
            r"\b(?:rpm|rate[ -]?limit)\b)",
            re.I,
        ),
        "query_subscription",
        ("rpm_limit",),
    ),
    (
        "plan",
        re.compile(
            r"(?:当前套餐|订阅套餐|套餐级别|\b(?:current )?(?:plan|tier)\b)",
            re.I,
        ),
        "query_subscription",
        ("plan",),
    ),
    (
        "subscription_status",
        re.compile(r"(?:订阅状态|套餐状态|\bsubscription status\b)", re.I),
        "query_subscription",
        ("status",),
    ),
    (
        "current_concurrency",
        re.compile(
            r"(?:当前并发|实时并发|并发用量|并发使用量|打满并发|达到并发|"
            r"\bcurrent concurrency\b|\bconcurrency usage\b)",
            re.I,
        ),
        "query_api_usage",
        ("concurrency_current",),
    ),
    (
        "request_count",
        re.compile(
            r"(?:当前请求数|请求用量|实时请求量|\bcurrent request count\b|"
            r"\brequest usage\b)",
            re.I,
        ),
        "query_api_usage",
        ("request_count",),
    ),
)
_SATURATION_QUERY = re.compile(
    r"(?:打满|达到|饱和|\b(?:at|reached|hit)\b.{0,12}\b(?:limit|quota)\b)",
    re.I,
)
_BILLING_ID = re.compile(
    r"(?<![A-Za-z0-9_.:-])bill[-_][a-z0-9]"
    r"(?:[a-z0-9._:-]{0,61}[a-z0-9])?(?![A-Za-z0-9_-])",
    re.I,
)
_BILLING_POLICY_OR_PROCESS = re.compile(
    r"(?:"
    r"退款政策|退费政策|计费政策|政策|规则|标准处理|处理流程|处理方式|"
    r"如何处理|怎么处理|应当如何|应该如何|是否符合|是否适用|适用条件|"
    r"\b(?:refund|billing)\s+(?:policy|rule|procedure|process)\b|"
    r"\b(?:policy|rule|procedure|process|eligib(?:le|ility))\b|"
    r"\bhow\s+(?:should|do|would)\b.{0,24}\b(?:handle|process|refund)\b"
    r")",
    re.I,
)
_BILLING_ANAPHORA = re.compile(
    r"(?:"
    r"(?:这|该|那)(?:一)?(?:条|笔|个|项)?(?:重复)?(?:扣费|收费|账单|费用|交易)|"
    r"(?:这|该|那)(?:一)?(?:条|笔|个|项)?记录|"
    r"\b(?:this|that)\s+(?:duplicate\s+)?(?:charge|bill|billing\s+record|invoice|"
    r"transaction)\b"
    r")",
    re.I,
)
_CLAIM_CORRECTION = re.compile(
    r"(?:而是|(?:但|不过)?实际(?:上)?(?:是|为)?|应(?:当|该)?(?:是|为)|"
    r"更正(?:为|成)?|纠正(?:为|成)?)"
    r"|\b(?:but\s+actually|actually|rather|instead)\b",
    re.I,
)
_VALUE_NEGATION_PREFIX = re.compile(
    r"(?:不是|并非|不为|不等于|非|不)\s*$"
    r"|(?:\bis\s+not|\bisn['’]?t|\bnot(?:\s+\w+){0,3})\s*$",
    re.I,
)
_VALUE_NEGATION_SUFFIX = re.compile(
    r"^\s*(?:不是|并非|不(?:正确|准确|属实|是)|"
    r"is\s+not|isn['’]?t|incorrect|wrong|not\s+correct)",
    re.I,
)
_CLAIM_STATEMENT_BOUNDARY = re.compile(r"[。；;！!\n]+")
_CLAIM_CLAUSE_BOUNDARY = re.compile(r"[，,]+")
_EPISTEMIC_UNCERTAINTY = re.compile(
    r"(?:无法|不能|未能|尚未|不(?:能|可)).{0,12}(?:确认|确定|判断|核实|验证|证明)"
    r"|未(?:经)?(?:确认|确定|核实|验证)"
    r"|(?:未知|不清楚|不确定|待确认|有待确认|尚待确认|可能|也许|大概|疑似)"
    r"|(?:cannot|can['’]?t|unable\s+to|not\s+able\s+to).{0,16}"
    r"(?:confirm|determine|verify|establish)"
    r"|\bnot\s+(?:confirmed|verified|known|established)\b"
    r"|\b(?:unknown|uncertain|unconfirmed|maybe|possibly|probably|might)\b"
    r"|\bmay\s+be\b",
    re.I,
)
_CLAIM_NON_ASSERTIVE = re.compile(
    r"[？?]|(?:是否|是不是|能否|可否)|(?:吗|呢)\s*$"
    r"|^\s*(?:is|are|was|were|can|could|would|does|do)\b",
    re.I,
)


def _billing_ids(text: str) -> list[str]:
    """Return unique opaque billing references while preserving source spelling."""

    unique: dict[str, str] = {}
    for match in _BILLING_ID.finditer(text):
        value = match.group(0)
        unique.setdefault(value.casefold(), value)
    return list(unique.values())


def resolve_referential_billing_reference(
    state: Mapping[str, Any],
) -> ReferentialBillingResolution:
    """Resolve one billing reference without treating history as current truth.

    The current accepted customer message owns the question. Prior customer
    messages may supply one unambiguous opaque identifier for an anaphoric,
    read-only policy follow-up. Assistant text, summaries, model output, memory,
    retrieved content, and tool observations are deliberately excluded.
    """

    classification = state.get("classification", {})
    if not (
        classification.get("policy_boundary", "allowed") == "allowed"
        and classification.get("issue_type") == "billing_refund"
        and classification.get("requested_action", "none") == "none"
    ):
        return ReferentialBillingResolution(
            status="not_applicable",
            reason_code="unsafe_classification",
        )

    message = str(state.get("redacted_message", "")).strip()
    current_ids = _billing_ids(message)
    is_policy_question = bool(message and _BILLING_POLICY_OR_PROCESS.search(message))
    is_referential = bool(current_ids or _BILLING_ANAPHORA.search(message))
    if not (is_policy_question and is_referential):
        return ReferentialBillingResolution(
            status="not_applicable",
            reason_code="not_referential_billing_question",
        )
    if len(current_ids) == 1:
        return ReferentialBillingResolution(
            status="resolved",
            reason_code="current_message_reference",
            billing_record_id=current_ids[0],
        )
    if len(current_ids) > 1:
        return ReferentialBillingResolution(
            status="unresolved",
            reason_code="current_message_ambiguous",
        )

    history_ids: dict[str, str] = {}
    for item in state.get("relevant_history", []):
        if (
            not isinstance(item, dict)
            or item.get("history_kind") != "message"
            or item.get("role") != "customer"
        ):
            continue
        for value in _billing_ids(str(item.get("content", ""))):
            history_ids.setdefault(value.casefold(), value)
    if len(history_ids) == 1:
        return ReferentialBillingResolution(
            status="resolved",
            reason_code="history_unique_reference",
            billing_record_id=next(iter(history_ids.values())),
        )
    return ReferentialBillingResolution(
        status="unresolved",
        reason_code=("history_ambiguous" if history_ids else "history_reference_missing"),
    )


def current_run_billing_observation(
    state: Mapping[str, Any],
    billing_record_id: str,
) -> dict[str, Any] | None:
    """Return only a current-run successful read of the exact resolved record."""

    run_id = state.get("run_id")
    if not run_id or not billing_record_id:
        return None
    return next(
        (
            item
            for item in reversed(state.get("tool_observations", []))
            if item.get("run_id") == run_id
            and item.get("tool_name") == "query_billing_record"
            and item.get("status") == "ok"
            and item.get("source_refs")
            and isinstance(item.get("data"), dict)
            and str(item["data"].get("billing_record_id", "")).casefold()
            == billing_record_id.casefold()
        ),
        None,
    )


def requested_current_fact_requirements(
    state: Mapping[str, Any],
) -> CurrentFactRequirements:
    """Derive authoritative field obligations for an explicit current-fact question.

    The primary issue label is not an exclusive evidence namespace. This bounded
    contract applies only to allowed, read-only fact questions. Explanations,
    recommendations, policy questions, action requests, and entitlement-change
    semantics keep their existing Agent paths.
    """

    classification = state.get("classification", {})
    message = str(state.get("redacted_message", "")).strip()
    if not (
        message
        and classification.get("policy_boundary", "allowed") == "allowed"
        and classification.get("issue_type") != "entitlement_change"
        and classification.get("requested_action", "none") == "none"
        and classification.get("needs_realtime_facts") is True
        and _EXPLICIT_QUERY.search(message)
        and _POLICY_OR_OPERATIONAL_REQUEST.search(message) is None
    ):
        return {}

    requirements: CurrentFactRequirements = {
        fact_name: (tool_name, data_fields)
        for fact_name, pattern, tool_name, data_fields in _FIELDS
        if pattern.search(message)
    }
    if "current_concurrency" in requirements and _SATURATION_QUERY.search(message):
        requirements.setdefault(
            "concurrency_limit",
            ("query_subscription", ("concurrency_limit",)),
        )
    return requirements


def current_run_tool_observation(
    state: Mapping[str, Any],
    tool_name: ReadToolName,
) -> dict[str, Any] | None:
    run_id = state.get("run_id")
    if not run_id:
        return None
    return next(
        (
            item
            for item in reversed(state.get("tool_observations", []))
            if item.get("run_id") == run_id
            and item.get("tool_name") == tool_name
            and item.get("status") == "ok"
            and item.get("source_refs")
            and isinstance(item.get("data"), dict)
        ),
        None,
    )


def requested_current_fact_observation(
    state: Mapping[str, Any],
    *,
    tool_name: ReadToolName,
    data_fields: tuple[str, ...],
) -> dict[str, Any] | None:
    observation = current_run_tool_observation(state, tool_name)
    if observation is None:
        return None
    data = observation["data"]
    if not all(field in data and data[field] is not None for field in data_fields):
        return None
    return observation


def _fact_field_pattern(fact_name: str) -> re.Pattern[str] | None:
    return next((pattern for name, pattern, _, _ in _FIELDS if name == fact_name), None)


def _claim_fields(text: str) -> set[str]:
    return {name for name, pattern, _, _ in _FIELDS if pattern.search(text)}


def _fact_bound_claim_segments(text: str, fact_name: str) -> list[str]:
    """Return affirmative-candidate segments bound to the requested fact.

    A plain statement must carry the target field in the same clause as its
    value. A final correction such as ``状态不是 suspended，而是 active`` may
    inherit that field only when the correction suffix does not name a
    different current-fact field.
    """

    field_pattern = _fact_field_pattern(fact_name)
    if field_pattern is None:
        return []
    normalized = " ".join(text.casefold().split())
    segments: list[str] = []
    for statement in _CLAIM_STATEMENT_BOUNDARY.split(normalized):
        statement = statement.strip()
        if not statement or _CLAIM_NON_ASSERTIVE.search(statement):
            continue
        corrections = list(_CLAIM_CORRECTION.finditer(statement))
        if corrections:
            correction = corrections[-1]
            effective = statement[correction.end() :].strip()
            effective_fields = _claim_fields(effective)
            if (
                effective
                and field_pattern.search(statement)
                and (not effective_fields or fact_name in effective_fields)
            ):
                segments.append(effective)
            continue
        segments.extend(
            clause.strip()
            for clause in _CLAIM_CLAUSE_BOUNDARY.split(statement)
            if clause.strip() and field_pattern.search(clause)
        )
    return segments


def _value_span_is_negated(text: str, start: int, end: int) -> bool:
    return bool(
        _VALUE_NEGATION_PREFIX.search(text[max(0, start - 32) : start])
        or _VALUE_NEGATION_SUFFIX.search(text[end : min(len(text), end + 32)])
    )


def _literal_value_spans(text: str, value: str) -> list[tuple[int, int]]:
    escaped = re.escape(value)
    if re.fullmatch(r"[a-z0-9_ -]+", value, re.I):
        pattern = re.compile(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", re.I)
    else:
        pattern = re.compile(escaped, re.I)
    return [match.span() for match in pattern.finditer(text)]


def _numeric_value_present(text: str, value: object) -> bool:
    raw_value = str(value).strip()
    if isinstance(value, bool) or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", raw_value):
        return False
    expected = float(raw_value)
    return any(
        abs(float(match.group(0)) - expected) < 1e-9
        and not _value_span_is_negated(text, match.start(), match.end())
        for match in re.finditer(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?", text)
    )


def _fact_value_present(
    text: str,
    *,
    fact_name: str,
    data: dict[str, Any],
) -> bool:
    field_name = {
        "subscription_status": "status",
        "current_concurrency": "concurrency_current",
    }.get(fact_name, fact_name)
    value = data.get(field_name)
    if value is None:
        return False
    value_text = str(value).strip().casefold()
    if not value_text:
        return False
    status_aliases = {
        "active": ("active", "正常", "有效", "已启用"),
        "normal": ("normal", "正常"),
        "inactive": ("inactive", "未启用", "已停用"),
        "suspended": ("suspended", "已暂停", "暂停"),
    }
    for segment in _fact_bound_claim_segments(text, fact_name):
        if _EPISTEMIC_UNCERTAINTY.search(segment):
            continue
        if fact_name == "remaining_balance":
            currency = str(data.get("balance_currency") or "").casefold()
            if _numeric_value_present(segment, value) and bool(
                currency and (currency in segment or (currency == "usd" and "$" in segment))
            ):
                return True
            continue
        if _numeric_value_present(segment, value):
            return True
        if any(
            not _value_span_is_negated(segment, start, end)
            for alias in status_aliases.get(value_text, (value_text,))
            for start, end in _literal_value_spans(segment, alias.casefold())
        ):
            return True
    return False


def requested_current_fact_status(
    state: Mapping[str, Any],
    candidate: CandidateResponse,
) -> tuple[list[str], list[str]]:
    """Return missing and stale obligations for one model-proposed answer."""

    requirements = requested_current_fact_requirements(state)
    if not requirements or candidate.action != "answer":
        return [], []
    missing: list[str] = []
    stale: list[str] = []
    for fact_name, (tool_name, data_fields) in requirements.items():
        observation = requested_current_fact_observation(
            state,
            tool_name=tool_name,
            data_fields=data_fields,
        )
        if observation is None:
            missing.append(f"current_fact_observation:{fact_name}")
            continue
        if not observation_is_fresh(observation):
            stale.append(f"current_fact:{fact_name}")
            continue
        source_ids = {
            str(source.get("source_id"))
            for source in observation.get("source_refs", [])
            if source.get("source_id")
        }
        bound_claim_text = "\n".join(
            claim.text
            for claim in candidate.material_claims
            if source_ids & set(claim.observation_source_ids)
        )
        if (
            not source_ids
            or not bound_claim_text
            or not _fact_value_present(
                bound_claim_text,
                fact_name=fact_name,
                data=observation["data"],
            )
        ):
            missing.append(f"current_fact_claim:{fact_name}")
    return list(dict.fromkeys(missing)), list(dict.fromkeys(stale))


def requested_current_fact_reads_complete(state: Mapping[str, Any]) -> bool:
    requirements = requested_current_fact_requirements(state)
    return bool(requirements) and all(
        requested_current_fact_observation(
            state,
            tool_name=tool_name,
            data_fields=data_fields,
        )
        is not None
        for tool_name, data_fields in requirements.values()
    )


def requested_current_fact_contract_valid(
    state: Mapping[str, Any],
    candidate: CandidateResponse,
) -> bool:
    requirements = requested_current_fact_requirements(state)
    missing, stale = requested_current_fact_status(state, candidate)
    return bool(requirements) and not missing and not stale


def requested_current_fact_projection(
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project bounded authoritative facts into the Provider's trusted task state."""

    requirements = requested_current_fact_requirements(state)
    if not requirements:
        return None
    facts: list[dict[str, Any]] = []
    for fact_name, (tool_name, data_fields) in requirements.items():
        observation = requested_current_fact_observation(
            state,
            tool_name=tool_name,
            data_fields=data_fields,
        )
        facts.append(
            {
                "field": fact_name,
                "tool_name": tool_name,
                "status": "observed" if observation is not None else "missing",
                "freshness_status": (
                    observation.get("freshness_status") if observation is not None else None
                ),
                "values": (
                    {field: observation["data"].get(field) for field in data_fields}
                    if observation is not None
                    else {}
                ),
                "source_ids": (
                    [
                        str(source.get("source_id"))
                        for source in observation.get("source_refs", [])
                        if source.get("source_id")
                    ]
                    if observation is not None
                    else []
                ),
            }
        )
    return {
        "required_fields": list(requirements),
        "reads_complete": requested_current_fact_reads_complete(state),
        "facts": facts,
        "instruction": (
            "Answer every requested field from its fresh current-run Observation. "
            "Each material claim must cite an observation_source_id from the matching "
            "fact projection and must state the observed value. If a projection is "
            "stale, say that the current value cannot be confirmed and do not publish "
            "that stale value. Do not infer a missing field from conversation history."
        ),
    }
