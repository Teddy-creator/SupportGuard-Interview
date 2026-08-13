from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_business_facts
from current_predicate_facts import record_predicate_operands
from supportguard.agent.persistence import GENESIS_EVENT_HASH
from supportguard.config import Settings
from supportguard.db.base import Base
from supportguard.db.models import AgentEvent, AuditEvent
from supportguard.providers.cassette import replay_native_tool_cassette
from supportguard.rag.reranking import CrossEncoderConfig, CrossEncoderReranker
from supportguard.runtime.worker import finalizer_state
from supportguard.services.retention import (
    PROTECTED,
    RETENTION_CLASSES,
    RETENTION_MANIFEST,
    RetentionService,
    validate_retention_schema,
)


def test_cross_encoder_contract_is_default_off_without_model_execution() -> None:
    adapter = CrossEncoderReranker()
    assert adapter.config == CrossEncoderConfig()
    assert adapter.trace([]).model_dump() == {
        "trace_schema": "cross-encoder-trace.v1",
        "enabled": False,
        "executed": False,
        "candidate_count": 0,
        "reason": "disabled_pending_valid_dev",
    }
    with pytest.raises(RuntimeError, match="disabled_pending_valid_dev"):
        adapter.rerank("query", [])
    record_predicate_operands(
        requirement_id="C5-P0-14",
        predicate_id="reranker_disabled_recorded",
        subject_kind="reranker_default_off_contract",
        operands={
            "enabled": adapter.trace([]).enabled,
            "executed": adapter.trace([]).executed,
            "candidate_count": adapter.trace([]).candidate_count,
            "reason": adapter.trace([]).reason,
        },
    )


def test_retention_manifest_has_exact_schema_coverage_and_defaults_deny() -> None:
    external_checkpoint_tables = {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_task_identities",
        "checkpoint_thread_identities",
        "checkpoint_value_identities",
        "checkpoint_writes",
        "checkpoints",
    }
    validate_retention_schema(set(Base.metadata.tables) | external_checkpoint_tables)
    with pytest.raises(RuntimeError, match="unclassified_table"):
        validate_retention_schema(
            set(Base.metadata.tables) | external_checkpoint_tables | {"unclassified_table"}
        )
    operands = {
        "manifest_table_count": len(RETENTION_MANIFEST),
        "metadata_table_count": len(Base.metadata.tables),
        "external_checkpoint_table_count": len(external_checkpoint_tables),
        "unclassified_table_error": "unclassified_table",
        "protected_class": PROTECTED,
        "schema_table_count": len(set(Base.metadata.tables) | external_checkpoint_tables),
        "manifest_classes": sorted(set(RETENTION_MANIFEST.values())),
        "protected_fact_classes": {
            name: RETENTION_MANIFEST[name]
            for name in (
                "agent_events",
                "audit_events",
                "human_decisions",
                "business_actions",
                "policy_capability_results",
                "finalizer_payloads",
            )
        },
    }
    record_predicate_operands(
        requirement_id="C6-P0-10",
        predicate_id="retention_manifest_behavior_complete",
        subject_kind="retention_manifest_contract",
        operands=operands,
    )
    for predicate_id in (
        "retention_schema_coverage_exact",
        "retention_default_deny",
    ):
        record_predicate_operands(
            requirement_id="C5-P0-16",
            predicate_id=predicate_id,
            subject_kind="retention_manifest_contract",
            operands=operands,
        )


@pytest.mark.asyncio
async def test_deepseek_native_tool_cassette_freezes_request_response_and_parser() -> None:
    path = Path(__file__).parent / "fixtures/provider/deepseek-native-tools.v3.json"
    report = await replay_native_tool_cassette(path)
    assert report["request_hash"] == (
        "9fcf57167ee9bf088daaf8682d42cb4d697a9f5abb6e34a776cb21c69ffcb8d5"
    )
    assert report["response_hash"] == (
        "f9befb4cb6c855b11e7f653fb02048922c0abfc39d88f5af5d881d2766c0a196"
    )
    assert report["tool_call_count"] == 1
    assert report["network_access"] is False


def test_finalizer_state_excludes_langgraph_interrupt_transport_object() -> None:
    opaque_interrupt = object()
    assert finalizer_state(
        {"action_result": {"proposal_id": "proposal_1"}, "__interrupt__": opaque_interrupt}
    ) == {"action_result": {"proposal_id": "proposal_1"}}


@pytest.mark.asyncio
async def test_retention_preserves_durable_agent_event_chain(db_session: AsyncSession) -> None:
    await seed_business_facts(db_session)
    event = AgentEvent(
        id="event_retained_chain",
        tenant_id="tenant_demo",
        run_id="run_demo",
        ticket_id="ticket_demo",
        customer_id="cust_demo",
        sequence=1,
        ticket_sequence=1,
        run_sequence=1,
        step_index=1,
        event_type="agent_started",
        parent_event_hash=GENESIS_EVENT_HASH,
        event_hash="a" * 64,
        correlation_id="run_demo",
        causation_id=None,
    )
    db_session.add(event)
    await db_session.flush()

    report = await RetentionService(
        db_session,
        Settings(_env_file=None, app_env="test", retention_event_days=7),
    ).run(apply=True)

    assert set(report.eligible) == set(RETENTION_CLASSES)
    assert set(report.deleted) == set(RETENTION_CLASSES)
    assert RETENTION_MANIFEST["agent_events"] == PROTECTED
    assert report.deleted["agent_events"] == 0
    assert await db_session.get(AgentEvent, event.id) is event
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "retention_run")
    )
    assert audit is not None
    assert audit.payload["preserved"] == {"agent_events": "full_ticket_hash_chain"}
    operands = {
        "eligible_classes": sorted(report.eligible),
        "deleted_classes": sorted(report.deleted),
        "retention_classes": sorted(RETENTION_CLASSES),
        "agent_event_policy": RETENTION_MANIFEST["agent_events"],
        "protected_policy": PROTECTED,
        "agent_event_delete_count": report.deleted["agent_events"],
        "persisted_event_id": (await db_session.get(AgentEvent, event.id)).id,
        "expected_event_id": event.id,
        "audit_preserved": audit.payload["preserved"],
    }
    for predicate_id in (
        "legacy_run_retention_execute_zero",
        "pg_retention_outbox_inbox_delete_zero",
        "protected_facts_auto_delete_zero",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-10",
            predicate_id=predicate_id,
            subject_kind="retention_service_contract",
            operands=operands,
        )
    record_predicate_operands(
        requirement_id="C5-P0-16",
        predicate_id="protected_facts_not_deleted",
        subject_kind="retention_service_contract",
        operands=operands,
    )
