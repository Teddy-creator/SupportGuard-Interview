from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from supportguard.agent.evidence_contracts import (
    EligibleCitation,
    EvidenceDecision,
    EvidenceGroup,
    EvidenceRequirements,
    FreshScopedObservation,
)
from supportguard.agent.schemas import CandidateResponse

_COMPARISON_QUANTIFIED_MARKER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:\d+(?:\.\d+)?\s*"
    r"(?:tokens?|tok|kb|mb|gb|tb|ms|qps|rps|k|m|b|%|％|万|亿))"
    r"(?![A-Za-z0-9])",
    re.I,
)
_APPLICABILITY_QUESTION = re.compile(
    r"(?:"
    r"适用|是否支持|支持吗|能否|可以吗|是否可用|以哪个为准|"
    r"一样吗|相同吗|有区别吗|区域|套餐|模型|"
    r"\b(?:apply|applicable|supported|available|same|different|which version)\b"
    r")",
    re.I,
)
_CLOUD_REGION_SLUG = re.compile(
    r"\b(?:af|ap|ca|eu|me|sa|us)-"
    r"(?:central|east|north|northeast|northwest|south|southeast|southwest|west)"
    r"(?:-\d+)?\b",
    re.I,
)
_NAMED_REGION = re.compile(
    r"(?:部署区域|区域|region)\s*(?:是|为|=|:|：|in)?\s*"
    r"([a-z][a-z0-9-]{1,63}|[\u4e00-\u9fff]{1,12}区)",
    re.I,
)
_CHINESE_REGION = re.compile(
    r"(?:在|于)\s*([\u4e00-\u9fff]{1,8}区)"
    r"(?=\s*(?:也|是否|能否|可以|适用|支持))"
)
_PLAN_QUALIFIER = re.compile(
    r"\b(free|starter|pro|enterprise)\b(?:\s*(?:plan|tier|套餐))?",
    re.I,
)
_GENERIC_REGION_REQUIREMENT = re.compile(
    r"(?:区域|地区).{0,8}(?:要求|限制|适用|支持|可用)|"
    r"(?:要求|限制|适用).{0,8}(?:区域|地区)|"
    r"\b(?:region|regional)\s+(?:requirement|restriction|availability|support)\b",
    re.I,
)
_GENERIC_PLAN_REQUIREMENT = re.compile(
    r"(?:套餐).{0,8}(?:要求|限制|适用|支持|可用)|"
    r"(?:要求|限制|适用).{0,8}(?:套餐)|"
    r"\b(?:plan|tier)\s+(?:requirement|restriction|availability|support)\b",
    re.I,
)
_REGION_CONCLUSION = re.compile(
    r"(?:没有|无|不受|不限).{0,8}(?:区域|地区)|"
    r"(?:区域|地区).{0,10}(?:没有|无|不限|仅限|适用于|覆盖|支持)|"
    r"\b(?:no|without|all|any|supported)\s+(?:region|regional)",
    re.I,
)
_PLAN_CONCLUSION = re.compile(
    r"(?:没有|无|不受|不限).{0,8}(?:套餐)|"
    r"(?:套餐).{0,10}(?:没有|无|不限|仅限|适用于|覆盖|支持)|"
    r"\b(?:no|without|all|any|supported)\s+(?:plan|tier)",
    re.I,
)
_REFERENTIAL_COMPARISON_SCOPE = re.compile(
    r"(?:"
    r"(?:刚才|此前|之前|前(?:面|文)|上(?:面|述))"
    r".{0,16}(?:提到|说到|讨论|说明)?"
    r".{0,8}(?:限制|上限|差异|区别|变化|变更|版本)|"
    r"(?:这|那)(?:两个|些)?(?:版本|限制|上限|差异|区别|变化|变更)|"
    r"\b(?:previously mentioned|discussed earlier|above|earlier)"
    r".{0,32}(?:limit|difference|change|version)\b"
    r")",
    re.I,
)
_REFERENCE_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]+")
_REFERENCE_TECHNICAL_ANCHOR = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z][A-Za-z0-9]*(?:[_./-][A-Za-z0-9$]+)+|"
    r"\$ref|(?:19|20)\d{2}"
    r")(?![A-Za-z0-9])",
    re.I,
)
_REFERENCE_FACETS: dict[str, tuple[str, ...]] = {
    "context": ("上下文", "context"),
    "limit": ("上限", "限制", "limit"),
    "version": ("版本", "版", "version", "revision"),
    "json": ("json",),
    "schema": ("schema", "结构"),
}
_COMPARISON_CURRENT_CUE = re.compile(
    r"(?:当前|新版|现行|最新|\b(?:current|new|latest)\b)",
    re.I,
)
_COMPARISON_HISTORICAL_CUE = re.compile(
    r"(?:旧|历史|此前|原来|\b(?:old|legacy|historical|previous)\b)",
    re.I,
)
_COMPARISON_TRANSITION_CUE = re.compile(
    r"(?:从.+?(?:到|至)|提升至|提高到|降低至|变更为|\b(?:from|changed|increased|decreased).+?\bto\b)",
    re.I,
)
_FACET_PUBLIC_LABELS: dict[str, str] = {
    "context": "上下文",
    "limit": "限制",
    "version": "版本差异",
    "json": "JSON 输出",
    "schema": "Schema 限制",
}


