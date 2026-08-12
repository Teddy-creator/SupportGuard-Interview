"""Frozen v1.2.6 application-role capability denominator.

This module is deliberately data-only.  Migrations, runtime adapters and the
PostgreSQL catalog gate import the same manifest so a role cannot gain a new
capability merely because one layer forgot to update its private allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionGrant:
    signature: str
    roles: frozenset[str]


@dataclass(frozen=True)
class OwnerOnlyFunction:
    signature: str
    identity_arguments: str
    owner: str
    volatility: str
    strict: bool
    parallel: str
    security_definer: bool
    search_path: tuple[str, ...]
    definition_sha256: str


OWNER_ONLY_FUNCTIONS = (
    OwnerOnlyFunction(
        signature="supportguard_internal_format_utc_timestamp(timestamptz)",
        identity_arguments="p_value timestamp with time zone",
        owner="supportguard_owner",
        volatility="s",
        strict=True,
        parallel="s",
        security_definer=False,
        search_path=("search_path=pg_catalog",),
        definition_sha256="c8ebbec2b907c9ebf7ea8b2a1da3ed4f75155425396e12b7eae323b4835b8989",
    ),
)

# v1.5.12 semantic kernels are intentionally absent from FUNCTION_GRANTS:
# runtime roles reach them only through owner-owned triggers and their existing
# narrow SECURITY DEFINER wrappers.
V1512_OWNER_ONLY_FUNCTIONS = frozenset(
    {
        "supportguard_internal_validate_approval_transition(text,text,text,bigint,bigint)",
        "supportguard_internal_allocate_dispatch_sequence(text,text)",
        "supportguard_internal_activate_next_ticket_turn(text,text,text)",
        "supportguard_internal_converge_ticket_status(text,text,text)",
        "supportguard_internal_publish_runtime_failure(text,text,text,text,text,text)",
        "supportguard_internal_publish_verified_zero_effect_event(text,text,text,text,text)",
        "supportguard_internal_converge_dead_job(text,text)",
        "supportguard_internal_classify_action_effect(text,text)",
    }
)


MCP_OWNER_ONLY_HELPERS = (
    OwnerOnlyFunction(
        signature="supportguard_action_mcp_execute(text,jsonb,jsonb)",
        identity_arguments=(
            "p_capability_name text, p_model_arguments jsonb, p_trusted_context jsonb"
        ),
        owner="supportguard_owner",
        volatility="v",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public, supportguard_control",),
        definition_sha256="202942b0ed90de5f8a1b302f389696b67e2e0625d6f4532c5416a3de656fe9c8",
    ),
    OwnerOnlyFunction(
        signature=("supportguard_action_observation_bound(jsonb,jsonb,text,text,text,integer)"),
        identity_arguments=(
            "p_trusted_context jsonb, p_bindings jsonb, p_tool_name text, "
            "p_resource_field text, p_resource_id text, p_resource_version integer"
        ),
        owner="supportguard_owner",
        volatility="v",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public",),
        definition_sha256="085cd17d3819d7abfef5b86fb59f138824e4413d1f9095d8f6258e193bac7afb",
    ),
    OwnerOnlyFunction(
        signature="supportguard_canonical_jsonb(jsonb)",
        identity_arguments="p_value jsonb",
        owner="supportguard_owner",
        volatility="i",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public",),
        definition_sha256="c725534c0dd5f92cf6de90225399cc15c9439383c3f178eb8f8bf617bc0def1b",
    ),
    OwnerOnlyFunction(
        signature=(
            "supportguard_read_mcp_chunk_payload(knowledge_chunks,knowledge_documents,"
            "double precision,double precision,boolean,integer)"
        ),
        identity_arguments=(
            "c knowledge_chunks, d knowledge_documents, p_vector_distance double precision, "
            "p_keyword_score double precision, p_exact_token_match boolean, "
            "p_channel_rank integer"
        ),
        owner="supportguard_owner",
        volatility="i",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public",),
        definition_sha256="db42404b1bb5c37f43b70bb0f5844d8e32117833aa51fee95f3a0fe902c18382",
    ),
    OwnerOnlyFunction(
        signature="supportguard_read_mcp_execute(text,jsonb,jsonb)",
        identity_arguments="p_tool_name text, p_model_arguments jsonb, p_trusted_context jsonb",
        owner="supportguard_owner",
        volatility="v",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public, supportguard_control",),
        definition_sha256="4ceb7a2c6001b3a50f5e544404aee298816139e0a780887e8d5b198cb88a6d49",
    ),
    OwnerOnlyFunction(
        signature="supportguard_read_mcp_search_execute(jsonb,jsonb)",
        identity_arguments="p_model_arguments jsonb, p_trusted_context jsonb",
        owner="supportguard_owner",
        volatility="v",
        strict=False,
        parallel="u",
        security_definer=False,
        search_path=("search_path=pg_catalog, public, supportguard_control",),
        definition_sha256="ede261847485e89664b05e8cd58f4c72d389c3f2c562791779db98c2c290e1b5",
    ),
)


MCP_HELPER_CALL_GRAPH = frozenset(
    {
        (
            "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)",
            "supportguard_action_mcp_execute",
        ),
        (
            "supportguard_action_mcp_propose_api_key_revocation(jsonb,jsonb)",
            "supportguard_action_mcp_execute",
        ),
        (
            "supportguard_action_mcp_propose_entitlement_change(jsonb,jsonb)",
            "supportguard_action_mcp_execute",
        ),
        (
            "supportguard_action_mcp_propose_refund(jsonb,jsonb)",
            "supportguard_action_mcp_execute",
        ),
        (
            "supportguard_action_mcp_execute(text,jsonb,jsonb)",
            "supportguard_action_observation_bound",
        ),
        ("supportguard_action_mcp_execute(text,jsonb,jsonb)", "supportguard_canonical_jsonb"),
        (
            "supportguard_api_accept_conversation_approval_decision(text,text,jsonb)",
            "supportguard_canonical_jsonb",
        ),
        ("supportguard_read_mcp_execute(text,jsonb,jsonb)", "supportguard_canonical_jsonb"),
        (
            "supportguard_read_mcp_search_execute(jsonb,jsonb)",
            "supportguard_canonical_jsonb",
        ),
        (
            "supportguard_read_mcp_search_execute(jsonb,jsonb)",
            "supportguard_read_mcp_chunk_payload",
        ),
        (
            "supportguard_read_mcp_check_service_status(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        ("supportguard_read_mcp_query_account(jsonb,jsonb)", "supportguard_read_mcp_execute"),
        (
            "supportguard_read_mcp_query_api_key_metadata(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_query_api_usage(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_query_billing_record(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_query_incident_impact(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_query_request_trace(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_query_subscription(jsonb,jsonb)",
            "supportguard_read_mcp_execute",
        ),
        (
            "supportguard_read_mcp_search_knowledge(jsonb,jsonb)",
            "supportguard_read_mcp_search_execute",
        ),
        (
            "supportguard_worker_execute_approved_action(text,text,text,bigint)",
            "supportguard_canonical_jsonb",
        ),
    }
)


RUNTIME_ROLES = frozenset(
    {
        "supportguard_api",
        "supportguard_dispatcher",
        "supportguard_reconciler",
        "supportguard_worker",
        "supportguard_read_mcp",
        "supportguard_action_mcp",
        "supportguard_bootstrap",
        "supportguard_maintenance",
    }
)

FUNCTION_GRANTS = (
    *(
        FunctionGrant(signature, frozenset({"supportguard_api"}))
        for signature in (
            "supportguard_api_resolve_principal(text,text)",
            "supportguard_api_accept_ticket(jsonb)",
            "supportguard_api_accept_message(text,jsonb)",
            "supportguard_api_accept_conversation_approval_decision(text,text,jsonb)",
            "supportguard_api_converge_checkpoint_binding_stale(text)",
            "supportguard_api_withdraw_proposal(text,jsonb)",
            "supportguard_api_transition_conversation(text,text,jsonb)",
            "supportguard_api_customer_exists(text)",
            "supportguard_api_list_tickets(text,integer)",
            "supportguard_api_get_ticket(text,text)",
            "supportguard_api_get_run(text,text)",
            "supportguard_api_get_run_inspector(text,text,text,text,text)",
            "supportguard_api_list_ticket_events(text,text,bigint,integer)",
            "supportguard_api_list_approvals(integer)",
            "supportguard_api_get_approval(text)",
            "supportguard_api_get_approval_source(text,bigint,text,integer)",
            "supportguard_api_runtime_snapshot()",
            "supportguard_api_accept_conversation_message(text,jsonb)",
            "supportguard_api_list_conversations(text,text,text,integer)",
            "supportguard_api_get_conversation(text,text)",
            "supportguard_api_get_run_citations(text,text)",
            "supportguard_api_get_conversation_page(text,text,integer,integer)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_dispatcher"}))
        for signature in (
            "supportguard_dispatcher_claim_outbox(integer)",
            "supportguard_dispatcher_mark_published(text,text)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_reconciler"}))
        for signature in (
            "supportguard_reconciler_candidates(integer)",
            "supportguard_reconciler_prepare(text,bigint,text)",
            "supportguard_reconciler_repair(text,bigint,text,jsonb)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_worker"}))
        for signature in (
            "supportguard_worker_claim_job(text,text)",
            "supportguard_worker_heartbeat_job(text,text,bigint)",
            "supportguard_worker_finish_job(text,text,bigint,text)",
            "supportguard_worker_accept_delivery(jsonb)",
            "supportguard_worker_record_poison(jsonb)",
            "supportguard_worker_finalize(jsonb)",
            "supportguard_worker_revalidate_approver_scope(text,text,text,bigint)",
            "supportguard_worker_execute_approved_action(text,text,text,bigint)",
            "supportguard_worker_publish_conversation_message(jsonb)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_read_mcp"}))
        for signature in (
            "supportguard_read_mcp_query_account(jsonb,jsonb)",
            "supportguard_read_mcp_query_subscription(jsonb,jsonb)",
            "supportguard_read_mcp_query_api_usage(jsonb,jsonb)",
            "supportguard_read_mcp_check_service_status(jsonb,jsonb)",
            "supportguard_read_mcp_query_billing_record(jsonb,jsonb)",
            "supportguard_read_mcp_query_request_trace(jsonb,jsonb)",
            "supportguard_read_mcp_query_api_key_metadata(jsonb,jsonb)",
            "supportguard_read_mcp_query_incident_impact(jsonb,jsonb)",
            "supportguard_read_mcp_search_knowledge(jsonb,jsonb)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_action_mcp"}))
        for signature in (
            "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)",
            "supportguard_action_mcp_propose_refund(jsonb,jsonb)",
            "supportguard_action_mcp_propose_api_key_revocation(jsonb,jsonb)",
            "supportguard_action_mcp_propose_entitlement_change(jsonb,jsonb)",
        )
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_maintenance"}))
        for signature in (
            "supportguard_maintenance_plan_pg_retention()",
            "supportguard_maintenance_apply_pg_retention(text)",
            "supportguard_maintenance_trim_eligibility(text,text,text)",
            "supportguard_maintenance_authorize_trim(text,text,text,text)",
            "supportguard_maintenance_finalize_trim(text,text,text,text,text)",
            "supportguard_maintenance_abort_trim(text,text)",
            "supportguard_maintenance_retention_report(text)",
        )
    ),
    FunctionGrant(
        "supportguard_record_service_heartbeat(text,text,text)",
        frozenset(
            {
                "supportguard_api",
                "supportguard_dispatcher",
                "supportguard_reconciler",
                "supportguard_worker",
                "supportguard_read_mcp",
                "supportguard_action_mcp",
            }
        ),
    ),
    FunctionGrant(
        "supportguard_runtime_acquire_writer_barrier(jsonb)",
        frozenset(
            {
                "supportguard_dispatcher",
                "supportguard_worker",
                "supportguard_reconciler",
                "supportguard_maintenance",
            }
        ),
    ),
    FunctionGrant(
        "supportguard_runtime_release_writer_barrier(text)",
        frozenset(
            {
                "supportguard_dispatcher",
                "supportguard_worker",
                "supportguard_reconciler",
                "supportguard_maintenance",
            }
        ),
    ),
    FunctionGrant(
        "supportguard_runtime_bind_drain(jsonb)",
        frozenset({"supportguard_worker", "supportguard_reconciler"}),
    ),
    *(
        FunctionGrant(signature, frozenset({"supportguard_migrator"}))
        for signature in (
            "supportguard_migrator_upgrade_transition(jsonb)",
            "supportguard_migrator_write_attestation(jsonb)",
        )
    ),
)

# Historical v1.2.6 exposed this compatibility entry point.  Keep it inside
# FUNCTION_GRANTS so that the frozen historical denominator remains auditable,
# while expected_function_grants() removes the explicit retirement from the
# current live capability manifest.
V126_RETIRED_FUNCTION_GRANTS = (
    FunctionGrant(
        "supportguard_api_accept_message(text,jsonb)",
        frozenset({"supportguard_api"}),
    ),
)

# Interview Edition Phase 4 retires live escalation creation from Action MCP.
# Keep the signature in FUNCTION_GRANTS as an immutable historical fact; only
# the effective current manifest removes it.
INTERVIEW_RETIRED_FUNCTION_GRANTS = (
    FunctionGrant(
        "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)",
        frozenset({"supportguard_action_mcp"}),
    ),
)

TRIGGER_ONLY_FUNCTIONS = frozenset(
    {
        "supportguard_runtime_writer_barrier_guard()",
        "supportguard_delivery_state_version_guard()",
        "supportguard_retention_trim_guard()",
        "supportguard_business_action_commit_guard()",
        "supportguard_v15_message_before_insert()",
        "supportguard_v15_message_after_insert()",
        "supportguard_v15_run_after_write()",
        "supportguard_approval_revision_binding_guard()",
        "supportguard_runtime_job_identity_guard()",
        "supportguard_approval_identity_compat_guard()",
    }
)

WORKER_SELECT_TABLES = frozenset(
    {
        "customers",
        "subscriptions",
        "api_usage_snapshots",
        "api_usage_buckets",
        "billing_records",
        "service_incidents",
        "api_request_traces",
        "api_key_metadata",
        "plan_catalog",
        "incident_impacts",
        "ticket_messages",
        "human_decisions",
        "mutation_kill_switches",
        "knowledge_ingest_runs",
        "knowledge_documents",
        "knowledge_chunks",
        "retrieval_traces",
    }
)

WORKER_MUTABLE_TABLES = frozenset(
    {
        "support_tickets",
        "agent_runs",
        "business_actions",
        "checkpoint_commit_markers",
        "agent_call_attempts",
        "tool_transport_attempts",
        "raw_provider_decision_envelopes",
        "policy_capability_invocations",
        "policy_capability_attempts",
        "turn_groups",
        "tool_invocations",
        "finalizer_payloads",
        "ticket_summaries",
        "context_ledgers",
    }
)

# Added after the frozen v1.2.6 grant migration.  Keep this separate so a clean
# migration replay cannot reference the v1.5 table before it exists.
V15_WORKER_MUTABLE_TABLES = frozenset({"conversation_turns"})
V15_WORKER_SELECT_TABLES = frozenset({"proposal_withdrawals"})

WORKER_APPEND_ONLY_TABLES = frozenset(
    {
        "agent_events",
        "audit_events",
        "provider_runtime_events",
        "policy_capability_results",
        "tool_observations",
        "claim_records",
        "approval_snapshots",
        "approval_action_revisions",
        "context_memberships",
        "citation_bindings",
    }
)

WORKER_CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)

BOOTSTRAP_TABLES = frozenset(
    {
        "tenants",
        "users",
        "memberships",
        "approver_tenant_scopes",
        "mutation_kill_switches",
        "customers",
        "subscriptions",
        "api_usage_snapshots",
        "api_usage_buckets",
        "billing_records",
        "service_incidents",
        "api_request_traces",
        "api_key_metadata",
        "plan_catalog",
        "incident_impacts",
        "knowledge_ingest_runs",
        "knowledge_documents",
        "knowledge_chunks",
    }
)

WORKER_APPROVAL_UPDATE_COLUMNS = frozenset(
    {
        "status",
        "selected_revision_id",
        "selected_revision_number",
        "decided_at",
        "consumed_at",
        "status_version",
    }
)

WORKER_PROPOSAL_UPDATE_COLUMNS = frozenset({"status", "status_version"})


def expected_worker_table_grants() -> dict[str, frozenset[str]]:
    """Return frozen whole-table grants; column UPDATE grants are separate."""

    grants: dict[str, frozenset[str]] = {
        table: frozenset({"SELECT"}) for table in WORKER_SELECT_TABLES
    }
    grants.update({table: frozenset({"SELECT"}) for table in V15_WORKER_SELECT_TABLES})
    grants.update(
        {table: frozenset({"SELECT", "INSERT", "UPDATE"}) for table in WORKER_MUTABLE_TABLES}
    )
    grants.update(
        {table: frozenset({"SELECT", "INSERT", "UPDATE"}) for table in V15_WORKER_MUTABLE_TABLES}
    )
    grants.update({table: frozenset({"SELECT", "INSERT"}) for table in WORKER_APPEND_ONLY_TABLES})
    grants.update(
        {
            table: frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
            for table in WORKER_CHECKPOINT_TABLES
        }
    )
    grants["approval_requests"] = frozenset({"SELECT", "INSERT"})
    grants["proposal_records"] = frozenset({"SELECT", "INSERT"})
    return grants


def expected_function_grants() -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    retired = {
        item.signature
        for item in (*V126_RETIRED_FUNCTION_GRANTS, *INTERVIEW_RETIRED_FUNCTION_GRANTS)
    }
    for item in FUNCTION_GRANTS:
        if item.signature in retired:
            continue
        if item.signature in result:
            raise ValueError(f"duplicate function signature: {item.signature}")
        result[item.signature] = item.roles
    return result
