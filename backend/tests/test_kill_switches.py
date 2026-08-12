import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from supportguard.db.models import MutationKillSwitch
from supportguard.services.kill_switches import assert_mutation_enabled
from supportguard.services.runtime_jobs import RuntimeConflict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_type", ["refund", "api_key_revocation", "entitlement_change"]
)
async def test_mutation_switch_is_fresh_and_fail_closed(
    db_session: AsyncSession, action_type: str
) -> None:
    await seed_business_facts(db_session)
    switch = await db_session.get(
        MutationKillSwitch,
        {"tenant_id": "tenant_demo", "action_type": action_type},
    )
    assert switch is not None
    switch.enabled = False
    await db_session.flush()
    with pytest.raises(RuntimeConflict, match=f"mutation_disabled:{action_type}"):
        await assert_mutation_enabled(
            db_session, tenant_id="tenant_demo", action_type=action_type
        )
    switch.enabled = True
    selected = await assert_mutation_enabled(
        db_session, tenant_id="tenant_demo", action_type=action_type
    )
    assert selected is switch
