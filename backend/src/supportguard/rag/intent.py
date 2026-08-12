from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RetrievalIntentEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["retrieval-intent.v1"] = "retrieval-intent.v1"
    intent: Literal["current", "historical", "compare"]
    historical_version: str | None = None
    as_of: datetime | None = None
    reason_code: str


_COMPARE = re.compile(
    r"(?:"
    r"对比|比较|变化|差异|区别|之前.{0,12}现在|"
    r"(?:版本|文档|政策|规则|说明).{0,20}(?:不同|冲突|不一致|矛盾)|"
    r"(?:不同|冲突|不一致|矛盾).{0,20}(?:版本|文档|政策|规则|说明)|"
    r"compare|difference|"
    r"(?:version|document|policy|rule).{0,30}(?:conflict|differ|inconsisten)"
    r")",
    re.I,
)
_HISTORICAL = re.compile(r"(?:历史|旧版|当时|此前|previous|historical|as[ -]?of)", re.I)
_VERSION = re.compile(r"\b(?:v|version[ =:]?)(\d+(?:\.\d+){0,2})\b", re.I)
_CURRENT_VERSION_PREFIX = re.compile(
    r"(?:当前|现在|最新|current|latest|newest).{0,16}$",
    re.I,
)
_HISTORICAL_VERSION_PREFIX = re.compile(
    r"(?:历史|旧版|旧版本|此前|previous|historical|old).{0,16}$",
    re.I,
)
_ISO_DATE = re.compile(r"(?<!\d)(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?!\d)")
_ZH_DATE = re.compile(r"(20\d{2})年(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日")
_AMBIGUOUS_DATE = re.compile(r"(?:20\d{2}年(?:0?[1-9]|1[0-2])月?|20\d{2}-(?:0?[1-9]|1[0-2]))")


def canonical_document_version(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized[:1].lower() == "v":
        normalized = normalized[1:]
    return normalized or None


def _parse_date(user_text: str) -> tuple[datetime | None, bool]:
    match = _ISO_DATE.search(user_text) or _ZH_DATE.search(user_text)
    if match is not None:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=UTC,
            ), False
        except ValueError:
            return None, True
    return None, bool(_AMBIGUOUS_DATE.search(user_text))


def _historical_version_anchor(user_text: str) -> str | None:
    """Choose an explicit historical version without stealing a current anchor.

    A comparison query can legitimately mention both ``current v5`` and an
    older version.  Treating the first version token as historical asks the
    historical retrieval lane for the current document and makes a complete
    comparison impossible.  Prefer an explicitly historical token, then a
    neutral token, and ignore tokens whose local prefix marks them as current.
    """

    historical: list[str] = []
    neutral: list[str] = []
    current: set[str] = set()
    for match in _VERSION.finditer(user_text):
        version = canonical_document_version(match.group(1))
        if version is None:
            continue
        prefix = user_text[max(0, match.start() - 24) : match.start()]
        if _HISTORICAL_VERSION_PREFIX.search(prefix):
            historical.append(version)
        elif _CURRENT_VERSION_PREFIX.search(prefix):
            current.add(version)
        else:
            neutral.append(version)
    if historical:
        return historical[0]
    for version in neutral:
        if version not in current:
            return version
    return None


def resolve_retrieval_intent(user_text: str) -> RetrievalIntentEnvelope:
    comparison = bool(_COMPARE.search(user_text))
    version = _historical_version_anchor(user_text)
    as_of, ambiguous_date = _parse_date(user_text)
    if ambiguous_date:
        return RetrievalIntentEnvelope(
            intent="historical" if not comparison else "compare",
            historical_version=version,
            reason_code="ambiguous_historical_anchor",
        )
    if comparison:
        return RetrievalIntentEnvelope(
            intent="compare",
            historical_version=version,
            as_of=as_of,
            reason_code="explicit_comparison_semantics",
        )
    if _HISTORICAL.search(user_text) or version is not None or as_of is not None:
        return RetrievalIntentEnvelope(
            intent="historical",
            historical_version=version,
            as_of=as_of,
            reason_code="explicit_historical_semantics",
        )
    return RetrievalIntentEnvelope(intent="current", reason_code="default_current")
