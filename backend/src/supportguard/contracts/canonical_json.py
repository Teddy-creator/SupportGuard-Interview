from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TypeAlias

CanonicalScalar: TypeAlias = str | int | bool | None
CanonicalValue: TypeAlias = CanonicalScalar | dict[str, "CanonicalValue"] | list["CanonicalValue"]


class CanonicalJsonError(ValueError):
    """The value cannot be represented by the frozen canonical-json.v1 contract."""


def canonical_decimal_string(value: Decimal | float) -> str:
    """Project one finite decimal-like value to a non-exponent JSON string."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJsonError("non-finite decimal projection")
        projected = Decimal(str(value))
    else:
        if not value.is_finite():
            raise CanonicalJsonError("non-finite decimal projection")
        projected = value
    if projected.is_zero():
        return "0"
    return format(projected, "f")


def _project(value: object, *, path: str = "$") -> CanonicalValue:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalJsonError(f"invalid Unicode scalar at {path}") from exc
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        projected: dict[str, CanonicalValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(f"object key at {path} must be a string")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalJsonError(f"invalid Unicode object key at {path}") from exc
            projected[key] = _project(item, path=f"{path}.{key}")
        return projected
    if isinstance(value, list):
        return [_project(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        raise CanonicalJsonError(f"non-JSON sequence at {path}: {type(value).__name__}")
    raise CanonicalJsonError(f"unsupported canonical value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Encode a schema-projected value using canonical-json.v1 exact bytes."""

    projected = _project(value)
    return json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
