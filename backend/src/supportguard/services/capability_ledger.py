from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.capability_decisions import (
    CausalDecisionV2,
    EscalationCausalDecisionV2,
    ProposalCausalDecisionV2,
)
from supportguard.db.models import (
    PolicyCapabilityAttempt,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
)
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository


def _hash(value: object) -> str:
    return canonical_json_hash(value)


def capability_payload_hash(value: object) -> str:
    """Canonical hash shared by the Action MCP receipt and Worker ledger."""

    return _hash(value)


@dataclass(frozen=True, slots=True)
class ReservedCapability:
    id: str
    capability_name: str
    sequence: int
    causal_decision_hash: str
    causal_decision: CausalDecisionV2
    observation_binding_hash: str
    effect_identity: str
    attempt_id: str
    attempt_ordinal: int


class PolicyCapabilityLedger:
    """Durable deterministic-policy ledger, separate from the model tool budget."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(
        self,
        lease: JobLease,
        *,
        segment_id: str,
        capability_name: str,
        causal_decision: CausalDecisionV2,
        observation_binding: object,
    ) -> ReservedCapability:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        if not isinstance(causal_decision, (ProposalCausalDecisionV2, EscalationCausalDecisionV2)):
            raise RuntimeConflict("policy_capability_typed_decision_required")
        if causal_decision.capability_name != capability_name:
            raise RuntimeConflict("policy_capability_decision_name_mismatch")
        decision_hash = _hash(causal_decision.model_dump(mode="python"))
        binding_hash = _hash(observation_binding)
        if causal_decision.observation_binding_hash != binding_hash:
            raise RuntimeConflict("policy_capability_observation_binding_mismatch")
        effect_identity = _hash(
            {
                "tenant_id": lease.tenant_id,
                "run_id": lease.run_id,
                "segment_id": segment_id,
                "capability_name": capability_name,
                "causal_decision_hash": decision_hash,
                "observation_binding_hash": binding_hash,
            }
        )
        existing = await self.session.scalar(
            select(PolicyCapabilityInvocation).where(
                PolicyCapabilityInvocation.tenant_id == lease.tenant_id,
                PolicyCapabilityInvocation.effect_identity == effect_identity,
            )
        )
        if existing is not None:
            raise RuntimeConflict("policy_capability_already_reserved")
        current = await self.session.scalar(
            select(func.max(PolicyCapabilityInvocation.sequence)).where(
                PolicyCapabilityInvocation.run_id == lease.run_id,
                PolicyCapabilityInvocation.segment_id == segment_id,
            )
        )
        row = PolicyCapabilityInvocation(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=segment_id,
            fencing_token=lease.fencing_token,
            capability_name=capability_name,
            sequence=int(current or 0) + 1,
            causal_decision_hash=decision_hash,
            observation_binding_hash=binding_hash,
            effect_identity=effect_identity,
            status="reserved",
        )
        self.session.add(row)
        await self.session.flush()
        attempt = PolicyCapabilityAttempt(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            invocation_id=row.id,
            fencing_token=lease.fencing_token,
            ordinal=1,
            status="reserved",
        )
        self.session.add(attempt)
        await self.session.flush()
        return ReservedCapability(
            row.id,
            row.capability_name,
            row.sequence,
            row.causal_decision_hash,
            causal_decision,
            row.observation_binding_hash,
            row.effect_identity,
            attempt.id,
            attempt.ordinal,
        )

    async def finish(
        self,
        lease: JobLease,
        reserved: ReservedCapability,
        *,
        status: str,
        error_code: str | None = None,
        payload: dict[str, object] | None = None,
        reconciled: bool = False,
    ) -> PolicyCapabilityResult | None:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        row = await self.session.get(PolicyCapabilityInvocation, reserved.id, with_for_update=True)
        if (
            row is None
            or row.status not in {"reserved", "executing"}
            or row.fencing_token != lease.fencing_token
        ):
            raise RuntimeConflict("policy_capability_not_active")
        if status not in {"succeeded", "failed", "denied", "unknown"}:
            raise ValueError("invalid policy capability outcome")
        attempt = await self.session.get(
            PolicyCapabilityAttempt, reserved.attempt_id, with_for_update=True
        )
        if attempt is None or attempt.status not in {"reserved", "executing"}:
            raise RuntimeConflict("policy_capability_attempt_not_active")
        existing_result = await self.session.scalar(
            select(PolicyCapabilityResult).where(
                PolicyCapabilityResult.invocation_id == reserved.id
            )
        )
        result_payload = payload or {"error_code": error_code}
        result_hash = _hash(result_payload)
        if existing_result is not None:
            if existing_result.effect_identity != reserved.effect_identity:
                raise RuntimeConflict("policy_capability_result_conflict")
            # The Action MCP writes a succeeded result in the same transaction as
            # the business effect.  It is authoritative when the stdio response is
            # lost and the caller can only report an unknown transport outcome.
            if status != "unknown" and (
                existing_result.status != status or existing_result.payload_hash != result_hash
            ):
                raise RuntimeConflict("policy_capability_result_conflict")
            attempt.status = "succeeded" if existing_result.status == "succeeded" else "failed"
            attempt.error_code = None
            attempt.completed_at = datetime.now(UTC)
            row.status = existing_result.status
            row.error_code = None
            row.completed_at = datetime.now(UTC)
            await self.session.flush()
            return existing_result
        attempt.status = (
            "unknown"
            if status == "unknown"
            else ("succeeded" if status == "succeeded" else "failed")
        )
        attempt.error_code = error_code
        attempt.completed_at = datetime.now(UTC)
        row.status = status
        row.error_code = error_code
        row.completed_at = datetime.now(UTC)
        if status == "unknown":
            await self.session.flush()
            return None
        result = PolicyCapabilityResult(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            invocation_id=row.id,
            effect_identity=row.effect_identity,
            status=status,
            payload_hash=result_hash,
            payload=result_payload,
            reconciled_at=datetime.now(UTC) if reconciled else None,
        )
        self.session.add(result)
        await self.session.flush()
        return result

    async def reconcile_unknown(
        self,
        lease: JobLease,
        *,
        invocation_id: str,
        status: str,
        payload: dict[str, object],
    ) -> PolicyCapabilityResult:
        """Resolve an unknown transport by effect identity without re-sending it."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        if status not in {"succeeded", "failed", "denied"}:
            raise ValueError("invalid reconciled capability outcome")
        row = await self.session.get(
            PolicyCapabilityInvocation, invocation_id, with_for_update=True
        )
        if (
            row is None
            or row.run_id != lease.run_id
            or row.job_id != lease.job_id
            or row.status != "unknown"
        ):
            raise RuntimeConflict("policy_capability_not_reconcilable")
        existing = await self.session.scalar(
            select(PolicyCapabilityResult).where(PolicyCapabilityResult.invocation_id == row.id)
        )
        payload_hash = _hash(payload)
        if existing is not None:
            if existing.status != status or existing.payload_hash != payload_hash:
                raise RuntimeConflict("policy_capability_reconciliation_conflict")
            attempts = (
                await self.session.scalars(
                    select(PolicyCapabilityAttempt).where(
                        PolicyCapabilityAttempt.invocation_id == row.id
                    )
                )
            ).all()
            for attempt in attempts:
                attempt.status = "succeeded" if status == "succeeded" else "failed"
                attempt.error_code = None
                attempt.completed_at = datetime.now(UTC)
            row.status = status
            row.error_code = None
            row.completed_at = datetime.now(UTC)
            await self.session.flush()
            return existing
        result = PolicyCapabilityResult(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            invocation_id=row.id,
            effect_identity=row.effect_identity,
            status=status,
            payload_hash=payload_hash,
            payload=payload,
            reconciled_at=datetime.now(UTC),
        )
        self.session.add(result)
        attempts = (
            await self.session.scalars(
                select(PolicyCapabilityAttempt).where(
                    PolicyCapabilityAttempt.invocation_id == row.id
                )
            )
        ).all()
        for attempt in attempts:
            attempt.status = "succeeded" if status == "succeeded" else "failed"
            attempt.error_code = None
            attempt.completed_at = datetime.now(UTC)
        row.status = status
        row.error_code = None
        row.completed_at = datetime.now(UTC)
        await self.session.flush()
        return result

    async def reconcile_unknown_effect(
        self,
        lease: JobLease,
        *,
        invocation_id: str,
    ) -> PolicyCapabilityResult:
        """Converge an unknown call from its atomic effect receipt, never by resend."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        row = await self.session.get(
            PolicyCapabilityInvocation, invocation_id, with_for_update=True
        )
        if row is None or row.status != "unknown":
            raise RuntimeConflict("policy_capability_not_reconcilable")
        receipt = await self.session.scalar(
            select(PolicyCapabilityResult).where(
                PolicyCapabilityResult.invocation_id == row.id,
                PolicyCapabilityResult.effect_identity == row.effect_identity,
            )
        )
        if receipt is not None:
            return await self.reconcile_unknown(
                lease,
                invocation_id=row.id,
                status=receipt.status,
                payload=dict(receipt.payload),
            )
        return await self.reconcile_unknown(
            lease,
            invocation_id=row.id,
            status="failed",
            payload={
                "error_code": "capability_effect_unprovable",
                # The Action MCP commits the business effect and this receipt
                # atomically. Absence of the canonical receipt therefore
                # proves that no effect committed; it is a failure, not an
                # unknown effect or a human-queue transition.
                "effect_status": "not_applied",
                "resolution": "failed",
                "effect_identity": row.effect_identity,
            },
        )

    async def reconcile_stale_active_effect(
        self,
        lease: JobLease,
        *,
        invocation_id: str,
    ) -> PolicyCapabilityResult:
        """Fence-take over an abandoned capability using only its durable receipt."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        row = await self.session.get(
            PolicyCapabilityInvocation, invocation_id, with_for_update=True
        )
        if (
            row is None
            or row.tenant_id != lease.tenant_id
            or row.run_id != lease.run_id
            or row.job_id != lease.job_id
            or row.fencing_token >= lease.fencing_token
            or row.status not in {"reserved", "executing", "unknown"}
        ):
            raise RuntimeConflict("policy_capability_stale_takeover_not_allowed")
        result = await self.session.scalar(
            select(PolicyCapabilityResult).where(
                PolicyCapabilityResult.invocation_id == row.id,
                PolicyCapabilityResult.effect_identity == row.effect_identity,
            )
        )
        if result is None:
            payload: dict[str, object] = {
                "error_code": "capability_effect_unprovable",
                "effect_status": "not_applied",
                "resolution": "failed",
                "effect_identity": row.effect_identity,
            }
            result = PolicyCapabilityResult(
                tenant_id=row.tenant_id,
                run_id=row.run_id,
                job_id=row.job_id,
                invocation_id=row.id,
                effect_identity=row.effect_identity,
                status="failed",
                payload_hash=_hash(payload),
                payload=payload,
                reconciled_at=datetime.now(UTC),
            )
            self.session.add(result)
        attempts = (
            await self.session.scalars(
                select(PolicyCapabilityAttempt)
                .where(PolicyCapabilityAttempt.invocation_id == row.id)
                .with_for_update()
            )
        ).all()
        for attempt in attempts:
            attempt.status = "succeeded" if result.status == "succeeded" else "failed"
            attempt.error_code = None
            attempt.completed_at = datetime.now(UTC)
        row.status = result.status
        row.error_code = None
        row.completed_at = datetime.now(UTC)
        await self.session.flush()
        return result
