from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal


@dataclass(frozen=True)
class ReadFreshnessContract:
    """Shared lifetime contract for one authoritative read observation."""

    policy: str
    freshness_class: Literal["transactional"]
    lifetime: timedelta


ACCOUNT_SUBSCRIPTION_FRESHNESS = ReadFreshnessContract(
    policy="account_subscription_15m",
    freshness_class="transactional",
    lifetime=timedelta(minutes=15),
)

CURRENT_FACT_FRESHNESS: dict[str, ReadFreshnessContract] = {
    "query_account": ACCOUNT_SUBSCRIPTION_FRESHNESS,
    "query_subscription": ACCOUNT_SUBSCRIPTION_FRESHNESS,
}


def current_fact_freshness_contract(tool_name: str) -> ReadFreshnessContract | None:
    """Return the shared Runtime/Memory contract for singleton current facts."""

    return CURRENT_FACT_FRESHNESS.get(tool_name)
