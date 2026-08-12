from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from supportguard.agent.schemas import Classification
from supportguard.conversation_text import is_standalone_greeting
from supportguard.services.conversation_action_state import ConversationActionStateV1

_ACTION_QUERY_SCHEMA_VERSION = "conversation-action-state-query.v1"


def contains_exact_resource_reference(message: str, resource_id: str) -> bool:
    """Match one opaque resource reference without substring collisions."""

    if not resource_id:
        return False
    identifier_character = r"A-Za-z0-9_.:-"
    return bool(
        re.search(
            rf"(?<![{identifier_character}]){re.escape(resource_id)}"
            rf"(?![{identifier_character}])",
            message,
            re.I,
        )
    )


def _explicit_action_resource_references(
    message: str,
    projections: list[ConversationActionStateV1],
) -> set[str]:
    """Extract supported opaque resource tokens, including unknown references."""

    namespaces = {
        re.split(r"[_:-]", item.resource_id, maxsplit=1)[0].casefold()
        for item in projections
        if re.search(r"[_:-]", item.resource_id)
    }
    namespaces.update(
        {
            "bill",
            "billing",
            "invoice",
            "key",
            "apikey",
            "subscription",
            "sub",
            "entitlement",
        }
    )
    identifiers = re.findall(
        r"(?<![A-Za-z0-9_.:-])"
        r"([A-Za-z][A-Za-z0-9]*(?:[_:-][A-Za-z0-9]+)+)"
        r"(?![A-Za-z0-9_.:-])",
        message,
    )
    generic_tokens = {
        "api_key",
        "api-key",
        "concurrency_limit_exceeded",
    }
    return {
        identifier
        for identifier in identifiers
        if identifier.casefold() not in generic_tokens
        and re.split(r"[_:-]", identifier, maxsplit=1)[0].casefold() in namespaces
    }


def _action_query_kind(
    normalized: str,
    *,
    resource_reference: bool,
    has_recent_action_referent: bool,
) -> str | None:
    workflow_reference = resource_reference or bool(
        re.search(
            r"(?:申请|审批|操作|处理|执行|退款|退费|撤销|吊销|"
            r"配额|套餐|订阅|api\s*key|转入人工|人工队列|"
            r"这个|这项|那项|它|"
            r"\bapproval\b|\brefund\b|\bwithdraw\b|\brevoke\b|"
            r"\bthis\b|\bthat\b|\bit\b)",
            normalized,
            re.I,
        )
    )
    terminal_word = (
        r"(?:拒绝|没通过|没有通过|未通过|失败|没成功|没有成功|未成功|失效|过期|撤回|"
        r"没有执行|未执行|denied|declined|reject(?:ed)?|fail(?:ed|ure)?|"
        r"stale|invalidated|expired|withdrawn|not executed)"
    )
    reason_word = r"(?:为什么|为何|原因|怎么回事|怎么会|怎么就|why|reason)"
    reason_trigger = bool(
        re.search(
            rf"{reason_word}.{{0,32}}{terminal_word}"
            rf"|{terminal_word}.{{0,32}}{reason_word}",
            normalized,
            re.I,
        )
    )
    bare_rejection_reason = bool(
        re.fullmatch(
            rf"[\s，,。.!！？?]*"
            rf"(?:(?:为什么|为何).{{0,12}}{terminal_word}"
            rf"|why\s+(?:(?:was|did|is)\s+)?(?:it\s+)?{terminal_word})"
            rf".{{0,4}}[\s，,。.!！？?]*",
            normalized,
            re.I,
        )
    )
    if reason_trigger and (workflow_reference or bare_rejection_reason):
        return "reason"
    if workflow_reference and re.search(
        r"(?:进度|处理到哪|执行到哪|还要多久|什么时候完成|"
        r"什么时候到账|正在执行|仍在处理|后来怎么样|后续怎么样)"
        r"|\b(?:progress|how long|still processing|in progress|"
        r"where is it now|what happened next)\b",
        normalized,
        re.I,
    ):
        return "progress"
    if (has_recent_action_referent or workflow_reference) and re.search(
        r"(?:还|仍然?|现在).{0,8}(?:能|可以).{0,8}继续.{0,16}"
        r"(?:查询|咨询|提问|问问题|查看|了解)"
        r"|(?:can|may)\s+i\s+(?:still\s+)?"
        r"(?:ask|query|check|view|continue\s+(?:asking|the\s+conversation))",
        normalized,
        re.I,
    ):
        return "continuity"
    if re.search(
        r"(?:申请|审批|操作|处理|执行|退款|退费|撤销|吊销|"
        r"配额|套餐|变更|这个|这项|那项|它)"
        r".{0,24}(?:状态|进度|结果|怎么样了|如何了|到哪了|"
        r"完成了吗|成功了吗|生效了吗|到账了吗|通过了吗|"
        r"拒绝了|失败了|撤回了|执行了吗|后来呢|现在呢)"
        r"|(?:状态|进度|结果).{0,18}"
        r"(?:申请|审批|操作|处理|执行|退款|撤销|变更)"
        r"|(?:转入人工|人工队列|manual takeover).{0,18}"
        r"(?:什么意思|状态|怎么回事)"
        r"|\b(?:status|progress|result|was it executed|"
        r"did it execute|was it approved|what happened|"
        r"where does it stand|how did it end)\b",
        normalized,
        re.I,
    ):
        return "status"
    return None


