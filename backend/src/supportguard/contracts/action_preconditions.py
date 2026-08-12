from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionType = Literal["refund", "api_key_revocation", "entitlement_change"]
PlannedAction = Literal["none", "refund", "api_key_revocation", "entitlement_change"]
AdmissionStatus = Literal["none", "missing", "admitted", "mismatch"]


class QuotaChangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rpm_limit: int | None = Field(default=None, strict=True, ge=1, le=10_000_000)
    concurrency_limit: int | None = Field(default=None, strict=True, ge=1, le=100_000)

    @model_validator(mode="after")
    def require_exactly_one_target(self) -> QuotaChangeTarget:
        supplied = [
            value for value in (self.rpm_limit, self.concurrency_limit) if value is not None
        ]
        if len(supplied) != 1:
            raise ValueError("quota_change requires exactly one non-null target")
        return self


class PlanChangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=64)


def validate_entitlement_target(
    change_type: Literal["quota_change", "plan_change"],
    target: Any,
) -> dict[str, Any]:
    schema: type[BaseModel] = (
        QuotaChangeTarget if change_type == "quota_change" else PlanChangeTarget
    )
    return schema.model_validate(target).model_dump(mode="json", exclude_none=True)


class ActionAdmission(BaseModel):
    """Deterministic minimum fields required before any high-risk action workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-admission.v1"] = "action-admission.v1"
    action_type: ActionType
    issue_type: Literal["billing_refund", "credential_security", "entitlement_change"]
    missing_fields: tuple[str, ...]
    extracted_arguments: dict[str, Any] = Field(default_factory=dict)
    clarification_question: str


class AdmissionFieldSource(BaseModel):
    """Auditable source for one deterministic admission field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_name: str
    message_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)


