from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from supportguard.agent.current_facts import current_run_tool_observation
from supportguard.tools.gateway import ReadToolName

ApiDiagnosticRequirements = dict[ReadToolName, tuple[str, ...]]

_RATE_LIMIT_SIGNAL: Final = re.compile(
    r"(?:\b429\b|concurrency[_ -]?limit[_ -]?exceeded|rate[_ -]?limit(?:ed|ing)?)",
    re.I,
)
_OBSERVED_FAILURE: Final = re.compile(
    r"(?:返回|报(?:错)?|出现|遇到|失败|触发|一并发就|"
    r"\b(?:get|got|getting|return(?:s|ed)?|fail(?:s|ed|ing)?|hit|encounter(?:ed)?)\b)",
    re.I,
)
_CUSTOMER_STATE_CONTEXT: Final = re.compile(
    r"(?:余额|账户|账号|套餐|订阅|当前并发|并发上限|"
    r"\b(?:balance|account|plan|subscription|current concurrency)\b)",
    re.I,
)
_CONTROL_SEPARATION: Final = re.compile(
    r"(?:独立|分开|不同|无关|不会|不能|does not|doesn't|independent|separate)",
    re.I,
)
_REQUEST_REFERENCE: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:req(?:uest)?|trace)(?:[-_.:][A-Za-z0-9]+)+",
    re.I,
)
_GENERIC_REQUEST_LABELS: Final = {
    "request_id",
    "request-id",
    "request.id",
    "request:id",
    "trace_id",
    "trace-id",
    "trace.id",
    "trace:id",
}

_REQUIRED_READS: Final[tuple[tuple[ReadToolName, tuple[str, ...]], ...]] = (
    ("search_knowledge", ("evidence", "index_version")),
    (
        "query_subscription",
        ("subscription_id", "plan", "status", "concurrency_limit", "version"),
    ),
    (
        "query_api_usage",
        (
            "remaining_balance",
            "balance_currency",
            "concurrency_current",
            "freshness_status",
            "resource_version",
        ),
    ),
)


def _customer_messages(state: Mapping[str, Any]) -> tuple[str, ...]:
    messages = [str(state.get("redacted_message", "")).strip()]
    messages.extend(
        str(item.get("content", "")).strip()
        for item in state.get("relevant_history", [])
        if isinstance(item, dict)
        and item.get("history_kind") == "message"
        and item.get("role") == "customer"
    )
    return tuple(message for message in messages if message)


def _request_references(message: str) -> tuple[str, ...]:
    references = [
        match.group(0)
        for match in _REQUEST_REFERENCE.finditer(message)
        if match.group(0).lower() not in _GENERIC_REQUEST_LABELS and len(match.group(0)) <= 128
    ]
    return tuple(dict.fromkeys(references))


def message_specifies_request(message: str) -> bool:
    """Return whether one unambiguous opaque Request/Trace reference is present."""

    return len(_request_references(message)) == 1


def explicit_request_id(state: Mapping[str, Any]) -> str | None:
    """Resolve one customer-supplied request ID without trusting Assistant history."""

    current = _request_references(str(state.get("redacted_message", "")))
    if current:
        return current[0] if len(current) == 1 else None
    historical = tuple(
        dict.fromkeys(
            reference
            for message in _customer_messages(state)[1:]
            for reference in _request_references(message)
        )
    )
    return historical[0] if len(historical) == 1 else None


def required_api_rate_limit_diagnostic_reads(
    state: Mapping[str, Any],
) -> ApiDiagnosticRequirements:
    """Return the minimum customer-scoped evidence for a concrete 429 diagnosis.

    Generic troubleshooting questions remain knowledge-only. Transactional
    subscription and usage reads are required only when the customer describes
    an observed rate/concurrency failure or asks how that failure relates to
    their current account state. Assistant history never activates the rule.
    """

    classification = state.get("classification", {})
    if not (
        classification.get("policy_boundary", "allowed") == "allowed"
        and classification.get("issue_type") == "api_diagnostics"
        and classification.get("requested_action", "none") == "none"
        and classification.get("needs_realtime_facts") is True
    ):
        return {}
    messages = _customer_messages(state)
    if not any(_RATE_LIMIT_SIGNAL.search(message) for message in messages):
        return {}
    if not any(
        _OBSERVED_FAILURE.search(message) or _CUSTOMER_STATE_CONTEXT.search(message)
        for message in messages
    ):
        return {}
    requirements = dict(_REQUIRED_READS)
    if explicit_request_id(state) is not None:
        requirements["query_request_trace"] = (
            "request_id",
            "model",
            "status_code",
            "error_class",
            "version",
        )
    return requirements


def pending_api_rate_limit_diagnostic_tools(
    state: Mapping[str, Any],
) -> tuple[ReadToolName, ...]:
    pending: list[ReadToolName] = []
    for tool_name, fields in required_api_rate_limit_diagnostic_reads(state).items():
        observation = current_run_tool_observation(state, tool_name)
        if observation is None or any(
            field not in observation["data"] or observation["data"][field] is None
            for field in fields
        ):
            pending.append(tool_name)
    return tuple(pending)


def api_rate_limit_diagnostic_reads_complete(state: Mapping[str, Any]) -> bool:
    requirements = required_api_rate_limit_diagnostic_reads(state)
    return bool(requirements) and not pending_api_rate_limit_diagnostic_tools(state)


def api_rate_limit_control_separation_present(texts: list[str]) -> bool:
    """Return whether the validated answer already explains the two control planes."""

    joined = "\n".join(texts)
    return bool(
        re.search(r"(?:余额|balance)", joined, re.I)
        and re.search(r"(?:并发|concurrency)", joined, re.I)
        and _CONTROL_SEPARATION.search(joined)
    )


def api_rate_limit_knowledge_query(state: Mapping[str, Any]) -> str:
    """Ground retrieval in bounded customer text without Assistant authority."""

    return "\n".join(_customer_messages(state)[-3:])[-2000:]