class EvidenceAssessment(BaseModel):
    """Deterministic acceptance boundary for a model-proposed terminal result."""

    model_config = ConfigDict(extra="forbid")

    sufficient: bool
    required_groups: list[str] = Field(default_factory=list)
    satisfied_groups: list[str] = Field(default_factory=list)
    missing_groups: list[str] = Field(default_factory=list)
    stale_groups: list[str] = Field(default_factory=list)
    result: Literal["accept", "replan", "terminal"]
    error_code: str | None = None


class ReferentialApplicabilityContract(BaseModel):
    """Evidence-confirmed topic shape for one anaphoric applicability turn."""

    model_config = ConfigDict(extra="forbid")

    marker_hints: list[str] = Field(default_factory=list)
    required_facets: list[str] = Field(default_factory=list)


def comparison_transition_markers(evidence: Iterable[dict[str, Any]]) -> list[str]:
    """Return bounded quantified markers shared by both comparison lanes.

    Focused current and historical spans commonly repeat the same material
    transition (for example, an old and a new capacity). Requiring at least two
    markers avoids treating an isolated number, date, or document version as the
    substance of a comparison.
    """

    markers_by_group: dict[str, list[str]] = {"current": [], "historical": []}
    explicit_pairs: list[tuple[str, str]] = []
    for item in evidence:
        if (
            item.get("supporting_span_eligible") is not True
            or item.get("evidence_group") not in markers_by_group
        ):
            continue
        group = str(item["evidence_group"])
        span = str(item.get("supporting_span") or "")
        pair = _comparison_marker_pair(span)
        if pair is not None and pair not in explicit_pairs:
            explicit_pairs.append(pair)
        for match in _COMPARISON_QUANTIFIED_MARKER.finditer(span):
            marker = re.sub(r"\s+", "", match.group(0)).casefold()
            if marker and marker not in markers_by_group[group]:
                markers_by_group[group].append(marker)
    historical = set(markers_by_group["historical"])
    shared = [item for item in markers_by_group["current"] if item in historical]
    if len(shared) >= 2:
        return shared[:4]
    if explicit_pairs:
        return list(explicit_pairs[0])
    return []


def comparison_transition_claim(evidence: Iterable[dict[str, Any]]) -> str | None:
    """Return one minimal transition fact from a single eligible bound span.

    A comparison may cite a historical changelog whose focused span names the
    revision without repeating both old and new limits.  The current published
    compatibility span can still carry the complete transition.  Requiring the
    pair to coexist in one selected span keeps this fallback attributable and
    prevents conversation memory from becoming factual authority.
    """

    for item in evidence:
        if item.get("supporting_span_eligible") is not True:
            continue
        pair = _comparison_marker_pair(str(item.get("supporting_span") or ""))
        if pair is None:
            continue
        historical, current = pair
        return f"关键版本变化是：旧版本为 {historical}，当前版本为 {current}。"
    return None


def comparison_transition_roles_explicit(
    candidate: CandidateResponse,
    required_markers: list[str],
) -> bool:
    """Return whether one public claim labels the old and current values.

    A bare ``from A to B`` transition can be numerically complete while still
    leaving the reader to infer which value is the active product contract.
    Publication therefore treats temporal roles as explicit only when the
    evidence-derived markers are attached to old/historical and
    current/new-version cues in the same public claim set.
    """

    if not required_markers:
        return True
    public_claims = candidate_public_claim_text(candidate)
    pair = _comparison_explicit_role_pair(public_claims)
    if pair is None:
        return False
    normalized_pair = set(pair)
    return all(marker in normalized_pair for marker in required_markers)


def _comparison_explicit_role_pair(span: str) -> tuple[str, str] | None:
    """Extract a quantified pair only when both temporal roles are explicit."""

    clauses = [
        item.strip()
        for item in re.split(
            r"[。！？!?；;，,\n]+|\b(?:while|whereas)\b|(?:而|但)",
            span,
            flags=re.I,
        )
        if item.strip()
    ]
    historical: list[str] = []
    current: list[str] = []
    for clause in clauses:
        marker_matches = [
            (match.start(), re.sub(r"\s+", "", match.group(0)).casefold())
            for match in _COMPARISON_QUANTIFIED_MARKER.finditer(clause)
        ]
        historical_cues = [match.start() for match in _COMPARISON_HISTORICAL_CUE.finditer(clause)]
        current_cues = [match.start() for match in _COMPARISON_CURRENT_CUE.finditer(clause)]
        for marker_position, marker in marker_matches:
            historical_distance = min(
                (abs(marker_position - position) for position in historical_cues),
                default=None,
            )
            current_distance = min(
                (abs(marker_position - position) for position in current_cues),
                default=None,
            )
            if historical_distance is not None and (
                current_distance is None or historical_distance < current_distance
            ):
                if marker not in historical:
                    historical.append(marker)
            elif current_distance is not None and marker not in current:
                current.append(marker)
    for historical_marker in historical:
        for current_marker in current:
            if historical_marker != current_marker:
                return historical_marker, current_marker
    return None


