from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from supportguard.contracts.canonical_json import (
    canonical_decimal_string,
    canonical_json_hash,
)
from supportguard.contracts.timestamps import format_canonical_utc_timestamp

REFUND_APPLICATION_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class RefundChargeFacts:
    """The complete, scoped billing facts used by the refund policy."""

    billing_record_id: str
    tenant_id: str
    customer_id: str
    amount: Decimal
    currency: str
    status: str
    charged_at: datetime
    service_period_start: date
    service_period_end: date
    version: int
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class RefundPairChecks:
    same_scope: bool
    explicit_relation: bool
    both_charged: bool
    same_amount: bool
    same_currency: bool
    same_service_period: bool
    within_application_window: bool

    @property
    def eligible(self) -> bool:
        return all(
            (
                self.same_scope,
                self.explicit_relation,
                self.both_charged,
                self.same_amount,
                self.same_currency,
                self.same_service_period,
                self.within_application_window,
            )
        )

    @property
    def failed(self) -> tuple[str, ...]:
        values = (
            ("same_scope", self.same_scope),
            ("explicit_relation", self.explicit_relation),
            ("both_charged", self.both_charged),
            ("same_amount", self.same_amount),
            ("same_currency", self.same_currency),
            ("same_service_period", self.same_service_period),
            ("within_application_window", self.within_application_window),
        )
        return tuple(name for name, passed in values if not passed)


@dataclass(frozen=True, slots=True)
class RefundPairEvaluation:
    target: RefundChargeFacts
    original: RefundChargeFacts | None
    checks: RefundPairChecks
    pair_hash: str | None

    @property
    def eligible(self) -> bool:
        return self.checks.eligible and self.pair_hash is not None


def _aware_utc(value: datetime) -> datetime:
    # PostgreSQL preserves timezone information, while SQLite's DateTime
    # adapter returns the same stored UTC wall clock without ``tzinfo``.
    # The repository boundary has no tenant-configurable timezone and every
    # write is normalized to UTC, so restoring UTC here keeps both dialects on
    # one semantic contract without accepting local-time input at the API.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def refund_pair_payload(
    target: RefundChargeFacts,
    original: RefundChargeFacts,
) -> dict[str, object]:
    """Return the canonical snapshot bound to Proposal, Approval and Effect."""

    def charge(value: RefundChargeFacts) -> dict[str, object]:
        return {
            "amount": canonical_decimal_string(value.amount),
            "billing_record_id": value.billing_record_id,
            "charged_at": format_canonical_utc_timestamp(_aware_utc(value.charged_at)),
            "currency": value.currency,
            "customer_id": value.customer_id,
            "service_period_end": value.service_period_end.isoformat(),
            "service_period_start": value.service_period_start.isoformat(),
            "status": value.status,
            "tenant_id": value.tenant_id,
            "version": value.version,
        }

    return {
        "original": charge(original),
        "policy_version": "refund-pair.v1",
        "target": {**charge(target), "duplicate_of": target.duplicate_of},
        "window_days": REFUND_APPLICATION_WINDOW_DAYS,
    }


def refund_pair_checks_payload(checks: RefundPairChecks) -> dict[str, bool]:
    return {
        "same_scope": checks.same_scope,
        "explicit_relation": checks.explicit_relation,
        "both_charged": checks.both_charged,
        "same_amount": checks.same_amount,
        "same_currency": checks.same_currency,
        "same_service_period": checks.same_service_period,
        "within_application_window": checks.within_application_window,
    }


def evaluate_refund_pair(
    target: RefundChargeFacts,
    original: RefundChargeFacts | None,
    *,
    now: datetime,
) -> RefundPairEvaluation:
    """Evaluate the documented duplicate-charge policy without side effects."""

    current = _aware_utc(now)
    target_charged_at = _aware_utc(target.charged_at)
    original_charged_at = _aware_utc(original.charged_at) if original is not None else None
    checks = RefundPairChecks(
        same_scope=bool(
            original is not None
            and target.tenant_id == original.tenant_id
            and target.customer_id == original.customer_id
        ),
        explicit_relation=bool(
            original is not None
            and target.billing_record_id != original.billing_record_id
            and target.duplicate_of == original.billing_record_id
        ),
        both_charged=bool(
            original is not None and target.status == "charged" and original.status == "charged"
        ),
        same_amount=bool(original is not None and target.amount == original.amount),
        same_currency=bool(original is not None and target.currency == original.currency),
        same_service_period=bool(
            original is not None
            and target.service_period_start == original.service_period_start
            and target.service_period_end == original.service_period_end
            and target.service_period_start < target.service_period_end
        ),
        within_application_window=bool(
            original_charged_at is not None
            and original_charged_at <= current
            and target_charged_at <= current
            and current - target_charged_at <= timedelta(days=REFUND_APPLICATION_WINDOW_DAYS)
        ),
    )
    payload = refund_pair_payload(target, original) if original is not None else None
    return RefundPairEvaluation(
        target=target,
        original=original,
        checks=checks,
        pair_hash=canonical_json_hash(payload) if payload is not None else None,
    )
