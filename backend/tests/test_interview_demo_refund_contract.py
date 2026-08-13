from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from supportguard.contracts.refund_policy import (
    REFUND_APPLICATION_WINDOW_DAYS,
    RefundChargeFacts,
    evaluate_refund_pair,
    refund_pair_checks_payload,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)


def _charge(
    billing_record_id: str,
    *,
    duplicate_of: str | None = None,
) -> RefundChargeFacts:
    return RefundChargeFacts(
        billing_record_id=billing_record_id,
        tenant_id="tenant_demo",
        customer_id="cust_demo",
        amount=Decimal("49.00"),
        currency="USD",
        status="charged",
        charged_at=NOW - timedelta(days=1),
        service_period_start=PERIOD_START,
        service_period_end=PERIOD_END,
        version=2 if duplicate_of else 1,
        duplicate_of=duplicate_of,
    )


def test_refund_pair_requires_two_explicit_matching_recent_charges() -> None:
    original = _charge("bill_demo_original")
    target = _charge(
        "bill_demo_duplicate",
        duplicate_of=original.billing_record_id,
    )

    decision = evaluate_refund_pair(target, original, now=NOW)

    assert decision.eligible is True
    assert decision.pair_hash == (
        "d23da159a0514474b21607fea4a06d49b7a3d82e696594c956a372191469c034"
    )
    assert refund_pair_checks_payload(decision.checks) == {
        "same_scope": True,
        "explicit_relation": True,
        "both_charged": True,
        "same_amount": True,
        "same_currency": True,
        "same_service_period": True,
        "within_application_window": True,
    }


@pytest.mark.parametrize(
    ("target_change", "original_change", "failed_check"),
    (
        ({"tenant_id": "tenant_other"}, {}, "same_scope"),
        ({"duplicate_of": "bill_wrong"}, {}, "explicit_relation"),
        ({"status": "refunded"}, {}, "both_charged"),
        ({"amount": Decimal("50.00")}, {}, "same_amount"),
        ({"currency": "EUR"}, {}, "same_currency"),
        ({"service_period_end": date(2026, 10, 1)}, {}, "same_service_period"),
        (
            {"charged_at": NOW - timedelta(days=REFUND_APPLICATION_WINDOW_DAYS + 1)},
            {},
            "within_application_window",
        ),
        ({"charged_at": NOW + timedelta(seconds=1)}, {}, "within_application_window"),
        ({}, {"charged_at": NOW + timedelta(seconds=1)}, "within_application_window"),
    ),
)
def test_refund_pair_fails_closed_for_each_policy_mismatch(
    target_change: dict[str, object],
    original_change: dict[str, object],
    failed_check: str,
) -> None:
    original = replace(_charge("bill_demo_original"), **original_change)
    target = replace(
        _charge("bill_demo_duplicate", duplicate_of="bill_demo_original"),
        **target_change,
    )

    decision = evaluate_refund_pair(target, original, now=NOW)

    assert decision.eligible is False
    assert failed_check in decision.checks.failed


def test_missing_original_never_becomes_a_refund_candidate() -> None:
    target = _charge("bill_demo_duplicate", duplicate_of="bill_demo_original")

    decision = evaluate_refund_pair(target, None, now=NOW)

    assert decision.eligible is False
    assert decision.pair_hash is None
    assert {
        "same_scope",
        "explicit_relation",
        "both_charged",
        "same_amount",
        "same_currency",
        "same_service_period",
        "within_application_window",
    } == set(decision.checks.failed)


def test_naive_database_utc_and_aware_utc_produce_one_pair_identity() -> None:
    original = _charge("bill_demo_original")
    target = _charge("bill_demo_duplicate", duplicate_of="bill_demo_original")
    aware = evaluate_refund_pair(target, original, now=NOW)
    naive = evaluate_refund_pair(
        replace(target, charged_at=target.charged_at.replace(tzinfo=None)),
        replace(original, charged_at=original.charged_at.replace(tzinfo=None)),
        now=NOW.replace(tzinfo=None),
    )

    assert aware.eligible is naive.eligible is True
    assert aware.pair_hash == naive.pair_hash