def _comparison_marker_pair(span: str) -> tuple[str, str] | None:
    """Extract an old/current quantified pair only from explicit comparison prose."""

    explicit_pair = _comparison_explicit_role_pair(span)
    if explicit_pair is not None:
        return explicit_pair

    clauses = [
        item.strip()
        for item in re.split(
            r"[。！？!?；;，,\n]+|\b(?:while|whereas)\b|(?:而|但)",
            span,
            flags=re.I,
        )
        if item.strip()
    ]
    all_markers: list[str] = []
    for clause in clauses:
        marker_matches = [
            (match.start(), re.sub(r"\s+", "", match.group(0)).casefold())
            for match in _COMPARISON_QUANTIFIED_MARKER.finditer(clause)
        ]
        markers = list(dict.fromkeys(marker for _, marker in marker_matches if marker))
        all_markers.extend(marker for marker in markers if marker not in all_markers)
    if _COMPARISON_TRANSITION_CUE.search(span) and len(all_markers) >= 2:
        return all_markers[0], all_markers[-1]
    return None


def missing_comparison_transition_markers(
    candidate: CandidateResponse,
    required_markers: list[str],
) -> list[str]:
    """Return evidence-derived markers omitted from the actually published claims."""

    if not required_markers:
        return []
    public_claims = re.sub(r"\s+", "", candidate_public_claim_text(candidate)).casefold()
    return [marker for marker in required_markers if marker not in public_claims]


def referential_applicability_contract(
    *,
    previous_assistant_answer: str | None,
    evidence: Iterable[dict[str, Any]],
) -> ReferentialApplicabilityContract:
    """Derive bounded topic-continuity requirements from current evidence.

    The previous assistant answer is an untrusted anaphora hint. A marker or
    semantic facet becomes a publication requirement only when it occurs in an
    eligible supporting span from the current Agent Run as well. This lets a
    region/plan follow-up preserve the material topic it refers to without
    turning conversation memory into authority or hard-coding a demo case.
    """

    if not previous_assistant_answer:
        return ReferentialApplicabilityContract()
    evidence_text = "\n".join(
        str(item.get("supporting_span") or "")
        for item in evidence
        if item.get("supporting_span_eligible") is True
    )
    if not evidence_text:
        return ReferentialApplicabilityContract()
    compact_evidence = re.sub(r"\s+", "", evidence_text).casefold()
    marker_requirements: list[str] = []
    facet_requirements: list[str] = []
    for sentence in _REFERENCE_SENTENCE_BOUNDARY.split(previous_assistant_answer):
        sentence = sentence.strip()
        if not sentence:
            continue
        compact_sentence = re.sub(r"\s+", "", sentence).casefold()
        anchors = [
            re.sub(r"\s+", "", match.group(0)).casefold()
            for pattern in (_COMPARISON_QUANTIFIED_MARKER, _REFERENCE_TECHNICAL_ANCHOR)
            for match in pattern.finditer(sentence)
        ]
        confirmed_anchors = [
            marker for marker in dict.fromkeys(anchors) if marker and marker in compact_evidence
        ]
        if not confirmed_anchors:
            continue
        marker_requirements.extend(confirmed_anchors)
        for facet, alternatives in _REFERENCE_FACETS.items():
            if any(
                alternative.casefold() in compact_sentence
                and alternative.casefold() in compact_evidence
                for alternative in alternatives
            ):
                facet_requirements.append(facet)
    facets = list(dict.fromkeys(facet_requirements))[:6]
    markers = list(dict.fromkeys(marker_requirements))[: 16 - len(facets)]
    return ReferentialApplicabilityContract(
        marker_hints=markers,
        required_facets=facets,
    )


def missing_referential_applicability_requirements(
    candidate: CandidateResponse,
    required_facets: Iterable[str],
) -> list[str]:
    """Return evidence-confirmed semantic facets omitted from publication.

    Exact marker values are Provider rewrite hints, not publication gates. A
    natural answer can preserve a context-limit or schema topic without
    repeating every historical number or technical identifier verbatim.
    """

    answer = re.sub(r"\s+", "", candidate_public_claim_text(candidate)).casefold()
    missing: list[str] = []
    for facet in required_facets:
        alternatives = _REFERENCE_FACETS.get(facet, ())
        if alternatives and not any(
            alternative.casefold() in answer for alternative in alternatives
        ):
            missing.append(f"topic_facet:{facet}")
    return missing


