from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.refund_policy import (
    RefundChargeFacts,
    RefundPairEvaluation,
    evaluate_refund_pair,
    refund_pair_checks_payload,
)
from supportguard.db.models import BillingRecord, ProposalRecord


def refund_charge_facts(billing: BillingRecord) -> RefundChargeFacts:
    """Project one ORM row into the policy's side-effect-free input."""

    return RefundChargeFacts(
        billing_record_id=billing.id,
        tenant_id=billing.tenant_id,
        customer_id=billing.customer_id,
        amount=billing.amount,
        currency=billing.currency,
        status=billing.status,
        charged_at=billing.charged_at,
        service_period_start=billing.service_period_start,
        service_period_end=billing.service_period_end,
        version=billing.version,
        duplicate_of=billing.duplicate_of,
    )


async def evaluate_billing_refund_pair(
    session: AsyncSession,
    billing: BillingRecord,
    *,
    now: datetime,
    lock_original: bool = False,
) -> RefundPairEvaluation:
    """Load the explicitly linked original charge and evaluate one scoped pair."""

    original: BillingRecord | None = None
    if billing.duplicate_of:
        statement = select(BillingRecord).where(
            BillingRecord.id == billing.duplicate_of,
            BillingRecord.tenant_id == billing.tenant_id,
            BillingRecord.customer_id == billing.customer_id,
        )
        if lock_original:
            statement = statement.with_for_update()
        original = await session.scalar(statement)
    return evaluate_refund_pair(
        refund_charge_facts(billing),
        refund_charge_facts(original) if original is not None else None,
        now=now,
    )


async def lock_and_evaluate_billing_refund_pair(
    session: AsyncSession,
    *,
    tenant_id: str,
    customer_id: str,
    billing_record_id: str,
    now: datetime,
) -> tuple[BillingRecord | None, RefundPairEvaluation | None]:
    """Re-read and lock an eligible pair in stable resource-id order."""

    probe = await session.scalar(
        select(BillingRecord).where(
            BillingRecord.id == billing_record_id,
            BillingRecord.tenant_id == tenant_id,
            BillingRecord.customer_id == customer_id,
        )
    )
    if probe is None:
        return None, None
    identities = sorted({probe.id, *(filter(None, (probe.duplicate_of,)))})
    rows = (
        await session.scalars(
            select(BillingRecord)
            .where(
                BillingRecord.tenant_id == tenant_id,
                BillingRecord.customer_id == customer_id,
                BillingRecord.id.in_(identities),
            )
            .order_by(BillingRecord.id)
            .with_for_update()
        )
    ).all()
    by_id = {row.id: row for row in rows}
    target = by_id.get(billing_record_id)
    if target is None:
        return None, None
    original = by_id.get(target.duplicate_of or "")
    return target, evaluate_refund_pair(
        refund_charge_facts(target),
        refund_charge_facts(original) if original is not None else None,
        now=now,
    )


def refund_pair_observation_fields(evaluation: RefundPairEvaluation) -> dict[str, object]:
    return {
        "duplicate_pair_eligible": evaluation.eligible,
        "refund_pair_hash": evaluation.pair_hash,
        "refund_pair_checks": refund_pair_checks_payload(evaluation.checks),
        "original_billing_record_id": (
            evaluation.original.billing_record_id if evaluation.original else None
        ),
        "original_amount": evaluation.original.amount if evaluation.original else None,
        "original_currency": evaluation.original.currency if evaluation.original else None,
        "original_status": evaluation.original.status if evaluation.original else None,
        "original_charged_at": (evaluation.original.charged_at if evaluation.original else None),
        "original_service_period_start": (
            evaluation.original.service_period_start if evaluation.original else None
        ),
        "original_service_period_end": (
            evaluation.original.service_period_end if evaluation.original else None
        ),
        "original_version": evaluation.original.version if evaluation.original else None,
    }


def bind_refund_pair_to_proposal(
    proposal: ProposalRecord,
    evaluation: RefundPairEvaluation,
) -> None:
    """Persist the immutable pair identity beside the generic action payload."""

    if not evaluation.eligible or evaluation.original is None or not evaluation.pair_hash:
        raise ValueError("refund_pair_is_not_eligible")
    proposal.refund_original_resource_id = evaluation.original.billing_record_id
    proposal.refund_original_version = evaluation.original.version
    proposal.refund_pair_hash = evaluation.pair_hash


def refund_pair_matches_proposal(
    evaluation: RefundPairEvaluation,
    proposal: ProposalRecord,
) -> bool:
    """Require the current pair to match the durable proposal-side binding."""

    return bool(
        evaluation.eligible
        and evaluation.original is not None
        and evaluation.pair_hash
        and proposal.refund_original_resource_id == evaluation.original.billing_record_id
        and proposal.refund_original_version == evaluation.original.version
        and proposal.refund_pair_hash == evaluation.pair_hash
    )
