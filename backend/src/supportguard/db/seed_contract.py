from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

SEED_VERSION: Final = "interview-seed.v1"
KNOWLEDGE_MANIFEST_PATH: Final = "knowledge/manifests/documents.json"
KNOWLEDGE_MANIFEST_SHA256: Final = (
    "faed4612819bddd775907486f77c8a2a955adb413d20f442e8b551f6b5147f3c"
)
KNOWLEDGE_SOURCE_BUNDLE_SHA256: Final = (
    "2eac863537c11184c1ab3c79cd00455e1f243e0ebd73535605de155a455244b0"
)

DEMO_BILLING_SERVICE_PERIOD_START: Final = date(2026, 8, 1)
DEMO_BILLING_SERVICE_PERIOD_END: Final = date(2026, 9, 1)


SEED_MANIFEST: Final[dict[str, object]] = {
    "version": SEED_VERSION,
    "clock_contract": {
        "capture": "once_per_seed_invocation",
        "precision": "minute",
        "dynamic_fields": [
            "api_usage_snapshots.observed_at",
            "api_request_traces.observed_at",
            "api_usage_buckets.bucket_start",
            "api_usage_buckets.bucket_end",
            "billing_records.charged_at",
        ],
        "semantic_hash_excludes_wall_clock": True,
        "ordinary_seed_allows_explicit_temporal_refresh_rows": True,
    },
    "tenants": {
        "tenant_demo": {"name": "Aster Labs", "status": "active"},
        "tenant_other": {"name": "Northwind AI", "status": "active"},
    },
    "principals": {
        "user_customer_demo": {
            "memberships": [["mem_demo_customer", "tenant_demo", "customer_admin"]]
        },
        "user_customer_other_demo": {
            "memberships": [["mem_other_customer", "tenant_other", "customer_admin"]]
        },
        "user_approver_demo": [
            ["mem_demo_approver", "tenant_demo", "support_approver"],
            ["mem_other_approver", "tenant_other", "support_approver"],
        ],
    },
    "demo_resources": {
        "customer": "cust_demo",
        "subscription": "sub_demo",
        "billing": ["bill_demo_original", "bill_demo_duplicate"],
        "billing_pair_policy": {
            "amount": "49.00",
            "currency": "USD",
            "status": "charged",
            "service_period": [
                DEMO_BILLING_SERVICE_PERIOD_START.isoformat(),
                DEMO_BILLING_SERVICE_PERIOD_END.isoformat(),
            ],
            "duplicate_relation": ["bill_demo_duplicate", "bill_demo_original"],
            "application_window_days": 30,
        },
        "api_key": "key_demo_leaked",
        "request_trace": "trace_demo_429",
        "service_incident": "incident_atlas_eu_resolved",
        "incident_impact": "impact_demo_429",
        "plan_catalog": "catalog_pro_eu_v1",
        "usage_snapshot": "usage_demo_current",
        "usage_bucket_count": 1440,
    },
    "cross_tenant_resources": {
        "customer": "cust_other",
        "subscription": "sub_other",
        "billing": "bill_other_001",
        "usage_bucket_count": 1440,
    },
    "action_policy": {
        "kill_switches_enabled": [
            "refund",
            "api_key_revocation",
            "entitlement_change",
        ],
        "tenant_scope": ["tenant_demo", "tenant_other"],
        "ordinary_seed_preserves_mutable_business_state": True,
        "clean_demo_state_requires_explicit_preflight": True,
    },
    "knowledge": {
        "manifest_path": KNOWLEDGE_MANIFEST_PATH,
        "manifest_sha256": KNOWLEDGE_MANIFEST_SHA256,
        "source_bundle_sha256": KNOWLEDGE_SOURCE_BUNDLE_SHA256,
        "index_identity": "bound_by_embedding_pipeline_at_ingest",
    },
}


def canonical_seed_manifest() -> bytes:
    return json.dumps(
        SEED_MANIFEST,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


SEED_CONTRACT_SHA256: Final = hashlib.sha256(canonical_seed_manifest()).hexdigest()


@dataclass(frozen=True)
class SeedReceipt:
    version: str
    contract_sha256: str
    captured_at: datetime
    row_counts: dict[str, int]
    knowledge_manifest_sha256: str
    knowledge_source_bundle_sha256: str


class SeedContractError(RuntimeError):
    """The existing demo data does not match the frozen Interview seed contract."""
