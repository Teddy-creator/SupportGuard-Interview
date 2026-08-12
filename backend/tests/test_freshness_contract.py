from datetime import UTC, datetime, timedelta

from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.contracts.freshness import current_fact_freshness_contract
from supportguard.memory.service import FRESHNESS


def test_query_subscription_runtime_and_memory_share_one_freshness_contract() -> None:
    observed_at = datetime(2026, 8, 11, tzinfo=UTC)
    contract = current_fact_freshness_contract("query_subscription")

    assert contract is not None
    assert contract.policy == "account_subscription_15m"
    assert contract.freshness_class == "transactional"
    assert contract.lifetime == timedelta(minutes=15)
    assert FRESHNESS["query_subscription"] == (contract.policy, contract.lifetime)
    assert AgentRuntimeServices._freshness_metadata("query_subscription", {}, observed_at) == (
        contract.freshness_class,
        "fresh",
        int(contract.lifetime.total_seconds()),
        observed_at,
    )
