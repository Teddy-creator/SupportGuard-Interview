from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

CANONICAL_UTC_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$"
)
CANONICAL_UTC_TIMESTAMP_EXAMPLE = "2026-07-15T00:00:00.000000+00:00"
_CANONICAL_UTC_TIMESTAMP_RE = re.compile(CANONICAL_UTC_TIMESTAMP_PATTERN, re.ASCII)

CanonicalUtcTimestamp = Annotated[
    str,
    Field(
        pattern=CANONICAL_UTC_TIMESTAMP_PATTERN,
        examples=[CANONICAL_UTC_TIMESTAMP_EXAMPLE],
        json_schema_extra={"format": "date-time"},
    ),
]


def format_canonical_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical_timestamp_requires_aware_datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_canonical_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _CANONICAL_UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("canonical_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("canonical_timestamp_invalid") from exc
    if format_canonical_utc_timestamp(parsed) != value:
        raise ValueError("canonical_timestamp_invalid")
    return parsed


def parse_database_utc_timestamp(value: object) -> datetime:
    """Parse PostgreSQL's valid UTC JSON timestamp at an internal DB boundary.

    PostgreSQL trims trailing fractional-second zeros when a ``timestamptz`` is
    converted to JSON. External contracts remain six-digit canonical strings;
    callers re-serialize the returned datetime with
    ``format_canonical_utc_timestamp``.
    """

    if not isinstance(value, str):
        raise ValueError("database_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("database_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError("database_timestamp_invalid")
    return parsed.astimezone(UTC)


def validate_canonical_utc_timestamp_text(value: object) -> str:
    parse_canonical_utc_timestamp(value)
    return str(value)
