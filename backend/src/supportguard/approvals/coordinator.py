from __future__ import annotations

from typing import Any

from sqlalchemy import select

from supportguard.actions.service import get_action_spec_or_none
from supportguard.approvals.snapshot import approval_snapshot_hash, approval_snapshot_payload
from supportguard.contracts.context import worker_execution_context
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    ApprovalRequest,
    ApprovalSnapshot,
    HumanDecision,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
)
from supportguard.db.session import ScopedSessionFactory
from supportguard.rag.citations import CitationPublicationConflict, CitationPublicationValidator
from supportguard.services.errors import DomainError, ErrorCode
from supportguard.services.runtime_jobs import JobLease, RuntimeJobRepository


class ApprovalCoordinator:
    def __init__(
        self,
        factory: ScopedSessionFactory,
        *,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.factory = factory
        self.test_capability = test_capability

    async def handle(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        decision: dict[str, Any],
        trace_id: str,
        publication_state: dict[str, Any],
    ) -> dict[str, Any]:
        action = str(decision["action"])
        approver_id = str(decision["approver_id"])
        async with self.factory.worker(worker_execution_context.get()) as session:
            existing = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            if existing is None:
                raise DomainError(ErrorCode.APPROVAL_NOT_FOUND, "Approval was not found")
            persisted_decision = await session.scalar(
                select(HumanDecision).where(HumanDecision.approval_id == approval_id)
            )
            if persisted_decision is None:
                if self.test_capability is None or existing.status != "pending":
                    raise DomainError(
                        ErrorCode.APPROVAL_BINDING_INVALID,
                        "Approval decision identity is unavailable",
                    )
            else:
                if persisted_decision.actor_id != approver_id:
                    raise DomainError(
                        ErrorCode.APPROVAL_BINDING_INVALID,
                        "Approval decision identity changed",
                    )
            lease: JobLease | None = None
            job_id = str(decision.get("job_id", ""))
            if job_id:
                worker = worker_execution_context.get()
                if (
                    worker.job_id != job_id
                    or worker.run_id != existing.run_id
                    or worker.tenant_id != existing.tenant_id
                    or worker.fencing_token != int(decision.get("fencing_token", 0))
                ):
                    raise DomainError(
                        ErrorCode.APPROVAL_BINDING_INVALID,
                        "Approval resume has no valid fenced job",
                    )
                lease = JobLease(
                    worker.job_id,
                    worker.run_id,
                    worker.tenant_id,
                    worker.executor_service_principal,
                    worker.fencing_token,
                    worker.deadline,
                )
                lease = await RuntimeJobRepository(session).refresh_lease(lease)
            if action in {"approve", "edit_and_approve"}:
                if lease is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_BINDING_INVALID,
                        "Approval resume has no valid fenced job",
                    )
                if existing.status not in {"approved", "executed"}:
                    raise DomainError(
                        ErrorCode.APPROVAL_STATE_CONFLICT,
                        "Approval is not executable",
                        details={"status": existing.status},
                    )
                if persisted_decision is None or persisted_decision.decision != action:
                    raise DomainError(
                        ErrorCode.APPROVAL_BINDING_INVALID,
                        "Approval resume decision binding changed",
                    )
                if existing.idempotency_key != idempotency_key:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Idempotency key mismatch",
                    )
                if self.test_capability is None and await self._has_legacy_binding(
                    session, existing
                ):
                    return await self._legacy_unknown_effect_response(
                        session, existing, trace_id=trace_id
                    )
                # Production publication authority lives in the fenced
                # PostgreSQL Runtime Action capability.  A Python preflight
                # may be useful for deterministic in-memory tests, but it
                # must never short-circuit the DB-owned CAS that records a
                # stale Approval and Proposal atomically.
                if self.test_capability is not None and not await self._validate_action_publication(
                    session,
                    existing,
                    publication_state=publication_state,
                    allow_unbound=True,
                ):
                    return await self._stale_response(session, existing)
                return {
                    "approval_id": approval_id,
                    "action_type": existing.action_type,
                    "resource_id": existing.resource_id,
                    "action_hash": existing.action_hash,
                    "idempotency_key": existing.idempotency_key,
                    "status": "execution_pending",
                    "execution_intent": "execute_runtime_action",
                    "expected_approval_status": existing.status,
                }
            if action == "reject" and existing.status == "rejected":
                return {
                    "approval_id": existing.id,
                    "status": existing.status,
                    "action_hash": existing.action_hash,
                }
            if action == "manual_takeover" and existing.status == "manual_takeover":
                return {
                    "approval_id": existing.id,
                    "status": existing.status,
                    "action_hash": existing.action_hash,
                }
            raise DomainError(
                ErrorCode.APPROVAL_STATE_CONFLICT,
                "Approval decision has not converged to the resume state",
                details={"status": existing.status},
            )

    @staticmethod
    async def _validate_action_publication(
        session: Any,
        approval: ApprovalRequest,
        *,
        publication_state: dict[str, Any],
        allow_unbound: bool = False,
    ) -> bool:
        snapshot = await session.scalar(
            select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval.id)
        )
        if snapshot is None or not snapshot.citation_binding_refs:
            return bool(allow_unbound)
        snapshot_payload = approval_snapshot_payload(
            approval_id=snapshot.approval_id,
            proposal_id=snapshot.proposal_id,
            tenant_id=snapshot.tenant_id,
            run_id=snapshot.run_id,
            origin_job_id=snapshot.origin_job_id,
            origin_marker_id=snapshot.origin_marker_id,
            origin_fencing_token=snapshot.origin_fencing_token,
            action_type=snapshot.action_type,
            action_payload=dict(snapshot.action_payload),
            action_hash=snapshot.action_hash,
            resource_version=snapshot.resource_version,
            policy_binding=dict(snapshot.policy_binding),
            citation_binding_refs=list(snapshot.citation_binding_refs),
        )
        lineage_refs = {
            str(item.get("citation_binding_id"))
            for item in snapshot.policy_binding.get("citation_lineage", [])
        }
        if (
            approval_snapshot_hash(snapshot_payload) != snapshot.snapshot_hash
            or lineage_refs != set(snapshot.citation_binding_refs)
            or snapshot.run_id != approval.run_id
            or snapshot.approval_id != approval.id
            or not await ApprovalCoordinator._validate_policy_binding(session, snapshot)
        ):
            return False
        state_binding_ids = {
            str(binding_id)
            for claim in publication_state.get("final", {}).get("material_claims", [])
            for binding_id in claim.get("citation_binding_ids", [])
        }
        if state_binding_ids != set(snapshot.citation_binding_refs):
            return False
        try:
            await CitationPublicationValidator(session).validate(
                run_id=str(approval.run_id), state=publication_state
            )
        except CitationPublicationConflict:
            return False
        return True

    @staticmethod
    async def _has_legacy_binding(session: Any, approval: ApprovalRequest) -> bool:
        snapshot = await session.scalar(
            select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval.id)
        )
        return bool(
            snapshot is None
            or snapshot.policy_binding.get("schema_version") != "deterministic-policy-binding.v1"
            or not snapshot.citation_binding_refs
        )

    @staticmethod
    async def _validate_policy_binding(session: Any, snapshot: ApprovalSnapshot) -> bool:
        policy = dict(snapshot.policy_binding)
        action_spec = get_action_spec_or_none(snapshot.action_type)
        expected_capability = action_spec.policy_capability if action_spec is not None else None
        invocation_id = str(policy.get("capability_invocation_id", ""))
        if (
            policy.get("schema_version") != "deterministic-policy-binding.v1"
            or expected_capability is None
            or not invocation_id
        ):
            return False
        row = await session.execute(
            select(PolicyCapabilityInvocation, PolicyCapabilityResult)
            .join(
                PolicyCapabilityResult,
                PolicyCapabilityResult.invocation_id == PolicyCapabilityInvocation.id,
            )
            .where(PolicyCapabilityInvocation.id == invocation_id)
        )
        matches = row.all()
        if len(matches) != 1:
            return False
        invocation, result = matches[0]
        return bool(
            invocation.tenant_id == snapshot.tenant_id
            and invocation.run_id == snapshot.run_id
            and invocation.job_id == snapshot.origin_job_id
            and invocation.segment_id == snapshot.origin_marker_id
            and invocation.fencing_token == snapshot.origin_fencing_token
            and invocation.capability_name == expected_capability
            and invocation.capability_name == policy.get("capability_name")
            and invocation.causal_decision_hash == policy.get("causal_decision_hash")
            and invocation.observation_binding_hash == policy.get("observation_binding_hash")
            and invocation.effect_identity == policy.get("effect_identity")
            and invocation.status == "succeeded"
            and result.tenant_id == snapshot.tenant_id
            and result.run_id == snapshot.run_id
            and result.job_id == snapshot.origin_job_id
            and result.invocation_id == invocation.id
            and result.effect_identity == invocation.effect_identity
            and result.status == "succeeded"
            and result.payload_hash == policy.get("result_payload_hash")
            and result.payload.get("proposal_id") == snapshot.proposal_id
        )

    @staticmethod
    async def _stale_response(session: Any, approval: ApprovalRequest) -> dict[str, Any]:
        """Return a fail-closed intent; never mutate part of the aggregate here."""

        return {
            "approval_id": approval.id,
            "action_type": approval.action_type,
            "resource_id": approval.resource_id,
            "action_hash": approval.action_hash,
            "status": "execution_precondition_failed",
            "execution_state": "verification_pending",
            "effect_status": "not_attempted",
            "reused": False,
            "reason": "publication_binding_stale",
        }

    @staticmethod
    async def _legacy_unknown_effect_response(
        session: Any,
        approval: ApprovalRequest,
        *,
        trace_id: str,
    ) -> dict[str, Any]:
        """Converge legacy approved state without claiming or replaying its effect."""

        return {
            "approval_id": approval.id,
            "status": "approved",
            "execution_state": "verification_pending",
            "effect_status": "unknown",
            "reused": False,
        }