def _requested_action_types(normalized: str) -> set[str]:
    matches = {
        "refund": bool(re.search(r"(?:退款|退费|\brefund\b)", normalized, re.I)),
        "api_key_revocation": bool(
            re.search(
                r"(?:api\s*key|密钥|凭证).{0,16}(?:撤销|吊销|禁用|作废)"
                r"|(?:撤销|吊销|禁用|作废).{0,16}(?:api\s*key|密钥|凭证)"
                r"|\b(?:revoke|disable).{0,12}(?:api\s*key|key)\b",
                normalized,
                re.I,
            )
        ),
        "entitlement_change": bool(
            re.search(
                r"(?:配额|并发|套餐|订阅).{0,16}(?:调整|变更|申请|审批|执行)"
                r"|(?:调整|变更).{0,16}(?:配额|并发|套餐|订阅)"
                r"|\b(?:quota|plan|subscription|concurrency).{0,16}"
                r"(?:change|adjust|approval)\b",
                normalized,
                re.I,
            )
        ),
    }
    return {action_type for action_type, matched in matches.items() if matched}


def _hinted_projection_statuses(normalized: str) -> set[str]:
    status_hints: tuple[tuple[str, set[str]], ...] = (
        (r"(?:拒绝|没通过|没有通过|未通过|\b(?:reject|denied|declined))", {"rejected"}),
        (r"(?:撤回|\bwithdraw)", {"withdrawn"}),
        (r"(?:失效|过期|\bstale)", {"stale"}),
        (r"(?:核验|无法确认|verification)", {"verification_pending"}),
        (r"(?:失败|\bfailed?\b)", {"failed"}),
        (r"(?:已执行|执行完成|已完成|已生效|已到账|\bexecuted\b|\bcompleted\b)", {"executed"}),
        (r"(?:正在执行|\bexecuting\b)", {"executing"}),
        (r"(?:待审批|等待审批|\bpending\b)", {"pending"}),
    )
    return next(
        (statuses for pattern, statuses in status_hints if re.search(pattern, normalized, re.I)),
        set(),
    )


