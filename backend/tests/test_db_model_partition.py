from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
from collections.abc import Iterable

import supportguard.db.models as model_facade
from supportguard.db import entities
from supportguard.db.base import Base

DOMAIN_CLASSES = {
    "auth": (
        "Tenant",
        "User",
        "Membership",
        "ApproverTenantScope",
    ),
    "conversation": (
        "SupportTicket",
        "TicketMessage",
        "ConversationTurn",
        "TicketSummary",
    ),
    "agent": (
        "AgentRun",
        "AgentEvent",
        "RuntimeJob",
        "CheckpointCommitMarker",
        "AgentCallAttempt",
        "ToolTransportAttempt",
        "RawProviderDecisionEnvelope",
        "PolicyCapabilityInvocation",
        "PolicyCapabilityAttempt",
        "PolicyCapabilityResult",
        "TurnGroup",
        "ToolInvocation",
        "ToolObservation",
        "ProviderRuntimeEvent",
    ),
    "evidence": (
        "Customer",
        "Subscription",
        "ApiUsageSnapshot",
        "ApiUsageBucket",
        "BillingRecord",
        "ServiceIncident",
        "ApiRequestTrace",
        "ApiKeyMetadata",
        "PlanCatalog",
        "IncidentImpact",
        "KnowledgeIngestRun",
        "KnowledgeDocument",
        "KnowledgeChunk",
        "RetrievalTrace",
        "ContextLedger",
        "ContextMembership",
        "CitationBinding",
        "ClaimRecord",
    ),
    "action": (
        "EscalationRecord",
        "ApprovalRequest",
        "ProposalWithdrawal",
        "BusinessAction",
        "FinalizerPayload",
        "ProposalRecord",
        "HumanDecision",
        "ApprovalSnapshot",
        "ApprovalActionRevision",
    ),
    "audit": (
        "RuntimeTimingSnapshot",
        "DatabaseIdentity",
        "UpgradeRun",
        "UpgradePhaseEvent",
        "UpgradeAttestation",
        "WriterBarrierReceipt",
        "MutationKillSwitch",
        "IdempotencyRequest",
        "OutboxEvent",
        "InboxDelivery",
        "ReconcileIntent",
        "RedisDeliveryObservation",
        "RetentionTrimIntent",
        "RetentionTrimReceipt",
        "QueueDeliveryAudit",
        "ServiceInstanceHeartbeat",
        "AuditEvent",
    ),
}


def _metadata_signature() -> list[dict[str, object]]:
    signature: list[dict[str, object]] = []
    for key, table in sorted(Base.metadata.tables.items()):
        signature.append(
            {
                "table": key,
                "columns": [
                    (
                        column.name,
                        str(column.type),
                        column.nullable,
                        column.primary_key,
                        column.unique,
                    )
                    for column in table.columns
                ],
                "constraints": sorted(
                    (
                        type(constraint).__name__,
                        constraint.name,
                        tuple(column.name for column in getattr(constraint, "columns", ())),
                        str(getattr(constraint, "sqltext", "")),
                    )
                    for constraint in table.constraints
                ),
                "indexes": sorted(
                    (index.name, index.unique, tuple(column.name for column in index.columns))
                    for index in table.indexes
                ),
            }
        )
    return signature


def _flatten(groups: Iterable[Iterable[str]]) -> set[str]:
    return {name for group in groups for name in group}


def test_model_facade_contains_no_mapped_class_definitions() -> None:
    tree = ast.parse(inspect.getsource(model_facade))

    assert not [node.name for node in tree.body if isinstance(node, ast.ClassDef)]


def test_each_mapped_class_has_one_domain_owner_and_stable_facade_identity() -> None:
    expected = _flatten(DOMAIN_CLASSES.values())
    mapper_names = {mapper.class_.__name__ for mapper in Base.registry.mappers}

    assert mapper_names == expected
    for domain_name, class_names in DOMAIN_CLASSES.items():
        domain = importlib.import_module(f"supportguard.db.entities.{domain_name}")
        for class_name in class_names:
            owned_class = getattr(domain, class_name)
            assert owned_class.__module__ == domain.__name__
            assert getattr(entities, class_name) is owned_class
            assert getattr(model_facade, class_name) is owned_class


def test_metadata_inventory_matches_current_schema_contract() -> None:
    payload = json.dumps(
        _metadata_signature(), sort_keys=True, separators=(",", ":"), default=str
    ).encode()

    assert len(Base.metadata.tables) == 66
    assert sum(len(table.columns) for table in Base.metadata.tables.values()) == 958
    assert sum(len(table.constraints) for table in Base.metadata.tables.values()) == 562
    assert sum(len(table.indexes) for table in Base.metadata.tables.values()) == 35
    assert hashlib.sha256(payload).hexdigest() == (
        "495d79af9ab792e4728e89c84d874d35d12d0512383e606beae5bfe0523ca888"
    )


def test_reloading_compatibility_facade_does_not_register_constraints_twice() -> None:
    before = _metadata_signature()

    reloaded = importlib.reload(model_facade)

    assert _metadata_signature() == before
    assert reloaded.Base is Base
