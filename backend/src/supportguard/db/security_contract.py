"""Public Interview Edition database security and baseline contract.

The runtime owns these declarations.  Catalog collection and equivalence
reporting live in the separately installable Validation distribution so the
production image does not acquire schema-inspection tooling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from supportguard.db.seed_contract import SEED_CONTRACT_SHA256, SEED_VERSION

LEGACY_FINAL_DATABASE_HEAD: Final = "b207c0a1d001"
INTERVIEW_BASELINE_ROOT_REVISION: Final = "i200_baseline_0001"
CURRENT_INTERVIEW_DATABASE_REVISION: Final = "i201_retire_escalation"


@dataclass(frozen=True, slots=True)
class BaselineIdentity:
    revision: str
    source_legacy_head: str
    script_location: str
    version_table_schema: str
    version_table: str
    target_catalog_contract: str


@dataclass(frozen=True, slots=True)
class SeedIdentity:
    version: str
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class EmptyDatabasePreflightContract:
    version_relation: str
    legacy_final_revision: str
    accepted_existing_revisions: tuple[str, ...]
    application_schemas: tuple[str, ...]
    allowed_existing_extensions: tuple[str, ...]
    allowed_bootstrap_objects: tuple[str, ...]
    required_absent_after_upgrade: tuple[str, ...]
    required_cluster_roles: tuple[str, ...]
    reject_legacy_history: bool
    reject_all_other_revisions: bool
    reject_unknown_user_objects: bool
    downgrade_supported: bool
    in_place_legacy_upgrade_supported: bool


@dataclass(frozen=True, slots=True)
class CatalogCategoryContract:
    name: str
    required: bool
    semantic_owner: str
    notes: str


@dataclass(frozen=True, slots=True)
class ManifestEntrypoint:
    name: str
    module: str
    attribute: str


@dataclass(frozen=True, slots=True)
class CriticalConstraintContract:
    requirement: str
    category: Literal["constraint", "index"]
    schema: str
    relation: str
    catalog_name: str


@dataclass(frozen=True, slots=True)
class AllowedCatalogDifference:
    """One exact, infrastructure-only row allowed between source and target."""

    category: str
    side: Literal["legacy_only", "baseline_only"]
    identity: str
    canonical_record: Mapping[str, object]
    rationale: str


BASELINE_IDENTITY: Final = BaselineIdentity(
    revision=INTERVIEW_BASELINE_ROOT_REVISION,
    source_legacy_head=LEGACY_FINAL_DATABASE_HEAD,
    script_location="backend/alembic_baseline",
    version_table_schema="public",
    version_table="alembic_version",
    target_catalog_contract="supportguard-interview-schema.v1",
)

SEED_IDENTITY: Final = SeedIdentity(
    version=SEED_VERSION,
    contract_sha256=SEED_CONTRACT_SHA256,
)

DATABASE_PREFLIGHT: Final = EmptyDatabasePreflightContract(
    version_relation="public.alembic_version",
    legacy_final_revision=LEGACY_FINAL_DATABASE_HEAD,
    accepted_existing_revisions=(
        INTERVIEW_BASELINE_ROOT_REVISION,
        CURRENT_INTERVIEW_DATABASE_REVISION,
    ),
    application_schemas=("public", "supportguard_control"),
    allowed_existing_extensions=("plpgsql", "vector"),
    allowed_bootstrap_objects=(
        "schema:supportguard_control",
        "extension:vector",
        "function:public.supportguard_bootstrap_transfer_ownership()",
    ),
    required_absent_after_upgrade=("function:public.supportguard_bootstrap_transfer_ownership()",),
    required_cluster_roles=(
        "supportguard_owner",
        "supportguard_rls_client",
        "supportguard_migrator",
        "supportguard_api",
        "supportguard_dispatcher",
        "supportguard_reconciler",
        "supportguard_worker",
        "supportguard_read_mcp",
        "supportguard_action_mcp",
        "supportguard_bootstrap",
        "supportguard_maintenance",
    ),
    reject_legacy_history=True,
    reject_all_other_revisions=True,
    reject_unknown_user_objects=True,
    downgrade_supported=False,
    in_place_legacy_upgrade_supported=False,
)

CATALOG_CATEGORY_CONTRACTS: Final = (
    CatalogCategoryContract("schemas", True, "database", "schema owner and persistence boundary"),
    CatalogCategoryContract("extensions", True, "database", "installed extension identity/version"),
    CatalogCategoryContract("relations", True, "database", "tables, views and materialized views"),
    CatalogCategoryContract("sequences", True, "database", "sequence type and allocation settings"),
    CatalogCategoryContract(
        "columns", True, "database", "type, nullability, defaults and generation"
    ),
    CatalogCategoryContract("constraints", True, "database", "PK/FK/check/unique/exclusion"),
    CatalogCategoryContract("indexes", True, "database", "definition and readiness flags"),
    CatalogCategoryContract("triggers", True, "database", "non-internal trigger definitions"),
    CatalogCategoryContract(
        "functions", True, "database", "body, owner, proconfig/search_path and execution flags"
    ),
    CatalogCategoryContract("rls", True, "security", "row-security enable/force flags"),
    CatalogCategoryContract("policies", True, "security", "RLS role/command/expressions"),
    CatalogCategoryContract("acl", True, "security", "effective database/schema/object grants"),
    CatalogCategoryContract("default_acl", True, "security", "future-object privilege defaults"),
    CatalogCategoryContract("roles", True, "security", "non-secret role attributes"),
    CatalogCategoryContract("role_memberships", True, "security", "role inheritance graph"),
    CatalogCategoryContract("owners", True, "security", "schema/relation/function ownership"),
    CatalogCategoryContract("vector", True, "rag", "pgvector identity and vector-column contract"),
    CatalogCategoryContract(
        "migration_metadata", True, "migration", "independent Alembic identity only"
    ),
)

MANIFEST_ENTRYPOINTS: Final = (
    ManifestEntrypoint("service_roles", "supportguard.db.permissions", "SERVICE_ROLES"),
    ManifestEntrypoint("function_capabilities", "supportguard.db.role_contract", "FUNCTION_GRANTS"),
    ManifestEntrypoint(
        "current_function_capabilities",
        "supportguard.db.role_contract",
        "expected_function_grants",
    ),
    ManifestEntrypoint(
        "owner_only_functions", "supportguard.db.role_contract", "OWNER_ONLY_FUNCTIONS"
    ),
    ManifestEntrypoint(
        "mcp_owner_only_helpers", "supportguard.db.role_contract", "MCP_OWNER_ONLY_HELPERS"
    ),
    ManifestEntrypoint(
        "trigger_only_functions", "supportguard.db.role_contract", "TRIGGER_ONLY_FUNCTIONS"
    ),
    ManifestEntrypoint(
        "worker_table_capabilities",
        "supportguard.db.role_contract",
        "expected_worker_table_grants",
    ),
    ManifestEntrypoint(
        "tenant_reference_constraints",
        "supportguard.db.reference_contract",
        "V1213_FROZEN_REFERENCE_FKS",
    ),
    ManifestEntrypoint(
        "rls_and_policies",
        "supportguard.db.security_contract",
        "CATALOG_CATEGORY_CONTRACTS",
    ),
)

CRITICAL_CONSTRAINTS: Final = (
    CriticalConstraintContract(
        "one-active-approval",
        "index",
        "public",
        "approval_requests",
        "uq_approval_active_resource",
    ),
    CriticalConstraintContract(
        "one-leased-job-per-run",
        "index",
        "public",
        "runtime_jobs",
        "uq_runtime_jobs_single_leased_run",
    ),
    CriticalConstraintContract(
        "one-leased-job-per-ticket",
        "index",
        "public",
        "runtime_jobs",
        "uq_runtime_jobs_single_leased_ticket",
    ),
    CriticalConstraintContract(
        "single-current-outbox",
        "index",
        "public",
        "outbox_events",
        "uq_outbox_current_job",
    ),
    CriticalConstraintContract(
        "accepted-message-idempotency",
        "constraint",
        "public",
        "idempotency_requests",
        "uq_idempotency_request_scope",
    ),
    CriticalConstraintContract(
        "effect-once-business-action",
        "constraint",
        "public",
        "business_actions",
        "uq_business_action_effect_identity",
    ),
)


# The independent Tree/Revision reuses Alembic's canonical version-table shape.
# The revision value is data, not catalog metadata, so the b207 and Interview
# schema catalogs must be byte-for-byte equivalent after normalization.
SCHEMA_EQUIVALENCE_ALLOWED_DIFFERENCES: Final[tuple[AllowedCatalogDifference, ...]] = ()


@dataclass(frozen=True, slots=True)
class InterviewCatalogDelta:
    """One exact catalog row intentionally changed after the i200 baseline."""

    category: str
    identity: str
    change: Literal["definition", "acl"]
    rationale: str


PHASE4_ESCALATION_RETIREMENT_CATALOG_DELTA: Final = (
    InterviewCatalogDelta(
        category="function",
        identity=(
            "public.supportguard_action_mcp_create_support_escalation("
            "p_model_arguments jsonb, p_trusted_context jsonb)"
        ),
        change="acl",
        rationale="remove the retired direct escalation capability from action_mcp",
    ),
    InterviewCatalogDelta(
        category="function",
        identity=(
            "public.supportguard_action_mcp_execute("
            "p_capability_name text, p_model_arguments jsonb, p_trusted_context jsonb)"
        ),
        change="definition",
        rationale=(
            "reject escalation and unknown capability names before any business lookup or write"
        ),
    ),
)


def catalog_category_names() -> tuple[str, ...]:
    return tuple(item.name for item in CATALOG_CATEGORY_CONTRACTS)
