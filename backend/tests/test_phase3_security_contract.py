from __future__ import annotations

from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.db.security_contract import (
    BASELINE_IDENTITY,
    CATALOG_CATEGORY_CONTRACTS,
    CRITICAL_CONSTRAINTS,
    CURRENT_INTERVIEW_DATABASE_REVISION,
    DATABASE_PREFLIGHT,
    INTERVIEW_BASELINE_ROOT_REVISION,
    INTERVIEW_TRUTHFUL_REFUND_REVISION,
    LEGACY_FINAL_DATABASE_HEAD,
    MANIFEST_ENTRYPOINTS,
    SCHEMA_EQUIVALENCE_ALLOWED_DIFFERENCES,
    SEED_IDENTITY,
    catalog_category_names,
)


def test_baseline_identity_is_independent_and_empty_only() -> None:
    assert LEGACY_FINAL_DATABASE_HEAD == "b207c0a1d001"
    assert BASELINE_IDENTITY.revision == INTERVIEW_BASELINE_ROOT_REVISION
    assert CURRENT_PRODUCT_DATABASE_HEAD == CURRENT_INTERVIEW_DATABASE_REVISION
    assert CURRENT_PRODUCT_DATABASE_HEAD == "i204_action_terminal_order"
    assert BASELINE_IDENTITY.source_legacy_head == LEGACY_FINAL_DATABASE_HEAD
    assert BASELINE_IDENTITY.script_location == "backend/alembic_baseline"
    assert BASELINE_IDENTITY.version_table == "alembic_version"
    assert DATABASE_PREFLIGHT.version_relation == "public.alembic_version"
    assert DATABASE_PREFLIGHT.legacy_final_revision == LEGACY_FINAL_DATABASE_HEAD
    assert DATABASE_PREFLIGHT.accepted_existing_revisions == (
        INTERVIEW_BASELINE_ROOT_REVISION,
        "i201_retire_escalation",
        "i202_refund_fence_authority",
        INTERVIEW_TRUTHFUL_REFUND_REVISION,
        CURRENT_INTERVIEW_DATABASE_REVISION,
    )
    assert DATABASE_PREFLIGHT.reject_legacy_history is True
    assert DATABASE_PREFLIGHT.reject_all_other_revisions is True
    assert DATABASE_PREFLIGHT.reject_unknown_user_objects is True
    assert DATABASE_PREFLIGHT.downgrade_supported is False
    assert DATABASE_PREFLIGHT.in_place_legacy_upgrade_supported is False
    assert SEED_IDENTITY.version == "interview-seed.v1"
    assert len(SEED_IDENTITY.contract_sha256) == 64


def test_catalog_contract_covers_every_v2_security_surface_once() -> None:
    names = catalog_category_names()
    assert len(names) == len(set(names)) == 18
    assert set(names) == {
        "schemas",
        "extensions",
        "relations",
        "sequences",
        "columns",
        "constraints",
        "indexes",
        "triggers",
        "functions",
        "rls",
        "policies",
        "acl",
        "default_acl",
        "roles",
        "role_memberships",
        "owners",
        "vector",
        "migration_metadata",
    }
    assert all(item.required for item in CATALOG_CATEGORY_CONTRACTS)


def test_security_manifest_has_public_owners_for_roles_rls_capabilities_and_constraints() -> None:
    names = {item.name for item in MANIFEST_ENTRYPOINTS}
    assert {
        "service_roles",
        "current_function_capabilities",
        "worker_table_capabilities",
        "tenant_reference_constraints",
        "rls_and_policies",
    } <= names
    requirements = {item.requirement for item in CRITICAL_CONSTRAINTS}
    assert requirements == {
        "one-active-approval",
        "one-leased-job-per-run",
        "one-leased-job-per-ticket",
        "single-current-outbox",
        "accepted-message-idempotency",
        "effect-once-business-action",
    }


def test_baseline_has_zero_catalog_allowances_and_closes_bootstrap_capability() -> None:
    bootstrap = "function:public.supportguard_bootstrap_transfer_ownership()"
    assert SCHEMA_EQUIVALENCE_ALLOWED_DIFFERENCES == ()
    assert bootstrap in DATABASE_PREFLIGHT.allowed_bootstrap_objects
    assert DATABASE_PREFLIGHT.required_absent_after_upgrade == (bootstrap,)
