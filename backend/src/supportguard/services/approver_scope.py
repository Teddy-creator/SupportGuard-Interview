from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import ApproverTenantScope, Membership
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict


async def assert_active_approver_scope(
    session: AsyncSession, *, tenant_id: str, actor_id: str
) -> None:
    membership = await session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == actor_id,
            Membership.role == "support_approver",
            Membership.status == "active",
        )
    )
    scope = await session.get(
        ApproverTenantScope, {"user_id": actor_id, "tenant_id": tenant_id}
    )
    if membership is None or scope is None:
        raise RuntimeConflict("approver_scope_invalid")


async def assert_execution_approver_scope(
    session: AsyncSession,
    *,
    approval_id: str,
    human_decision_id: str,
    lease: JobLease,
    tenant_id: str,
    actor_id: str,
    test_capability: TestRuntimeCapability | None = None,
) -> None:
    """Revalidate scope in the same transaction that may create a business effect."""

    if session.get_bind().dialect.name == "postgresql" and test_capability is None:
        active = await session.scalar(
            text(
                "SELECT supportguard_worker_revalidate_approver_scope("
                ":approval_id,:human_decision_id,:job_id,:fencing_token)"
            ),
            {
                "approval_id": approval_id,
                "human_decision_id": human_decision_id,
                "job_id": lease.job_id,
                "fencing_token": lease.fencing_token,
            },
        )
        if active is not True:
            raise RuntimeConflict("approver_scope_invalid")
        return
    await assert_active_approver_scope(session, tenant_id=tenant_id, actor_id=actor_id)
