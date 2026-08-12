from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.db.models import MutationKillSwitch
from supportguard.services.runtime_jobs import RuntimeConflict


async def assert_mutation_enabled(
    session: AsyncSession, *, tenant_id: str, action_type: str
) -> MutationKillSwitch:
    """Read the authoritative switch in the action transaction; missing means closed."""

    switch = await session.scalar(
        select(MutationKillSwitch).where(
            MutationKillSwitch.tenant_id == tenant_id,
            MutationKillSwitch.action_type == action_type,
        )
    )
    if switch is None or not switch.enabled:
        raise RuntimeConflict(f"mutation_disabled:{action_type}")
    return switch