def refers_to_prior_comparison_scope(message: str) -> bool:
    """Return whether this turn explicitly asks about a prior comparison.

    Conversation history may help retrieve evidence but cannot by itself widen
    the publication contract.  This predicate requires a customer-authored
    reference to a prior limit, difference, change, or version so a completed
    Compare observation cannot silently lose the material transition when the
    final answer is published.
    """

    return bool(message and _REFERENTIAL_COMPARISON_SCOPE.search(message))


def explicit_applicability_conditions(
    message: str,
    *,
    issue_type: str,
    policy_boundary: str,
    requested_action: str,
) -> tuple[str, ...]:
    """Extract customer-supplied region or plan constraints from this turn.

    These values remain untrusted request conditions, never product facts.
    They only keep the publication boundary from silently dropping a
    condition that the customer explicitly asked about.
    """

    if (
        not message
        or issue_type != "product_knowledge"
        or policy_boundary != "allowed"
        or requested_action != "none"
        or not _APPLICABILITY_QUESTION.search(message)
    ):
        return ()
    conditions: list[str] = []
    conditions.extend(match.group(0) for match in _CLOUD_REGION_SLUG.finditer(message))
    conditions.extend(match.group(1) for match in _NAMED_REGION.finditer(message))
    conditions.extend(match.group(1) for match in _CHINESE_REGION.finditer(message))
    conditions.extend(match.group(1) for match in _PLAN_QUALIFIER.finditer(message))
    return tuple(dict.fromkeys(value.strip() for value in conditions if value.strip()))


def requested_generic_applicability_dimensions(
    message: str,
    *,
    issue_type: str,
    policy_boundary: str,
    requested_action: str,
) -> tuple[Literal["region", "plan"], ...]:
    """Return requested scope dimensions when no concrete scope was supplied."""

    if (
        not message
        or issue_type != "product_knowledge"
        or policy_boundary != "allowed"
        or requested_action != "none"
    ):
        return ()
    explicit_conditions = explicit_applicability_conditions(
        message,
        issue_type=issue_type,
        policy_boundary=policy_boundary,
        requested_action=requested_action,
    )
    explicit_dimensions = {
        "plan" if condition.casefold() in {"free", "starter", "pro", "enterprise"} else "region"
        for condition in explicit_conditions
    }
    dimensions: list[Literal["region", "plan"]] = []
    if _GENERIC_REGION_REQUIREMENT.search(message) and "region" not in explicit_dimensions:
        dimensions.append("region")
    if _GENERIC_PLAN_REQUIREMENT.search(message) and "plan" not in explicit_dimensions:
        dimensions.append("plan")
    return tuple(dimensions)


def applicability_dimension_answered(text: str, dimension: Literal["region", "plan"]) -> bool:
    pattern = _REGION_CONCLUSION if dimension == "region" else _PLAN_CONCLUSION
    return bool(pattern.search(text))


def generic_applicability_dimension_claim(
    dimension: Literal["region", "plan"],
    evidence: Iterable[dict[str, Any]],
) -> str | None:
    """Publish one minimal scope fact from selected eligibility metadata."""

    field = "applicable_region" if dimension == "region" else "applicable_plan"
    label = "区域" if dimension == "region" else "套餐"
    scopes: list[str | None] = []
    for item in evidence:
        if item.get("supporting_span_eligible") is not True:
            return None
        envelope = item.get("eligibility_envelope")
        if not isinstance(envelope, dict) or field not in envelope:
            return None
        raw_scope = envelope.get(field)
        scopes.append(str(raw_scope).strip() if raw_scope is not None else None)
    if not scopes:
        return None
    unique_scopes = set(scopes)
    if unique_scopes == {None}:
        return f"当前引用资料未设置{label}限定，因此上述规则没有额外{label}要求。"
    if None in unique_scopes or len(unique_scopes) != 1:
        return None
    scope = next(iter(unique_scopes))
    return f"当前引用资料将上述规则限定在 {scope} {label}。"


def applicability_scope_claim(
    condition: str,
    evidence: Iterable[dict[str, Any]],
    *,
    topic_facets: Iterable[str] = (),
) -> str | None:
    """Return a deterministic claim when bound evidence covers one condition.

    Applicability metadata is authoritative Runtime state rather than model
    prose.  A null plan/region is the existing RAG wildcard contract; an exact
    value covers only the same customer-supplied condition.  Missing metadata,
    an incompatible scope, or an unknown condition shape fails closed.
    """

    normalized = condition.strip().casefold()
    if not normalized:
        return None
    if normalized in {"free", "starter", "pro", "enterprise"}:
        field = "applicable_plan"
        label = "套餐"
    elif _CLOUD_REGION_SLUG.fullmatch(condition.strip()) or condition.strip().endswith("区"):
        field = "applicable_region"
        label = "区域"
    else:
        return None

    scopes: list[str | None] = []
    for item in evidence:
        if item.get("supporting_span_eligible") is not True:
            return None
        envelope = item.get("eligibility_envelope")
        if not isinstance(envelope, dict) or field not in envelope:
            return None
        raw_scope = envelope.get(field)
        scope = str(raw_scope).strip() if raw_scope is not None else None
        if scope is not None and scope.casefold() != normalized:
            return None
        scopes.append(scope)
    if not scopes:
        return None
    facets = [
        facet
        for facet in dict.fromkeys(str(item) for item in topic_facets)
        if facet in _FACET_PUBLIC_LABELS
    ]
    labels: list[str] = []
    if "context" in facets and "limit" in facets:
        labels.append("上下文上限")
        facets = [facet for facet in facets if facet not in {"context", "limit"}]
    labels.extend(_FACET_PUBLIC_LABELS[facet] for facet in facets)
    subject = f"其中关于{'、'.join(labels)}的规则" if labels else "其中所述规则"
    if all(scope is None for scope in scopes):
        return f"当前引用资料没有{label}限定，因此{subject}适用于 {condition}。"
    return f"当前引用资料的适用{label}覆盖 {condition}，因此{subject}适用于 {condition}。"