class ActionAdmissionV2(BaseModel):
    """Deterministic action admission; it does not grant proposal authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["action-admission.v2"] = "action-admission.v2"
    status: AdmissionStatus
    planned_action: PlannedAction
    action_type: ActionType | None = None
    issue_type: str | None = None
    missing_fields: tuple[str, ...] = ()
    extracted_arguments: dict[str, Any] = Field(default_factory=dict)
    field_sources: tuple[AdmissionFieldSource, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    request_reason: str | None = None
    tenant_id: str
    customer_id: str
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification_version: str
    current_message_id: str
    turn_group_id: str
    reason_code: str
    clarification_question: str | None = None

    @model_validator(mode="after")
    def validate_status_shape(self) -> ActionAdmissionV2:
        if self.status == "none":
            if self.action_type is not None or self.missing_fields:
                raise ValueError("none admission forbids action fields")
        elif self.status == "missing":
            if self.action_type is None or not self.missing_fields:
                raise ValueError("missing admission requires action and missing fields")
        elif self.status == "admitted":
            if self.action_type is None or self.missing_fields:
                raise ValueError("admitted admission requires complete action fields")
        elif self.clarification_question is None:
            raise ValueError("mismatch admission requires a safe clarification")
        return self


_BILLING_ID = re.compile(
    r"\bbill[-_][a-z0-9](?:[a-z0-9._:-]{0,61}[a-z0-9])?(?![A-Za-z0-9_-])",
    re.I,
)
_API_KEY_REF = re.compile(r"\bkey[-_][a-z0-9][a-z0-9._:-]{1,126}\b", re.I)
_REFUND_ACTION = re.compile(
    r"(?:帮我|请|我要|我想|申请|发起|给我|处理).{0,12}(?:退款|退费)"
    r"|(?:退款|退费)(?:吧|。|！|!|$)"
    r"|(?:please\s+)?(?:refund|reimburse)"
    r"(?:\s+me|\s+this|\s+the(?:\s+[a-z0-9_-]+)?|\s+bill[-_][a-z0-9._:-]+|[.!]?$)",
    re.I,
)
_ACTION_REQUEST_CUE = re.compile(
    r"(?:帮我|麻烦|我要|我想|我需要|我希望|"
    r"(?:^|[\s，,。；;！!])请(?!问)|立即|继续(?:处理)?|"
    r"按.{0,12}(?:政策|流程|规则)|please|\bi\s+(?:want|need)\b)",
    re.I,
)
_KEY_DOMAIN = re.compile(
    r"(?:api\s*key|key\s*reference|密钥|凭证|\bkey[-_][a-z0-9][a-z0-9._:-]{1,126}\b)",
    re.I,
)
_KEY_REVOCATION = re.compile(
    r"(?:撤销|禁用|作废|吊销|revoke|disable|invalidate)",
    re.I,
)
_KEY_REVOCATION_NEGATED = re.compile(
    r"(?:不要|无需|不需要|不想|不希望|不必|禁止|别|先别|先不|暂不|拒绝).{0,16}"
    r"(?:撤销|禁用|作废|吊销|revoke|disable|invalidate)"
    r"|(?:do\s+not|don['’]?t|no\s+need\s+to|must\s+not|should\s+not|never|"
    r"refuse\s+to).{0,24}(?:revoke|disable|invalidate)",
    re.I,
)
_ENTITLEMENT_DOMAIN = re.compile(
    r"(?:并发|配额|额度|上限|套餐|订阅|concurrency|quota|limit|plan|subscription)",
    re.I,
)
_ENTITLEMENT_CHANGE = re.compile(
    r"(?:提高|提升|调整|修改|改为|改成|设置|增加|扩容|raise|increase|change|set|"
    r"adjust|upgrade)",
    re.I,
)
_ENTITLEMENT_CHANGE_NEGATED = re.compile(
    r"(?:不要|无需|不需要|不想|不希望|不必|禁止|别|先别|先不|暂不|拒绝).{0,16}"
    r"(?:提高|提升|调整|修改|改为|改成|设置|增加|扩容|raise|increase|change|set|"
    r"adjust|upgrade)"
    r"|(?:do\s+not|don['’]?t|no\s+need\s+to|must\s+not|should\s+not|never|"
    r"refuse\s+to).{0,24}(?:raise|increase|change|set|adjust|upgrade)",
    re.I,
)
_CONCURRENCY_TARGET = re.compile(
    r"(?:(?:调整到|提升到|提高到|增加到|改为|改成|设置为|上限为|"
    r"target(?:\s+is)?|\bto)"
    r"|(?:目标\s*)?(?:并发|concurrency)(?:\s*(?:上限|limit))?\s*(?:是|为))"
    r"\s*[:：]?\s*(\d{1,6})\b",
    re.I,
)
_RPM_TARGET = re.compile(
    r"(?:rpm|每分钟请求(?:数|上限)?).{0,12}"
    r"(?:调整到|提升到|提高到|增加到|改为|改成|设置为|上限为|to)\s*[:：]?\s*(\d{1,7})\b"
    r"|(?:调整到|提升到|提高到|增加到|改为|改成|设置为)\s*[:：]?\s*(\d{1,7})"
    r"\s*(?:rpm|每分钟请求)",
    re.I,
)
_PLAN_TARGET = re.compile(
    r"(?:套餐|plan).{0,12}(?:改为|改成|升级到|切换到|change\s+to|upgrade\s+to)"
    r"\s*[:：]?\s*([a-z][a-z0-9_-]{1,31})"
    r"|(?:改为|改成|升级到|切换到|change\s+to|upgrade\s+to)"
    r"\s*[:：]?\s*([a-z][a-z0-9_-]{1,31})\s*(?:套餐|plan)",
    re.I,
)
_DUPLICATE_CHARGE_ACTION = re.compile(
    r"(?:重复扣费|重复收费|duplicate\s+charge).{0,24}"
    r"(?:按.{0,6}政策处理|处理|退款|refund)",
    re.I,
)
_ACTION_CLAUSE_BOUNDARY = re.compile(
    r"[，,。；;！!？?\n]+|(?:但是|不过|然而|而是|随后|然后)|\b(?:but|however|instead|then)\b",
    re.I,
)
_ACTION_COORDINATED_CONTINUATION = re.compile(
    r"^(?:同时|并且|以及|还要|也要)|^(?:and|also)\b",
    re.I,
)
_ACTION_INFORMATIONAL = re.compile(
    r"(?:我想|想|希望|需要)?(?:知道|了解|询问|确认).{0,20}"
    r"(?:会有?什么|有何|是否会|会不会|可能有).{0,12}(?:影响|后果|风险)"
    r"|(?:撤销|禁用|作废|吊销|提高|提升|调整|修改|改为|改成|设置|增加|扩容)"
    r".{0,24}(?:会有?什么|有何|是否会|会不会|可能有).{0,12}(?:影响|后果|风险)"
    r"|(?:what|which).{0,16}(?:impact|effect|risk|consequence)"
    r"|(?:what\s+(?:happens|would\s+happen)|how\s+would).{0,40}"
    r"(?:revoke|disable|invalidate|raise|increase|change|set|adjust|upgrade)"
    r"|(?:revoke|disable|invalidate|raise|increase|change|set|adjust|upgrade)"
    r".{0,32}(?:what\s+(?:impact|effect)|how\s+would|would\s+.*(?:affect|impact))",
    re.I,
)
_REFUND_ACTION_NEGATED = re.compile(
    r"(?:不要|不用|无需|不需要|不想|不希望|不必|禁止|别|暂不|先不|拒绝)"
    r".{0,24}(?:退款|退费)"
    r"|(?:do\s+not|don['’]?t|no\s+need\s+to|must\s+not|should\s+not)"
    r".{0,32}(?:refund|reimburse)"
    r"|\bno\s+(?:refund|reimbursement)\b",
    re.I,
)
_REFUND_INFORMATIONAL = re.compile(
    r"(?:只|仅)(?:想|需要|需)?(?:说明|解释|介绍|了解|查询|查看|咨询|知道)"
    r"|(?:请问|想了解|想知道|说明|解释|介绍|查询|查看|咨询)"
    r".{0,28}(?:退款|退费|重复扣费|重复收费)"
    r"|(?:如何|怎么|怎样|为什么|什么是).{0,24}(?:退款|退费|重复扣费|重复收费)"
    r"|(?:退款|退费|重复扣费|重复收费).{0,32}"
    r"(?:是什么|有哪些|如何|怎么|怎样|为什么|需要什么|什么条件|什么流程|吗|呢)"
    r"|(?:only|just)\s+(?:explain|describe|show|tell|check|review|understand)"
    r"|(?:explain|describe|what|why|how|whether|can\s+i|could\s+i)"
    r".{0,48}(?:refund|reimburse|duplicate\s+charge)"
    r"|(?:refund|reimburse|duplicate\s+charge).{0,48}"
    r"(?:policy|process|rules?|criteria|eligibility|steps?|status).{0,24}"
    r"(?:what|why|how|which|can|could|\?)",
    re.I,
)

_ACTION_ISSUE_TYPE: dict[ActionType, str] = {
    "refund": "billing_refund",
    "api_key_revocation": "credential_security",
    "entitlement_change": "entitlement_change",
}

_MISSING_QUESTION: dict[ActionType, str] = {
    "refund": "请提供需要退款的账单 ID（Billing ID / 账单编号，例如 bill_...）。",
    "api_key_revocation": (
        "请提供要撤销的 API Key 引用（Key Reference，例如 key_...）；不要发送完整密钥。"
    ),
    "entitlement_change": ("请提供具体并发上限目标值、RPM 上限或目标套餐；系统不会猜测目标值。"),
}


class _AcceptedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class _MismatchContext(TypedDict):
    planned_action: PlannedAction
    issue_type: str | None
    messages: list[_AcceptedMessage]
    tenant_id: str
    customer_id: str
    classification_version: str
    current_message_id: str
    turn_group_id: str


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _scope_hash(*, tenant_id: str, customer_id: str) -> str:
    payload = json.dumps(
        {"customer_id": customer_id, "tenant_id": tenant_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _accepted_messages(
    *,
    current_turn: str,
    current_message_id: str,
    recent_conversation: Sequence[dict[str, Any]],
) -> list[_AcceptedMessage]:
    accepted: list[_AcceptedMessage] = []
    for index, item in enumerate(recent_conversation):
        if item.get("role") not in {"customer", "user"}:
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        message_id = str(item.get("message_id") or item.get("id") or f"accepted-message-{index}")
        if message_id == current_message_id:
            continue
        accepted.append(
            _AcceptedMessage(
                message_id=message_id,
                content=content,
                content_hash=_content_hash(content),
            )
        )
    current = current_turn.strip()
    accepted.append(
        _AcceptedMessage(
            message_id=current_message_id,
            content=current,
            content_hash=_content_hash(current),
        )
    )
    return accepted[-6:]


def _action_intents(
    messages: Sequence[_AcceptedMessage],
    *,
    continuation_action: ActionType | None = None,
    continuation_context: str = "",
) -> set[ActionType]:
    actions: set[ActionType] = set()
    for item in messages:
        if _refund_action_requested(item.content):
            actions.add("refund")
        if _key_action_requested(item.content):
            actions.add("api_key_revocation")
        if _entitlement_action_requested(item.content):
            actions.add("entitlement_change")
    if not messages or continuation_action is None:
        return actions
    current = messages[-1].content
    if (
        continuation_action == "api_key_revocation"
        and _KEY_DOMAIN.search(continuation_context)
        and _key_action_requested(current, domain_context=True)
    ):
        actions.add("api_key_revocation")
    if (
        continuation_action == "entitlement_change"
        and _ENTITLEMENT_DOMAIN.search(continuation_context)
        and _entitlement_action_requested(current, domain_context=True)
    ):
        actions.add("entitlement_change")
    return actions


def _refund_action_requested(content: str) -> bool:
    """Recognize refund authorization without treating policy questions as consent.

    A direct, non-negated refund request remains sufficient. Duplicate-charge
    shorthand additionally requires an independent request cue; merely asking
    how such a charge is handled is read-only. Clause-level evaluation lets a
    later explicit request win when the customer negates a different behavior,
    for example ``不要解释了，请直接退款``.
    """

    normalized = " ".join(content.split())
    if not normalized:
        return False
    disqualified_shorthand = False
    for raw_clause in _ACTION_CLAUSE_BOUNDARY.split(normalized):
        clause = raw_clause.strip()
        if not clause:
            continue
        if _REFUND_ACTION_NEGATED.search(clause):
            disqualified_shorthand = True
            continue
        if _REFUND_INFORMATIONAL.search(clause):
            disqualified_shorthand = True
            continue
        if _REFUND_ACTION.search(clause):
            return True
        if _DUPLICATE_CHARGE_ACTION.search(clause) and _ACTION_REQUEST_CUE.search(clause):
            return True
    if disqualified_shorthand:
        return False
    return bool(
        _DUPLICATE_CHARGE_ACTION.search(normalized) and _ACTION_REQUEST_CUE.search(normalized)
    )


def _refund_continuation_blocked(content: str) -> bool:
    """Return whether this turn forbids inheriting an earlier refund request."""

    normalized = " ".join(content.split())
    return bool(
        normalized
        and any(
            _REFUND_ACTION_NEGATED.search(clause) or _REFUND_INFORMATIONAL.search(clause)
            for clause in _ACTION_CLAUSE_BOUNDARY.split(normalized)
            if clause.strip()
        )
    )


def explicit_current_turn_action(current_turn: str) -> ActionType | None:
    """Return one unambiguous high-risk action explicitly requested now.

    This is the same deterministic parser used by ActionAdmissionV2, exposed
    for read-only convergence of an already active or non-repeatable action.
    It does not validate fields, select a resource, or grant action authority.
    """

    content = current_turn.strip()
    if not content:
        return None
    intents = _action_intents(
        (
            _AcceptedMessage(
                message_id="current-turn",
                content=content,
                content_hash=_content_hash(content),
            ),
        )
    )
    return next(iter(intents)) if len(intents) == 1 else None


def _explicit_action_requested(
    content: str,
    *,
    domain: re.Pattern[str],
    action: re.Pattern[str],
    negated_action: re.Pattern[str],
    domain_context: bool = False,
) -> bool:
    """Recognize customer authorization without depending on token order.

    The deterministic gate requires a domain, an action verb, and explicit
    request language. Passive state questions and explicit negation therefore
    remain non-authorizing even when they mention the same domain and verb.
    """

    return bool(
        _positive_action_clause_spans(
            content,
            domain=domain,
            action=action,
            negated_action=negated_action,
            domain_context=domain_context,
        )
    )


def _clause_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in _ACTION_CLAUSE_BOUNDARY.finditer(content):
        if start < boundary.start():
            spans.append((start, boundary.start()))
        start = boundary.end()
    if start < len(content):
        spans.append((start, len(content)))
    return spans


def _positive_action_clause_spans(
    content: str,
    *,
    domain: re.Pattern[str],
    action: re.Pattern[str],
    negated_action: re.Pattern[str],
    domain_context: bool = False,
) -> list[tuple[int, int]]:
    """Return only clauses that independently authorize the requested action.

    A correction clause may inherit its domain from another clause in the same
    accepted message (``不要提高到 80，请改成 40``), but it must contain its
    own positive action verb and its own imperative request cue. This preserves
    explicit corrections and sequenced requests while preventing a request cue
    in one clause from authorizing a later question. Negated and informational
    clauses never contribute resource references or targets.
    """

    normalized = " ".join(content.split())
    if not normalized or (not domain_context and domain.search(normalized) is None):
        return []
    positive: list[tuple[int, int]] = []
    prior_request_cue = False
    for start, end in _clause_spans(content):
        clause = content[start:end].strip()
        own_request_cue = bool(clause and _ACTION_REQUEST_CUE.search(clause))
        coordinated_request = bool(
            clause and prior_request_cue and _ACTION_COORDINATED_CONTINUATION.search(clause)
        )
        if (
            clause
            and negated_action.search(clause) is None
            and _ACTION_INFORMATIONAL.search(clause) is None
            and action.search(clause) is not None
            and (own_request_cue or coordinated_request)
        ):
            positive.append((start, end))
        prior_request_cue = prior_request_cue or own_request_cue
    return positive


def _key_action_requested(content: str, *, domain_context: bool = False) -> bool:
    return _explicit_action_requested(
        content,
        domain=_KEY_DOMAIN,
        action=_KEY_REVOCATION,
        negated_action=_KEY_REVOCATION_NEGATED,
        domain_context=domain_context,
    )


def _entitlement_action_requested(content: str, *, domain_context: bool = False) -> bool:
    return _explicit_action_requested(
        content,
        domain=_ENTITLEMENT_DOMAIN,
        action=_ENTITLEMENT_CHANGE,
        negated_action=_ENTITLEMENT_CHANGE_NEGATED,
        domain_context=domain_context,
    )


def _action_field_spans(
    messages: Sequence[_AcceptedMessage],
    *,
    domain: re.Pattern[str],
    action: re.Pattern[str],
    negated_action: re.Pattern[str],
    domain_context: bool = False,
) -> dict[str, tuple[tuple[int, int], ...]]:
    """Bind extracted fields to positive clauses or neutral continuations."""

    allowed: dict[str, tuple[tuple[int, int], ...]] = {}
    for item in messages:
        positive = _positive_action_clause_spans(
            item.content,
            domain=domain,
            action=action,
            negated_action=negated_action,
            domain_context=domain_context,
        )
        if positive:
            allowed[item.message_id] = tuple(
                (start, end)
                for start, end in _clause_spans(item.content)
                if negated_action.search(item.content[start:end]) is None
                and _ACTION_INFORMATIONAL.search(item.content[start:end]) is None
            )
        elif negated_action.search(item.content) or _ACTION_INFORMATIONAL.search(item.content):
            allowed[item.message_id] = ()
        else:
            allowed[item.message_id] = ((0, len(item.content)),)
    return allowed


def _matches(
    pattern: re.Pattern[str],
    messages: Sequence[_AcceptedMessage],
    *,
    groups: tuple[int, ...] = (0,),
    allowed_spans: dict[str, tuple[tuple[int, int], ...]] | None = None,
) -> list[tuple[str, AdmissionFieldSource]]:
    matches: list[tuple[str, AdmissionFieldSource]] = []
    for item in messages:
        spans = (
            allowed_spans.get(item.message_id, ())
            if allowed_spans is not None
            else ((0, len(item.content)),)
        )
        for span_start, span_end in spans:
            for match in pattern.finditer(item.content, span_start, span_end):
                group = next(
                    (index for index in groups if match.group(index) is not None),
                    groups[0],
                )
                value = match.group(group)
                start, end = match.span(group)
                matches.append(
                    (
                        value,
                        AdmissionFieldSource(
                            field_name="",
                            message_id=item.message_id,
                            content_hash=item.content_hash,
                            span_start=start,
                            span_end=end,
                        ),
                    )
                )
    return matches


def _unique_matches(
    matches: Sequence[tuple[str, AdmissionFieldSource]],
    *,
    field_name: str,
    normalize: bool = True,
) -> list[tuple[str, AdmissionFieldSource]]:
    unique: dict[str, tuple[str, AdmissionFieldSource]] = {}
    for value, source in matches:
        canonical = value.casefold() if normalize else value
        unique.setdefault(
            canonical,
            (
                value,
                source.model_copy(update={"field_name": field_name}),
            ),
        )
    return list(unique.values())


def _mismatch(
    *,
    planned_action: PlannedAction,
    action_type: ActionType | None,
    issue_type: str | None,
    messages: Sequence[_AcceptedMessage],
    tenant_id: str,
    customer_id: str,
    classification_version: str,
    current_message_id: str,
    turn_group_id: str,
    reason_code: str,
    question: str,
) -> ActionAdmissionV2:
    return ActionAdmissionV2(
        status="mismatch",
        planned_action=planned_action,
        action_type=action_type,
        issue_type=issue_type,
        source_message_ids=tuple(item.message_id for item in messages),
        request_reason=messages[-1].content or None,
        tenant_id=tenant_id,
        customer_id=customer_id,
        scope_hash=_scope_hash(tenant_id=tenant_id, customer_id=customer_id),
        classification_version=classification_version,
        current_message_id=current_message_id,
        turn_group_id=turn_group_id,
        reason_code=reason_code,
        clarification_question=question,
    )


def resolve_action_admission_v2(
    current_turn: str,
    recent_conversation: Sequence[dict[str, Any]],
    *,
    requested_action: PlannedAction,
    issue_type: str | None,
    tenant_id: str,
    customer_id: str,
    current_message_id: str,
    turn_group_id: str,
    classification_version: str = "classification.v1",
    requested_concurrency_limit: int | None = None,
    continuation_action: ActionType | None = None,
) -> ActionAdmissionV2:
    """Cross-check an untrusted typed plan against accepted customer messages.

    Only redacted customer text contributes resource references, targets, and
    request reasons. Runtime authentication contributes scope.
    """

    accepted_messages = _accepted_messages(
        current_turn=current_turn,
        current_message_id=current_message_id,
        recent_conversation=recent_conversation,
    )
    current_message = accepted_messages[-1]
    # Only the immediately preceding accepted customer turn may provide the
    # domain for an explicit correction. Older mentions cannot silently revive
    # a stale action context.
    continuation_context = accepted_messages[-2].content if len(accepted_messages) > 1 else ""
    trusted_continuation = continuation_action if continuation_action == requested_action else None
    current_intents = _action_intents(
        (current_message,),
        continuation_action=trusted_continuation,
        continuation_context=continuation_context,
    )
    current_blocks_continuation = requested_action == "refund" and _refund_continuation_blocked(
        current_message.content
    )
    if current_intents or continuation_action != requested_action or current_blocks_continuation:
        messages = [current_message]
    else:
        messages = accepted_messages
    intents = _action_intents(
        messages,
        continuation_action=trusted_continuation,
        continuation_context=continuation_context,
    )
    common: _MismatchContext = {
        "planned_action": requested_action,
        "issue_type": issue_type,
        "messages": messages,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "classification_version": classification_version,
        "current_message_id": current_message_id,
        "turn_group_id": turn_group_id,
    }
    if len(intents) > 1:
        return _mismatch(
            action_type=None,
            reason_code="action_intent_ambiguous",
            question="当前消息同时包含多种高风险操作，请一次只确认一种操作。",
            **common,
        )
    parsed_action = next(iter(intents), None)
    if requested_action == "none":
        if parsed_action is None:
            return ActionAdmissionV2(
                status="none",
                planned_action="none",
                issue_type=issue_type,
                source_message_ids=tuple(item.message_id for item in messages),
                request_reason=messages[-1].content or None,
                tenant_id=tenant_id,
                customer_id=customer_id,
                scope_hash=_scope_hash(tenant_id=tenant_id, customer_id=customer_id),
                classification_version=classification_version,
                current_message_id=current_message_id,
                turn_group_id=turn_group_id,
                reason_code="no_high_risk_action",
            )
        return _mismatch(
            action_type=parsed_action,
            reason_code="plan_omits_explicit_action",
            question="我识别到高风险操作意图，但当前计划不一致；请明确要执行的操作。",
            **common,
        )
    if parsed_action is None:
        return _mismatch(
            action_type=requested_action,
            reason_code="plan_without_customer_action",
            question="当前计划包含高风险操作，但客户消息未明确授权该操作；请明确确认。",
            **common,
        )
    if parsed_action != requested_action:
        return _mismatch(
            action_type=parsed_action,
            reason_code="planned_action_mismatch",
            question="客户请求与当前操作计划不一致，请重新确认要执行的操作。",
            **common,
        )
    if issue_type != _ACTION_ISSUE_TYPE[parsed_action]:
        return _mismatch(
            action_type=parsed_action,
            reason_code="issue_type_mismatch",
            question="请求类型与操作类型不一致，请补充说明要处理的问题。",
            **common,
        )

    arguments: dict[str, Any] = {}
    sources: list[AdmissionFieldSource] = []
    missing_fields: tuple[str, ...] = ()
    if parsed_action == "refund":
        refs = _unique_matches(
            _matches(_BILLING_ID, messages),
            field_name="billing_record_id",
        )
        if len(refs) > 1:
            return _mismatch(
                action_type=parsed_action,
                reason_code="resource_ref_ambiguous",
                question="检测到多个账单 ID，请明确本次只处理哪一个账单。",
                **common,
            )
        if not refs:
            missing_fields = ("billing_record_id",)
        else:
            arguments["billing_record_id"] = refs[0][0]
            sources.append(refs[0][1])
    elif parsed_action == "api_key_revocation":
        domain_context = bool(
            trusted_continuation == parsed_action and _KEY_DOMAIN.search(continuation_context)
        )
        field_spans = _action_field_spans(
            messages,
            domain=_KEY_DOMAIN,
            action=_KEY_REVOCATION,
            negated_action=_KEY_REVOCATION_NEGATED,
            domain_context=domain_context,
        )
        refs = _unique_matches(
            _matches(_API_KEY_REF, messages, allowed_spans=field_spans),
            field_name="api_key_ref",
        )
        if len(refs) > 1:
            return _mismatch(
                action_type=parsed_action,
                reason_code="resource_ref_ambiguous",
                question="检测到多个 API Key 引用，请明确本次只撤销哪一个引用。",
                **common,
            )
        if not refs:
            missing_fields = ("api_key_ref",)
        else:
            arguments["api_key_ref"] = refs[0][0]
            sources.append(refs[0][1])
    else:
        domain_context = bool(
            trusted_continuation == parsed_action
            and _ENTITLEMENT_DOMAIN.search(continuation_context)
        )
        field_spans = _action_field_spans(
            messages,
            domain=_ENTITLEMENT_DOMAIN,
            action=_ENTITLEMENT_CHANGE,
            negated_action=_ENTITLEMENT_CHANGE_NEGATED,
            domain_context=domain_context,
        )
        targets: list[tuple[dict[str, Any], AdmissionFieldSource]] = []
        concurrency = _unique_matches(
            _matches(
                _CONCURRENCY_TARGET,
                messages,
                groups=(1,),
                allowed_spans=field_spans,
            ),
            field_name="target.concurrency_limit",
            normalize=False,
        )
        for value, source in concurrency:
            targets.append(({"concurrency_limit": int(value)}, source))
        rpm = _unique_matches(
            _matches(
                _RPM_TARGET,
                messages,
                groups=(1, 2),
                allowed_spans=field_spans,
            ),
            field_name="target.rpm_limit",
            normalize=False,
        )
        for value, source in rpm:
            targets.append(({"rpm_limit": int(value)}, source))
        plans = _unique_matches(
            _matches(
                _PLAN_TARGET,
                messages,
                groups=(1, 2),
                allowed_spans=field_spans,
            ),
            field_name="target.plan",
        )
        for value, source in plans:
            targets.append(({"plan": value}, source))
        unique_targets: dict[str, tuple[dict[str, Any], AdmissionFieldSource]] = {}
        for target, source in targets:
            identity = json.dumps(target, sort_keys=True, separators=(",", ":"))
            unique_targets.setdefault(identity, (target, source))
        if len(unique_targets) > 1:
            return _mismatch(
                action_type=parsed_action,
                reason_code="target_ambiguous",
                question="检测到多个不同目标值，请明确本次只采用一个配额或套餐目标。",
                **common,
            )
        if not unique_targets:
            missing_fields = ("target",)
        else:
            target, source = next(iter(unique_targets.values()))
            if (
                requested_concurrency_limit is not None
                and target.get("concurrency_limit") != requested_concurrency_limit
            ):
                return _mismatch(
                    action_type=parsed_action,
                    reason_code="planned_target_mismatch",
                    question="客户给出的并发目标与当前计划不一致，请重新确认目标值。",
                    **common,
                )
            if "plan" in target:
                arguments.update({"change_type": "plan_change", "target": target})
            else:
                arguments.update({"change_type": "quota_change", "target": target})
            sources.append(source)

    status: Literal["missing", "admitted"] = "missing" if missing_fields else "admitted"
    return ActionAdmissionV2(
        status=status,
        planned_action=requested_action,
        action_type=parsed_action,
        issue_type=issue_type,
        missing_fields=missing_fields,
        extracted_arguments=arguments,
        field_sources=tuple(sources),
        source_message_ids=tuple(item.message_id for item in messages),
        request_reason=messages[-1].content or None,
        tenant_id=tenant_id,
        customer_id=customer_id,
        scope_hash=_scope_hash(tenant_id=tenant_id, customer_id=customer_id),
        classification_version=classification_version,
        current_message_id=current_message_id,
        turn_group_id=turn_group_id,
        reason_code=(
            "required_action_fields_missing" if status == "missing" else "action_request_admitted"
        ),
        clarification_question=(_MISSING_QUESTION[parsed_action] if status == "missing" else None),
    )


def _customer_history(recent_conversation: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(item.get("content", ""))
        for item in recent_conversation[-6:]
        if item.get("role") in {"customer", "user"} and item.get("content")
    )


def resolve_missing_action_preconditions(
    current_turn: str,
    recent_conversation: list[dict[str, Any]],
) -> ActionAdmission | None:
    """Return only explicit high-risk requests that still lack a typed required field.

    The patterns recognize generic action language and opaque resource shapes. They do
    not contain scenario IDs, product names, fixture values, or expected answer text.
    """

    current = current_turn.strip()
    history = _customer_history(recent_conversation)
    continuation_surface = f"{history}\n{current}".strip()

    refund_intent = _refund_action_requested(current) or (
        not _refund_continuation_blocked(current)
        and _refund_action_requested(history)
        and bool(_BILLING_ID.search(current))
    )
    if refund_intent:
        billing = _BILLING_ID.search(continuation_surface)
        if billing is None:
            return ActionAdmission(
                action_type="refund",
                issue_type="billing_refund",
                missing_fields=("billing_record_id",),
                clarification_question=(
                    "请提供需要退款的账单 ID（Billing ID / 账单编号，例如 bill_...）。"
                ),
            )

    key_intent = _key_action_requested(current) or (
        _key_action_requested(history) and bool(_API_KEY_REF.search(current))
    )
    if key_intent:
        key_ref = _API_KEY_REF.search(continuation_surface)
        if key_ref is None:
            return ActionAdmission(
                action_type="api_key_revocation",
                issue_type="credential_security",
                missing_fields=("api_key_ref",),
                clarification_question=(
                    "请提供要撤销的 API Key 引用（Key Reference，例如 key_...）；不要发送完整密钥。"
                ),
            )

    entitlement_intent = _entitlement_action_requested(current) or (
        _entitlement_action_requested(history) and bool(_CONCURRENCY_TARGET.search(current))
    )
    if entitlement_intent:
        target_match = _CONCURRENCY_TARGET.search(continuation_surface)
        if target_match is None:
            return ActionAdmission(
                action_type="entitlement_change",
                issue_type="entitlement_change",
                missing_fields=("target.concurrency_limit",),
                clarification_question=("请提供希望调整到的具体并发上限目标值（例如 40）。"),
            )
        target = validate_entitlement_target(
            "quota_change",
            {"concurrency_limit": int(target_match.group(1))},
        )
        if not target:
            raise ValueError("validated entitlement target cannot be empty")

    return None
