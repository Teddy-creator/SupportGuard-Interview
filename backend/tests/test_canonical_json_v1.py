from __future__ import annotations

from decimal import Decimal

import pytest

from supportguard.contracts.canonical_json import (
    CanonicalJsonError,
    canonical_decimal_string,
    canonical_json_bytes,
    canonical_json_hash,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.25, "1.25"),
        (1e-07, "0.0000001"),
        (Decimal("49.00"), "49.00"),
        (Decimal("-0.00"), "0"),
    ],
)
def test_decimal_projection_is_finite_non_exponent_string(
    value: Decimal | float, expected: str
) -> None:
    assert canonical_decimal_string(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN")])
def test_decimal_projection_rejects_non_finite(value: Decimal | float) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_decimal_string(value)


@pytest.mark.parametrize(
    ("value", "encoded", "digest"),
    [
        (
            {"b": 1, "a": "中文"},
            '{"a":"中文","b":1}',
            "78d145429acf033939ca3676748013583b73c65617891b3f6f0cd3cb64fc2dfd",
        ),
        (
            {"composed": "é", "decomposed": "e\u0301", "emoji": "😀"},
            '{"composed":"é","decomposed":"é","emoji":"😀"}',
            "f7875800432e80619a4aa05c55b8df883de5a023c212a1ce8cd02933fb55c650",
        ),
        (
            {"z": None, "a": [True, False, {"k": "v"}]},
            '{"a":[true,false,{"k":"v"}],"z":null}',
            "ec11ac735eb22aed36c51c933a5216f96d1d12ed9c83dc895148c21c90eeba8b",
        ),
        (
            {"escaped": 'line\n"quote"\\slash'},
            '{"escaped":"line\\n\\"quote\\"\\\\slash"}',
            "41aeb698edf5adf940ebefe0399cd62e8adde7acc2c7216deee89987798ecb27",
        ),
        (
            {"array": [3, 2, 1]},
            '{"array":[3,2,1]}',
            "3348bd6d6c610d1594a57a327aef673bff1c30aa0dfdcfc0508704775261afdd",
        ),
        (
            {"money": "49.00", "zero": 0},
            '{"money":"49.00","zero":0}',
            "a9a5e43e2b34a42eb48e6c26582f4434fa5298e91eded0745ceb9fd48e4bafd4",
        ),
    ],
)
def test_frozen_vectors(value: object, encoded: str, digest: str) -> None:
    assert canonical_json_bytes(value) == encoded.encode("utf-8")
    assert canonical_json_hash(value) == digest


@pytest.mark.parametrize(
    "value",
    [1.0, float("nan"), float("inf"), Decimal("1.00"), (1, 2), {1: "bad"}, b"bad"],
)
def test_non_canonical_inputs_fail_closed(value: object) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes(value)


def test_unicode_is_not_normalized() -> None:
    assert canonical_json_hash("é") != canonical_json_hash("e\u0301")


def test_unpaired_surrogate_is_rejected() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes("\ud800")