def _unresolved_action_query(
    *,
    query_kind: str,
    reason_code: str,
    requested_action_types: set[str],
    requested_resource_references: set[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": _ACTION_QUERY_SCHEMA_VERSION,
        "resolution": "unresolved",
        "query_kind": query_kind,
        "reason_code": reason_code,
        "requested_action_types": sorted(requested_action_types),
        "grants_action_authority": False,
    }
    if requested_resource_references is not None:
        payload["requested_resource_references"] = sorted(requested_resource_references)
    return payload


def _initial_action_candidates(
    *,
    resource_matches: list[ConversationActionStateV1],
    explicit_resource_references: set[str],
    requested_action_types: set[str],
    recent_action_approval_id: str | None,
    projections: list[ConversationActionStateV1],
    query_kind: str,
) -> tuple[list[ConversationActionStateV1] | None, dict[str, Any] | None]:
    if resource_matches:
        candidates = (
            [item for item in resource_matches if item.action_type in requested_action_types]
            if requested_action_types
            else resource_matches
        )
        if not candidates:
            return None, _unresolved_action_query(
                query_kind=query_kind,
                reason_code="resource_action_type_mismatch",
                requested_resource_references=explicit_resource_references,
                requested_action_types=requested_action_types,
            )
        return candidates, None
    if explicit_resource_references:
        return None, _unresolved_action_query(
            query_kind=query_kind,
            reason_code="resource_not_in_current_action_state",
            requested_resource_references=explicit_resource_references,
            requested_action_types=requested_action_types,
        )
    if not recent_action_approval_id:
        return None, _unresolved_action_query(
            query_kind=query_kind,
            reason_code="action_referent_missing",
            requested_action_types=requested_action_types,
        )
    referent_matches = [
        item for item in projections if item.approval_id == recent_action_approval_id
    ]
    if not referent_matches:
        return None, _unresolved_action_query(
            query_kind=query_kind,
            reason_code="action_referent_not_in_current_state",
            requested_action_types=requested_action_types,
        )
    if requested_action_types and all(
        item.action_type not in requested_action_types for item in referent_matches
    ):
        return None, _unresolved_action_query(
            query_kind=query_kind,
            reason_code="action_referent_action_type_mismatch",
            requested_action_types=requested_action_types,
        )
    return [
        item
        for item in referent_matches
        if not requested_action_types or item.action_type in requested_action_types
    ], None


def _ambiguous_action_options(
    candidates: list[ConversationActionStateV1],
) -> list[dict[str, Any]]:
    """Project stable choices without exposing credential references."""

    options: list[dict[str, Any]] = []
    for item in sorted(
        candidates,
        key=lambda candidate: (
            candidate.action_type,
            candidate.resource_type,
            candidate.resource_id,
            candidate.approval_id,
        ),
    )[:5]:
        option: dict[str, Any] = {
            "action_type": item.action_type,
            "resource_type": item.resource_type,
            "projection_status": item.projection_status,
        }
        if item.action_type == "api_key_revocation":
            option["resource_reference_hidden"] = True
        else:
            option["resource_id"] = item.resource_id
        options.append(option)
    return options


def resolve_action_state_query(
    message: str,
    current_actions: list[dict[str, Any]],
    *,
    recent_action_approval_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a status/reason inquiry without granting action authority."""

    normalized = " ".join(message.strip().split())
    if not normalized or not current_actions:
        return None
    projections = [ConversationActionStateV1.model_validate(item) for item in current_actions]
    resource_matches = [
        item
        for item in projections
        if contains_exact_resource_reference(normalized, item.resource_id)
    ]
    explicit_references = _explicit_action_resource_references(normalized, projections)
    query_kind = _action_query_kind(
        normalized,
        resource_reference=bool(resource_matches or explicit_references),
        has_recent_action_referent=recent_action_approval_id is not None,
    )
    if query_kind is None:
        return None
    requested_types = _requested_action_types(normalized)
    candidates, unresolved = _initial_action_candidates(
        resource_matches=resource_matches,
        explicit_resource_references=explicit_references,
        requested_action_types=requested_types,
        recent_action_approval_id=recent_action_approval_id,
        projections=projections,
        query_kind=query_kind,
    )
    if unresolved is not None:
        return unresolved
    if candidates is None:
        return _unresolved_action_query(
            query_kind=query_kind,
            reason_code="action_candidate_resolution_failed",
            requested_action_types=requested_types,
        )
    hinted_statuses = _hinted_projection_statuses(normalized)
    if len(candidates) > 1 and hinted_statuses:
        status_candidates = [
            item for item in candidates if item.projection_status in hinted_statuses
        ]
        if status_candidates:
            candidates = status_candidates
    if len(candidates) != 1:
        return {
            "schema_version": _ACTION_QUERY_SCHEMA_VERSION,
            "resolution": "ambiguous",
            "query_kind": query_kind,
            "candidate_options": _ambiguous_action_options(candidates),
            "grants_action_authority": False,
        }
    return {
        "schema_version": _ACTION_QUERY_SCHEMA_VERSION,
        "resolution": "selected",
        "approval_id": candidates[0].approval_id,
        "query_kind": query_kind,
        "grants_action_authority": False,
    }


def is_knowledge_only_api_question(classification: Mapping[str, Any]) -> bool:
    """Return whether API diagnostics need documentation but no account snapshot."""

    return (
        classification.get("issue_type") == "api_diagnostics"
        and classification.get("requested_action", "none") == "none"
        and classification.get("needs_realtime_facts") is False
    )


def canonicalize_non_material_classification(
    message: str,
    classification: Classification,
) -> Classification:
    """Narrow one exact social opener to the authority-free greeting contract."""

    if (
        classification.policy_boundary != "allowed"
        or classification.requested_action != "none"
        or classification.needs_realtime_facts
        or not is_standalone_greeting(message)
    ):
        return classification
    return Classification(
        issue_type="unknown",
        risk="low",
        policy_boundary="allowed",
        requested_action="none",
        requested_concurrency_limit=None,
        needs_realtime_facts=False,
        support_subject="supportguard_greeting",
        rationale=(
            "Provider semantic intake completed; deterministic conversation policy "
            "recognized a standalone social greeting with no factual support claim."
        ),
    )