def supported_referential_facets(
    evidence: Iterable[dict[str, Any]],
    requested_facets: Iterable[str],
) -> list[str]:
    """Return requested semantic facets supported by the supplied eligible spans."""

    text = re.sub(
        r"\s+",
        "",
        "\n".join(
            str(item.get("supporting_span") or "")
            for item in evidence
            if item.get("supporting_span_eligible") is True
        ),
    ).casefold()
    if not text:
        return []
    return [
        facet
        for facet in dict.fromkeys(str(item) for item in requested_facets)
        if facet in _REFERENCE_FACETS
        and any(alternative.casefold() in text for alternative in _REFERENCE_FACETS[facet])
    ]


def candidate_public_claim_text(candidate: CandidateResponse) -> str:
    """Return the text that publication renders for an evidence-backed answer."""

    claims = [claim.text.strip() for claim in candidate.material_claims if claim.text.strip()]
    return "\n".join(claims) if claims else candidate.answer


def _canonical_scope_hash(*, tenant_id: str, customer_id: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"customer_id": customer_id, "tenant_id": tenant_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _evidence_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _observation_scope_matches(
    observation: dict[str, Any],
    *,
    run_id: str,
    tenant_id: str,
    customer_id: str,
) -> bool:
    trusted_scope = observation.get("trusted_scope")
    if not isinstance(trusted_scope, dict):
        return False
    expected = _canonical_scope_hash(tenant_id=tenant_id, customer_id=customer_id)
    return bool(
        observation.get("run_id") == run_id
        and observation.get("tenant_id") == tenant_id
        and observation.get("customer_id") == customer_id
        and observation.get("scope_hash") == expected
        and trusted_scope.get("tenant_id") == tenant_id
        and trusted_scope.get("customer_id") == customer_id
        and trusted_scope.get("scope_hash") == expected
    )


def _fresh_scoped_observation(
    observation: dict[str, Any],
    *,
    run_id: str,
    tenant_id: str,
    customer_id: str,
    now: datetime,
) -> tuple[FreshScopedObservation | None, str | None]:
    expected_scope_hash = _canonical_scope_hash(
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    if not _observation_scope_matches(
        observation,
        run_id=run_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
    ):
        return None, "observation_not_current_scope"
    if observation.get("status") != "ok":
        return None, "observation_not_successful"
    if observation.get("freshness_status") != "fresh":
        return None, "observation_not_fresh_at_decision_time"
    observed_at = _evidence_time(observation.get("observed_at"))
    fresh_until = _evidence_time(observation.get("fresh_until"))
    tool_call_id = str(observation.get("tool_call_id") or "")
    observation_id = str(observation.get("observation_id") or tool_call_id)
    tool_name = str(observation.get("tool_name") or "")
    if (
        not observed_at
        or not fresh_until
        or not tool_call_id
        or not observation_id
        or not tool_name
    ):
        return None, "observation_identity_incomplete"
    if not (observed_at <= now <= fresh_until):
        return None, "observation_not_fresh_at_decision_time"
    source_refs = observation.get("source_refs")
    source_ids = (
        tuple(
            str(item["source_id"])
            for item in source_refs
            if isinstance(item, dict) and item.get("source_id")
        )
        if isinstance(source_refs, list)
        else ()
    )
    if len(source_ids) != len(set(source_ids)):
        return None, "observation_source_identity_duplicate"
    data = observation.get("data")
    evidence_items = data.get("evidence", []) if isinstance(data, dict) else []
    evidence_ids = (
        tuple(
            str(item["evidence_id"])
            for item in evidence_items
            if isinstance(item, dict)
            and item.get("evidence_id")
            and item.get("supporting_span_eligible") is True
        )
        if isinstance(evidence_items, list)
        else ()
    )
    if len(evidence_ids) != len(set(evidence_ids)):
        return None, "observation_evidence_identity_duplicate"
    resource_version = observation.get("resource_version")
    if resource_version is None and isinstance(observation.get("data"), dict):
        resource_version = observation["data"].get("resource_version") or observation["data"].get(
            "version"
        )
    try:
        return FreshScopedObservation(
            observation_id=observation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            run_id=run_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            scope_hash=expected_scope_hash,
            observed_at=observed_at,
            fresh_until=fresh_until,
            source_ids=source_ids,
            evidence_ids=evidence_ids,
            resource_version=str(resource_version) if resource_version is not None else None,
        ), None
    except ValueError:
        return None, "observation_projection_invalid"


def _eligible_citations(
    *,
    citation_bindings: Iterable[dict[str, Any]],
    provider_attempt_id: str,
    eligible_evidence_ids: set[str],
) -> tuple[tuple[EligibleCitation, ...], list[str]]:
    reasons: list[str] = []
    raw_by_id: dict[str, list[dict[str, Any]]] = {}
    for raw in citation_bindings:
        binding_id = str(raw.get("citation_binding_id") or "")
        if not binding_id:
            reasons.append("citation_binding_identity_missing")
            continue
        raw_by_id.setdefault(binding_id, []).append(raw)
    eligible: list[EligibleCitation] = []
    for binding_id, rows in raw_by_id.items():
        if len(rows) != 1:
            reasons.append(f"citation_binding_not_unique:{binding_id}")
            continue
        raw = rows[0]
        if raw.get("provider_attempt_id") != provider_attempt_id:
            reasons.append(f"citation_binding_not_current:{binding_id}")
            continue
        try:
            eligible.append(
                EligibleCitation(
                    citation_binding_id=binding_id,
                    provider_attempt_id=str(raw.get("provider_attempt_id") or ""),
                    evidence_id=str(raw.get("evidence_id") or ""),
                    document_id=str(raw.get("document_id") or ""),
                    chunk_id=str(raw.get("chunk_id") or ""),
                    content_hash=str(raw.get("content_hash") or ""),
                    locator_hash=str(raw.get("locator_hash") or ""),
                )
            )
        except ValueError:
            reasons.append(f"citation_binding_invalid:{binding_id}")
    reasons.extend(
        f"citation_evidence_not_eligible:{item.citation_binding_id}"
        for item in eligible
        if item.evidence_id not in eligible_evidence_ids
    )
    eligible = [item for item in eligible if item.evidence_id in eligible_evidence_ids]
    accepted_by_identity: dict[str, list[EligibleCitation]] = {}
    for item in eligible:
        accepted_by_identity.setdefault(item.evidence_id, []).append(item)
    ambiguous_ids = {
        item.citation_binding_id
        for items in accepted_by_identity.values()
        if len(items) > 1
        for item in items
    }
    reasons.extend(
        f"citation_evidence_identity_ambiguous:{binding_id}" for binding_id in sorted(ambiguous_ids)
    )
    eligible = [item for item in eligible if item.citation_binding_id not in ambiguous_ids]
    return tuple(eligible), reasons


def derive_evidence_requirements(
    *,
    issue_type: str,
    requested_action: str = "none",
    specified_request: bool = False,
    additional_groups: Iterable[EvidenceGroup] = (),
) -> EvidenceRequirements:
    """Derive evidence capabilities from deterministic request semantics."""

    groups: list[EvidenceGroup] = []
    if issue_type in {
        "api_diagnostics",
        "billing_refund",
        "credential_security",
        "incident_support",
        "product_knowledge",
        "entitlement_change",
    }:
        groups.append("knowledge")
    if issue_type in {"api_diagnostics", "incident_support"} and specified_request:
        groups.append("request_trace")
    action_group: dict[str, EvidenceGroup] = {
        "refund": "billing_record",
        "api_key_revocation": "api_key_metadata",
        "entitlement_change": "subscription",
    }
    if requested_action in action_group:
        groups.append(action_group[requested_action])
    groups.extend(additional_groups)
    return EvidenceRequirements(required_groups=tuple(dict.fromkeys(groups)))


def _evidence_group(observation: FreshScopedObservation) -> EvidenceGroup | None:
    groups: dict[str, EvidenceGroup] = {
        "search_knowledge": "knowledge",
        "query_request_trace": "request_trace",
        "query_billing_record": "billing_record",
        "query_api_key_metadata": "api_key_metadata",
        "query_subscription": "subscription",
        "query_account": "account",
        "query_api_usage": "api_usage",
    }
    group = groups.get(observation.tool_name)
    if group == "knowledge" and not observation.evidence_ids:
        return None
    return group


def _stale_required_groups(
    *,
    observations: Iterable[dict[str, Any]],
    requirements: EvidenceRequirements,
    run_id: str,
    tenant_id: str,
    customer_id: str,
    now: datetime,
) -> set[EvidenceGroup]:
    stale: set[EvidenceGroup] = set()
    required = set(requirements.required_groups)
    tool_groups: dict[str, EvidenceGroup] = {
        "search_knowledge": "knowledge",
        "query_request_trace": "request_trace",
        "query_billing_record": "billing_record",
        "query_api_key_metadata": "api_key_metadata",
        "query_subscription": "subscription",
        "query_account": "account",
        "query_api_usage": "api_usage",
    }
    for observation in observations:
        group = tool_groups.get(str(observation.get("tool_name") or ""))
        if (
            group not in required
            or observation.get("status") != "ok"
            or not _observation_scope_matches(
                observation,
                run_id=run_id,
                tenant_id=tenant_id,
                customer_id=customer_id,
            )
        ):
            continue
        observed_at = _evidence_time(observation.get("observed_at"))
        fresh_until = _evidence_time(observation.get("fresh_until"))
        if (
            observation.get("freshness_status") != "fresh"
            or observed_at is None
            or fresh_until is None
            or not (observed_at <= now <= fresh_until)
        ):
            stale.add(group)
    return stale


def _conflict_reasons(
    observations: Iterable[dict[str, Any]],
    *,
    evidence_conflict: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for observation in observations:
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        refusal_reason = data.get("refusal_reason")
        if refusal_reason:
            reasons.append(str(refusal_reason))
        elif data.get("conflict") is True:
            reasons.append("knowledge_conflict")
    if evidence_conflict and not reasons:
        reasons.append("evidence_conflict")
    return tuple(dict.fromkeys(reasons))


def decide_evidence(
    *,
    requirements: EvidenceRequirements,
    observations: list[dict[str, Any]],
    citation_bindings: Iterable[dict[str, Any]],
    run_id: str,
    tenant_id: str,
    customer_id: str,
    provider_attempt_id: str,
    evidence_conflict: bool,
    can_replan: bool,
    explainable_comparison: bool = False,
    now: datetime | None = None,
) -> EvidenceDecision:
    """Produce the frozen EvidenceDecision before CandidateResponse exists.

    Inputs are Runtime-owned Observation and context-membership records. This
    pure stage performs no I/O and grants no publication or action authority.
    """

    logical_now = now or datetime.now(UTC)
    projected_pairs: list[tuple[dict[str, Any], FreshScopedObservation]] = []
    observation_reasons: list[str] = []
    for observation in observations:
        projected, reason = _fresh_scoped_observation(
            observation,
            run_id=run_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            now=logical_now,
        )
        if projected is not None:
            projected_pairs.append((observation, projected))
        elif reason not in {"observation_not_current_scope", "observation_not_successful"}:
            observation_reasons.append(str(reason))
    pairs_by_id: dict[str, list[tuple[dict[str, Any], FreshScopedObservation]]] = {}
    for pair in projected_pairs:
        pairs_by_id.setdefault(pair[1].observation_id, []).append(pair)
    fresh_pairs: list[tuple[dict[str, Any], FreshScopedObservation]] = []
    for observation_id, pairs in pairs_by_id.items():
        if len(pairs) == 1:
            fresh_pairs.append(pairs[0])
        else:
            observation_reasons.append(f"observation_identity_ambiguous:{observation_id}")
    fresh_observations = tuple(projected for _, projected in fresh_pairs)
    source_owners: dict[str, list[str]] = {}
    for fresh_observation in fresh_observations:
        for source_id in fresh_observation.source_ids:
            source_owners.setdefault(source_id, []).append(fresh_observation.observation_id)
    observation_reasons.extend(
        f"observation_source_identity_ambiguous:{source_id}"
        for source_id, owners in source_owners.items()
        if len(owners) > 1
    )
    eligible_evidence_ids = {
        evidence_id
        for observation in fresh_observations
        for evidence_id in observation.evidence_ids
    }
    eligible_citations, reasons = _eligible_citations(
        citation_bindings=citation_bindings,
        provider_attempt_id=provider_attempt_id,
        eligible_evidence_ids=eligible_evidence_ids,
    )
    reasons[:0] = observation_reasons
    conflicts = _conflict_reasons(
        (observation for observation, _ in fresh_pairs),
        evidence_conflict=evidence_conflict,
    )
    explainable_conflict = bool(conflicts) and all(
        reason == "unresolved_published_version_conflict" for reason in conflicts
    )
    if conflicts and not (explainable_comparison and explainable_conflict):
        reasons.extend(f"conflict:{reason}" for reason in conflicts)
    satisfied = {
        group
        for observation in fresh_observations
        if (group := _evidence_group(observation)) is not None
    }
    if "knowledge" in satisfied and not eligible_citations:
        satisfied.remove("knowledge")
        reasons.append("eligible_knowledge_citation_missing")
    stale = (
        _stale_required_groups(
            observations=observations,
            requirements=requirements,
            run_id=run_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            now=logical_now,
        )
        - satisfied
    )
    missing = set(requirements.required_groups) - satisfied - stale
    reasons.extend(f"missing_evidence_group:{group}" for group in sorted(missing))
    reasons.extend(f"stale_evidence_group:{group}" for group in sorted(stale))
    reasons = list(dict.fromkeys(reasons))
    sufficient = not reasons and not missing and not stale
    result: Literal["accept", "replan", "terminal"] = (
        "accept" if sufficient else ("replan" if can_replan else "terminal")
    )
    error_code = None
    if not sufficient:
        if any(reason.startswith("conflict:") for reason in reasons):
            error_code = "evidence_conflict"
        elif any("observation" in reason or "source" in reason for reason in reasons):
            error_code = "evidence_freshness_insufficient"
        elif any("citation" in reason or "locator" in reason for reason in reasons):
            error_code = "citation_binding_incomplete"
        else:
            error_code = "evidence_group_incomplete"
    return EvidenceDecision(
        run_id=run_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        provider_attempt_id=provider_attempt_id,
        requirements=requirements,
        sufficient=sufficient,
        result=result,
        eligible_citations=eligible_citations,
        fresh_scoped_observations=fresh_observations,
        satisfied_groups=tuple(
            group for group in requirements.required_groups if group in satisfied
        ),
        missing_groups=tuple(group for group in requirements.required_groups if group in missing),
        stale_groups=tuple(group for group in requirements.required_groups if group in stale),
        conflict_reasons=conflicts,
        insufficient_reasons=tuple(reasons),
        error_code=error_code,
    )


def assess_terminal_evidence(
    *,
    issue_type: str,
    candidate: CandidateResponse,
    observations: list[dict[str, Any]],
    evidence_conflict: bool,
    specified_request: bool,
    can_replan: bool,
    explainable_comparison: bool = False,
    now: datetime | None = None,
) -> EvidenceAssessment:
    """Evaluate fact classes, never a prescribed tool sequence.

    The model remains responsible for choosing allowlisted read tools.  This
    function only decides whether the proposed result is supportable by the
    current run's terminal observations.
    """

    if candidate.action in {"reject", "manual_takeover"}:
        return EvidenceAssessment(sufficient=True, result="accept")

    required: list[str] = []
    if issue_type in {
        "api_diagnostics",
        "billing_refund",
        "credential_security",
        "incident_support",
        "product_knowledge",
        "entitlement_change",
    }:
        required.append("knowledge")
    if issue_type in {"api_diagnostics", "incident_support"} and specified_request:
        required.append("request_trace")

    action_group = {
        "refund_proposal": "billing_record",
        "api_key_revocation_proposal": "api_key_metadata",
        "entitlement_change_proposal": "subscription",
    }.get(candidate.action)
    if action_group is not None:
        required.append(action_group)

    current = [item for item in observations if item.get("status") == "ok"]
    by_group = {
        "knowledge": [
            item
            for item in current
            if item.get("tool_name") == "search_knowledge"
            and item.get("data", {}).get("evidence")
            and (
                not item.get("data", {}).get("refusal_reason")
                or (
                    explainable_comparison
                    and item.get("data", {}).get("refusal_reason")
                    == "unresolved_published_version_conflict"
                )
            )
        ],
        "request_trace": [
            item for item in current if item.get("tool_name") == "query_request_trace"
        ],
        "billing_record": [
            item for item in current if item.get("tool_name") == "query_billing_record"
        ],
        "api_key_metadata": [
            item for item in current if item.get("tool_name") == "query_api_key_metadata"
        ],
        "subscription": [item for item in current if item.get("tool_name") == "query_subscription"],
    }
    satisfied = [group for group in dict.fromkeys(required) if by_group.get(group)]
    missing = [group for group in dict.fromkeys(required) if group not in satisfied]

    source_to_observation = {
        str(source.get("source_id")): item
        for item in current
        if item.get("tool_name") != "search_knowledge"
        for source in item.get("source_refs", [])
        if source.get("source_id")
    }
    stale: list[str] = []
    for source_id in {
        source_id
        for claim in candidate.material_claims
        for source_id in claim.observation_source_ids
    }:
        observation = source_to_observation.get(source_id)
        if observation is None:
            missing.append("business_observation")
        elif not observation_is_fresh(observation, now=now):
            stale.append(str(observation.get("tool_name") or "business_observation"))

    missing = list(dict.fromkeys(missing))
    stale = list(dict.fromkeys(stale))
    if evidence_conflict and not explainable_comparison:
        missing.append("non_conflicting_knowledge")
    sufficient = not missing and not stale
    if sufficient:
        return EvidenceAssessment(
            sufficient=True,
            required_groups=list(dict.fromkeys(required)),
            satisfied_groups=satisfied,
            result="accept",
        )
    return EvidenceAssessment(
        sufficient=False,
        required_groups=list(dict.fromkeys(required)),
        satisfied_groups=satisfied,
        missing_groups=missing,
        stale_groups=stale,
        result="replan" if can_replan else "terminal",
        error_code=("evidence_freshness_insufficient" if stale else "evidence_group_incomplete"),
    )


def observation_is_fresh(
    observation: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if observation.get("freshness_status") != "fresh":
        return False
    fresh_until = _evidence_time(observation.get("fresh_until"))
    return fresh_until is not None and (now or datetime.now(UTC)) <= fresh_until
