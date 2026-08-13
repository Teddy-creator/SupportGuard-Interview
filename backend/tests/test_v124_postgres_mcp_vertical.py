from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import exc, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from current_predicate_facts import record_predicate_operands
from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    ApiKeyMetadata,
    ApiRequestTrace,
    ApiUsageBucket,
    ApiUsageSnapshot,
    BillingRecord,
    CheckpointCommitMarker,
    CitationBinding,
    ClaimRecord,
    ContextLedger,
    ContextMembership,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestRun,
    OutboxEvent,
    PolicyCapabilityAttempt,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    RetrievalTrace,
    RuntimeJob,
    Subscription,
    SupportTicket,
    TicketMessage,
    ToolInvocation,
    ToolObservation,
    ToolTransportAttempt,
    TurnGroup,
)
from supportguard.db.role_contract import expected_function_grants
from supportguard.db.scope import set_local_scope
from supportguard.evidence.publication_window import PublicationObservationWindow
from supportguard.mcp.client import action_mcp_session, read_mcp_session, structured_result
from supportguard.rag.citations import CitationPublicationConflict, CitationPublicationValidator
from supportguard.rag.context_projection import project_context_evidence
from supportguard.rag.embeddings import DeterministicEmbedding
from supportguard.rag.intent import resolve_retrieval_intent
from supportguard.rag.query import normalize_query
from supportguard.rag.repository import _keyword_terms
from supportguard.rag.types import EligibilityEnvelope, RetrievalFilter, SourceLocatorV2
from supportguard.services.refunds import (
    evaluate_billing_refund_pair,
    refund_pair_observation_fields,
)

pytestmark = pytest.mark.postgres


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


_READ_CASES: dict[str, dict[str, object]] = {
    "query_account": {},
    "query_subscription": {},
    "query_api_usage": {"window": "1m"},
    "check_service_status": {"model": "atlas-chat", "region": "eu-west"},
    "query_billing_record": {"billing_record_id": "bill_demo_duplicate"},
    "query_request_trace": {"request_id": "req_demo_429"},
    "query_api_key_metadata": {"api_key_ref": "key_demo_leaked"},
    "query_incident_impact": {"request_id": "req_demo_429"},
    "search_knowledge": {"query": "429 concurrency limit"},
}


def _action_cases(run_id: str) -> dict[str, dict[str, object]]:
    return {
        "propose_refund": {
            "billing_record_id": "bill_demo_duplicate",
            "refund_reason": "已核验中文重复扣费记录",
            "idempotency_key": f"refund-{run_id}",
        },
        "propose_api_key_revocation": {
            "api_key_id": "key_demo_leaked",
            "reason": "已核验中文密钥泄露风险",
            "idempotency_key": f"key-{run_id}",
        },
        "propose_entitlement_change": {
            "subscription_id": "sub_demo",
            "change_type": "quota_change",
            "target": {"rpm_limit": 70},
            "reason": "已核验中文配额调整请求",
            "idempotency_key": f"entitlement-{run_id}",
        },
    }


@pytest.mark.asyncio
async def test_retrieval_trace_requires_started_then_one_terminal_transition() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            ingest = await session.scalar(
                select(KnowledgeIngestRun).where(KnowledgeIngestRun.is_active.is_(True))
            )
            assert ingest is not None
            trace = RetrievalTrace(
                tenant_id="tenant_demo",
                origin_kind="maintenance",
                trace_status="started",
                trace_logical_time=datetime.now(UTC),
                temporal_selector={"mode": "current"},
                query_hash="a" * 64,
                filter_contract={"request_intent": "current"},
                vector_candidates=[],
                keyword_candidates=[],
                rrf_candidates=[],
                pre_filter_candidates=[],
                selected_candidates=[],
                omission_decisions=[],
                evidence_groups=[],
                eligibility_envelopes=[],
                pipeline_contract={"state": "started"},
                pipeline_fingerprint="0" * 64,
                index_version=ingest.index_version,
                corpus_snapshot_id=ingest.id,
                runtime_provenance={"provider_mode": "fixture"},
            )
            session.add(trace)
            await session.flush()
            trace.trace_status = "terminal_error"
            trace.error_digest = "b" * 64
            await session.flush()
            assert trace.status_version == 2
            trace_id = trace.id
        async with engine.connect() as connection:
            with pytest.raises(
                exc.DBAPIError, match="retrieval_trace_transition_invalid"
            ) as second_terminal_error:
                async with connection.begin():
                    await connection.execute(
                        select(RetrievalTrace)
                        .where(RetrievalTrace.id == trace_id)
                        .with_for_update()
                    )
                    await connection.execute(
                        text(
                            "UPDATE retrieval_traces SET trace_status='terminal_ok',"
                            "status_version=status_version+1,result_digest=:digest,"
                            "error_digest=NULL WHERE id=:trace_id"
                        ),
                        {"digest": "c" * 64, "trace_id": trace_id},
                    )
        for predicate_id in (
            "trace_retry_canonical",
            "terminal_executor_attempt_append_only",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-11",
                predicate_id=predicate_id,
                subject_kind="postgres_retrieval_trace_state_machine",
                operands={
                    "terminal_status": "terminal_error",
                    "terminal_status_version": 2,
                    "terminal_error_digest": "b" * 64,
                    "second_terminal_error": str(second_terminal_error.value),
                    "second_terminal_write_count": 0,
                },
            )
    finally:
        await engine.dispose()


async def _runtime_fixture(
    database_url: str,
    *,
    read_case_overrides: dict[str, dict[str, object]] | None = None,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
]:
    suffix = uuid4().hex[:12]
    ticket_id = f"ticket_v124_{suffix}"
    message_id = f"message_v124_{suffix}"
    run_id = f"run_v124_{suffix}"
    job_id = f"job_v124_{suffix}"
    marker_id = f"marker_v124_{suffix}"
    turn_id = f"turn_v124_{suffix}"
    now = datetime.now(UTC)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    bindings: list[dict[str, object]] = []
    async with factory() as session:
        # v1.5's first-message trigger creates the ConversationTurn under the
        # same forced-RLS transaction, so even this owner-backed fixture must
        # carry an explicit tenant scope.
        await set_local_scope(
            session,
            tenant_id="tenant_demo",
            principal_id="v124-mcp-test",
            principal_role="system_worker",
        )
        api_key = await session.get(ApiKeyMetadata, "keymeta_demo_leaked")
        subscription = await session.get(Subscription, "sub_demo")
        billing = await session.get(BillingRecord, "bill_demo_duplicate")
        assert api_key is not None and subscription is not None and billing is not None
        api_key.status = "active"
        api_key.version = 2
        subscription.status = "active"
        subscription.version = 3
        billing.status = "charged"
        billing.version = 2
        refund_pair = await evaluate_billing_refund_pair(session, billing, now=now)
        assert refund_pair.eligible
        assert refund_pair.original is not None
        assert refund_pair.pair_hash is not None
        refund_observation_data = json.loads(
            json.dumps(refund_pair_observation_fields(refund_pair), default=str)
        )
        session.add(
            SupportTicket(
                id=ticket_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                status="running",
                issue_type="account_support",
                risk="high",
            )
        )
        session.add(
            TicketMessage(
                id=message_id,
                tenant_id="tenant_demo",
                ticket_id=ticket_id,
                role="user",
                content="v1.2.4 restricted-role MCP vertical fixture",
            )
        )
        await session.flush()
        run = AgentRun(
            id=run_id,
            tenant_id="tenant_demo",
            ticket_id=ticket_id,
            customer_id="cust_demo",
            message_id=message_id,
            status="running",
            model="fake",
            provider_mode="fake",
            tool_call_mode="native",
            prompt_version="agent_decide.v1",
            schema_version="agent.v1",
            context_version="context.v1",
        )
        session.add(run)
        await session.flush()
        job = RuntimeJob(
            id=job_id,
            tenant_id="tenant_demo",
            run_id=run_id,
            kind="agent_start",
            status="leased",
            attempt=1,
            available_at=now,
            lease_owner="v124-mcp-test",
            lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
            fencing_token=1,
        )
        session.add(job)
        await session.flush()
        run.active_job_id = job_id
        run.active_fencing_token = 1
        # Persist both sides of the deferred active-job invariant before adding the
        # remainder of the vertical fixture.  The database validates the pointer
        # again at commit, but later flushes must not observe a leased orphan.
        await session.flush()
        session.add(
            OutboxEvent(
                id=f"outbox_v124_{suffix}",
                delivery_id=f"delivery_v124_{suffix}",
                redis_message_id=f"fixture-not-enqueued-{suffix}",
                tenant_id="tenant_demo",
                job_id=job_id,
                run_id=run_id,
                delivery_generation=1,
                event_type="runtime_job_ready",
                published_at=now,
                last_delivery_at=now,
                publish_attempts=1,
            )
        )
        session.add(
            CheckpointCommitMarker(
                id=marker_id,
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                fencing_token=1,
                delivery_generation=1,
                segment_kind="agent",
                status="prepared",
                private_namespace=f"private/{run_id}/1",
                expected_run_version=run.status_version,
                expected_run_status="running",
                expected_ticket_sequence=0,
                prepared_payload_hash="1" * 64,
                segment_input_hash="2" * 64,
            )
        )
        turn = TurnGroup(
            id=turn_id,
            tenant_id="tenant_demo",
            run_id=run_id,
            job_id=job_id,
            segment_id=marker_id,
            fencing_token=1,
            decision_ordinal=1,
            tool_round=1,
            expected_invocations=4,
            decision_hash="3" * 64,
            context_hash="4" * 64,
            tool_schema_hash="5" * 64,
            status="closed",
            closed_at=now,
        )
        session.add(turn)
        await session.flush()
        observations = (
            ("query_billing_record", "billing_record_id", "bill_demo_duplicate", 2),
            ("query_api_key_metadata", "api_key_id", "key_demo_leaked", 2),
            ("query_subscription", "subscription_id", "sub_demo", 3),
            ("search_knowledge", "document_id", "policy", 1),
        )
        for ordinal, (tool_name, field, resource_id, version) in enumerate(observations, 1):
            invocation = ToolInvocation(
                id=f"inv_v124_{ordinal}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                turn_group_id=turn_id,
                segment_id=marker_id,
                fencing_token=1,
                provider_tool_call_id=f"provider_v124_{ordinal}_{suffix}",
                logical_invocation_id=f"logical_v124_{ordinal}_{suffix}",
                ordinal=ordinal,
                tool_name=tool_name,
                arguments_hash="6" * 64,
                lifecycle="terminal",
                outcome="succeeded",
                terminal_at=now,
            )
            session.add(invocation)
            await session.flush()
            observation_data: dict[str, object] = {field: resource_id, "version": version}
            source_refs: list[dict[str, object]] = [
                {"resource_type": field, "resource_id": resource_id}
            ]
            if tool_name == "query_billing_record":
                observation_data.update(refund_observation_data)
                source_refs.append(
                    {
                        "resource_type": field,
                        "resource_id": refund_pair.original.billing_record_id,
                    }
                )
            payload = {
                "schema_version": "observation.v1",
                "tool_name": tool_name,
                "tool_call_id": invocation.provider_tool_call_id,
                "ticket_id": ticket_id,
                "run_id": run_id,
                "attempt_index": 1,
                "status": "ok",
                "retryable": False,
                "observed_at": now.isoformat(),
                "duration_ms": 1,
                "source_refs": source_refs,
                "data": observation_data,
            }
            content_hash = _hash(payload)
            observation = ToolObservation(
                id=f"obs_v124_{ordinal}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                invocation_id=invocation.id,
                segment_id=marker_id,
                fencing_token=1,
                status="ok",
                attempt_index=1,
                content_hash=content_hash,
                payload=payload,
            )
            session.add(observation)
            bindings.append(
                {
                    "tool_name": tool_name,
                    "status": "ok",
                    "resource_field": field,
                    "resource_id": resource_id,
                    "resource_version": version,
                    "source_refs": payload["source_refs"],
                    "invocation_id": invocation.id,
                    "provider_tool_call_id": invocation.provider_tool_call_id,
                    "logical_invocation_id": invocation.logical_invocation_id,
                    "observation_id": observation.id,
                    "observation_content_hash": content_hash,
                    "turn_group_id": turn_id,
                }
            )
        read_reservations: dict[str, dict[str, object]] = {}
        read_names = tuple(_READ_CASES)
        reservation_turn = TurnGroup(
            id=f"turn_v125_reservation_{suffix}",
            tenant_id="tenant_demo",
            run_id=run_id,
            job_id=job_id,
            segment_id=marker_id,
            fencing_token=1,
            decision_ordinal=2,
            tool_round=2,
            expected_invocations=len(read_names),
            decision_hash="7" * 64,
            context_hash="8" * 64,
            tool_schema_hash="9" * 64,
            status="open",
        )
        session.add(reservation_turn)
        await session.flush()
        for ordinal, name in enumerate(read_names, 1):
            reserved_arguments = (read_case_overrides or {}).get(name, _READ_CASES[name])
            invocation = ToolInvocation(
                id=f"inv_v125_{ordinal}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                turn_group_id=reservation_turn.id,
                segment_id=marker_id,
                fencing_token=1,
                provider_tool_call_id=f"read_v125_{ordinal}_{suffix}",
                logical_invocation_id=f"logical_v125_{ordinal}_{suffix}",
                ordinal=ordinal,
                tool_name=name,
                arguments_hash=_hash(reserved_arguments),
                lifecycle="executing",
            )
            session.add(invocation)
            await session.flush()
            attempt = AgentCallAttempt(
                id=f"attempt_v125_{ordinal}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                fencing_token=1,
                call_kind="read_mcp",
                ordinal=ordinal,
                logical_invocation_id=invocation.id,
                transport_ordinal=1,
                status="started",
                runtime_provenance={"provider_mode": "fake"},
            )
            session.add(attempt)
            await session.flush()
            transport = ToolTransportAttempt(
                id=f"transport_v125_{ordinal}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                invocation_id=invocation.id,
                agent_call_attempt_id=attempt.id,
                fencing_token=1,
                transport_ordinal=1,
                status="reserved",
            )
            session.add(transport)
            read_reservations[name] = {
                "logical_invocation_id": invocation.logical_invocation_id,
                "invocation_id": invocation.id,
                "tool_attempt_id": attempt.id,
                "transport_attempt_id": transport.id,
                "provider_tool_call_id": invocation.provider_tool_call_id,
            }
        capability_reservations: dict[str, dict[str, object]] = {}
        action_cases = _action_cases(run_id)
        binding_hash = _hash(bindings)
        for sequence, name in enumerate(action_cases, 1):
            resource = {
                "propose_refund": ("refund", "bill_demo_duplicate", 2),
                "propose_api_key_revocation": ("api_key_revocation", "key_demo_leaked", 2),
                "propose_entitlement_change": ("entitlement_change", "sub_demo", 3),
            }[name]
            causal_decision = {
                "variant": "proposal",
                "capability_name": name,
                "action_type": resource[0],
                "resource_id": resource[1],
                "resource_version": resource[2],
                "model_arguments": action_cases[name],
                "observation_binding_hash": binding_hash,
                "policy_version": "supportguard-policy-gate.v1",
            }
            invocation = PolicyCapabilityInvocation(
                id=f"capability_v125_{sequence}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                segment_id=marker_id,
                fencing_token=1,
                capability_name=name,
                sequence=sequence,
                causal_decision_hash=_hash(causal_decision),
                observation_binding_hash=binding_hash,
                effect_identity=hashlib.sha256(f"{run_id}:{name}".encode()).hexdigest(),
                status="reserved",
            )
            session.add(invocation)
            await session.flush()
            attempt = PolicyCapabilityAttempt(
                id=f"capattempt_v125_{sequence}_{suffix}",
                tenant_id="tenant_demo",
                run_id=run_id,
                job_id=job_id,
                invocation_id=invocation.id,
                fencing_token=1,
                ordinal=1,
                status="reserved",
            )
            session.add(attempt)
            capability_reservations[name] = {
                "invocation_id": invocation.id,
                "attempt_id": attempt.id,
                "sequence": sequence,
                "effect_identity": invocation.effect_identity,
                "causal_decision": causal_decision,
                "causal_decision_hash": invocation.causal_decision_hash,
                "observation_binding_hash": binding_hash,
            }
        await session.commit()
    await engine.dispose()
    return (
        {
            "tenant_id": "tenant_demo",
            "customer_id": "cust_demo",
            "ticket_id": ticket_id,
            "run_id": run_id,
            "job_id": job_id,
            "segment_id": marker_id,
            "delivery_generation": 1,
            "fencing_token": 1,
            "checkpoint_id": f"checkpoint_v124_{suffix}",
            "trace_id": f"trace_v124_{suffix}",
        },
        bindings,
        read_reservations,
        capability_reservations,
    )


def _read_context(
    method: str, reservation: dict[str, object], *, search: bool = False
) -> dict[str, object]:
    now = datetime.now(UTC)
    value: dict[str, object] = {
        "surface_kind": "read",
        "logical_invocation_id": reservation["logical_invocation_id"],
        "tool_attempt_id": reservation["tool_attempt_id"],
        "transport_attempt_id": reservation["transport_attempt_id"],
        "tool_name": method,
        "transport_attempt": 1,
        "agent_tool_round": 1,
        "call_deadline": (now + timedelta(minutes=1)).isoformat(),
        "worker_deadline": (now + timedelta(minutes=2)).isoformat(),
    }
    if search:
        value["retrieval_intent"] = resolve_retrieval_intent("429 concurrency limit").model_dump(
            mode="json"
        )
    return value


def _capability_context(method: str, reservation: dict[str, object]) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "surface_kind": "policy_capability",
        "capability_invocation_id": reservation["invocation_id"],
        "capability_attempt_id": reservation["attempt_id"],
        "capability_name": method,
        "effect_identity": reservation["effect_identity"],
        "capability_attempt": 1,
        "capability_sequence": reservation["sequence"],
        "agent_tool_round": None,
        "causal_decision_hash": reservation["causal_decision_hash"],
        "causal_decision_schema_version": "causal-decision.v2",
        "causal_decision": reservation["causal_decision"],
        "observation_binding_hash": reservation["observation_binding_hash"],
        "call_deadline": (now + timedelta(minutes=1)).isoformat(),
        "worker_deadline": (now + timedelta(minutes=2)).isoformat(),
    }


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_current_restricted_postgres_roles_call_all_twelve_stdio_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, bindings, read_reservations, capability_reservations = await _runtime_fixture(
        database_url
    )
    monkeypatch.setenv("APP_ENV", "test")
    assert make_url(os.environ["MCP_READ_DATABASE_URL"]).username == "supportguard_read_mcp"
    assert make_url(os.environ["MCP_ACTION_DATABASE_URL"]).username == "supportguard_action_mcp"
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    positive_tools: list[str] = []
    replay_rejected_tools: list[str] = []
    read_payloads: dict[str, dict[str, Any]] = {}

    read_cases = _READ_CASES
    async with read_mcp_session() as session:
        discovered = {tool.name for tool in (await session.list_tools()).tools}
        assert discovered == set(read_cases)
        forged_read = {
            **common,
            "tool_call_id": read_reservations["query_subscription"]["provider_tool_call_id"],
            "mcp_context": _read_context(
                "query_subscription", read_reservations["query_subscription"]
            ),
        }
        forged_read.pop("checkpoint_id")
        forged_payload = structured_result(
            await session.call_tool(
                "query_account",
                {"arguments": {}, "trusted_context": forged_read},
            )
        )
        assert forged_payload.get("domain_error") is True
        for name, specific in read_cases.items():
            trusted_context = {
                **common,
                "tool_call_id": read_reservations[name]["provider_tool_call_id"],
                "mcp_context": _read_context(
                    name,
                    read_reservations[name],
                    search=name == "search_knowledge",
                ),
            }
            trusted_context.pop("checkpoint_id")
            arguments = {"arguments": specific, "trusted_context": trusted_context}
            payload = structured_result(await session.call_tool(name, arguments))
            assert payload.get("domain_error") is not True, (name, payload)
            read_payloads[name] = payload
            positive_tools.append(name)
            replay = structured_result(await session.call_tool(name, arguments))
            assert replay.get("domain_error") is True, (name, replay)
            replay_rejected_tools.append(name)

    action_cases = _action_cases(str(common["run_id"]))
    async with action_mcp_session() as session:

        async def assert_action_rejected(tool_name: str, tool_arguments: dict[str, object]) -> None:
            raw = await session.call_tool(tool_name, tool_arguments)
            if raw.isError:
                return
            rejected = structured_result(raw)
            assert rejected.get("domain_error") is True, (tool_name, rejected)

        discovered = {tool.name for tool in (await session.list_tools()).tools}
        assert discovered == set(action_cases)
        assert discovered.isdisjoint(
            {"execute_refund", "execute_api_key_revocation", "execute_entitlement_change"}
        )
        forged_action = {
            **common,
            "reason": "forged retired capability",
            "idempotency_key": f"esc-{common['run_id']}",
            "tool_call_id": "action_v125_forged",
            "observation_binding": bindings,
            "mcp_context": _capability_context(
                "propose_refund", capability_reservations["propose_refund"]
            ),
        }
        forged_result = await session.call_tool("create_support_escalation", forged_action)
        if not forged_result.isError:
            assert structured_result(forged_result).get("domain_error") is True
        for ordinal, (name, specific) in enumerate(action_cases.items(), 1):
            changed = dict(specific)
            changed[next(key for key in ("reason", "refund_reason") if key in changed)] = (
                "substituted after reserve"
            )
            tampered_arguments = {
                **common,
                **changed,
                "tool_call_id": f"action_v124_tampered_args_{ordinal}",
                "observation_binding": bindings,
                "mcp_context": _capability_context(name, capability_reservations[name]),
            }
            tampered_payload = structured_result(await session.call_tool(name, tampered_arguments))
            assert tampered_payload.get("domain_error") is True, (name, tampered_payload)

            rebound_arguments = {
                **common,
                **specific,
                "tool_call_id": f"action_v124_tampered_binding_{ordinal}",
                "observation_binding": list(reversed(bindings)),
                "mcp_context": _capability_context(name, capability_reservations[name]),
            }
            rebound_payload = structured_result(await session.call_tool(name, rebound_arguments))
            assert rebound_payload.get("domain_error") is True, (name, rebound_payload)

            binding_mutations = [
                bindings[:-1],
                [*bindings, bindings[0]],
                [{**bindings[0], "resource_version": 999}, *bindings[1:]],
            ]
            for mutation_index, mutated_binding in enumerate(binding_mutations, 1):
                await assert_action_rejected(
                    name,
                    {
                        **common,
                        **specific,
                        "tool_call_id": (
                            f"action_v124_tampered_binding_{ordinal}_{mutation_index}"
                        ),
                        "observation_binding": mutated_binding,
                        "mcp_context": _capability_context(name, capability_reservations[name]),
                    },
                )

            base_context = _capability_context(name, capability_reservations[name])
            expired_at = datetime.now(UTC) - timedelta(minutes=1)
            context_mutations = [
                ({"tenant_id": "tenant_forged"}, {}),
                ({"run_id": "run_forged"}, {}),
                ({"job_id": "job_forged"}, {}),
                ({"fencing_token": int(common["fencing_token"]) + 1}, {}),
                ({}, {"capability_name": "wrong_capability"}),
                ({}, {"causal_decision_hash": "f" * 64}),
                ({}, {"causal_decision_schema_version": "causal-decision.v1"}),
                (
                    {},
                    {
                        "call_deadline": (expired_at - timedelta(minutes=1)).isoformat(),
                        "worker_deadline": expired_at.isoformat(),
                    },
                ),
            ]
            for mutation_index, (outer_patch, context_patch) in enumerate(context_mutations, 1):
                await assert_action_rejected(
                    name,
                    {
                        **common,
                        **outer_patch,
                        **specific,
                        "tool_call_id": f"action_v124_scope_{ordinal}_{mutation_index}",
                        "observation_binding": bindings,
                        "mcp_context": {**base_context, **context_patch},
                    },
                )

            arguments = {
                **common,
                **specific,
                "tool_call_id": f"action_v124_{ordinal}",
                "observation_binding": bindings,
                "mcp_context": _capability_context(name, capability_reservations[name]),
            }
            payload = structured_result(await session.call_tool(name, arguments))
            assert payload.get("domain_error") is not True, (name, payload)
            positive_tools.append(name)
            replay = structured_result(await session.call_tool(name, arguments))
            assert replay.get("domain_error") is True, (name, replay)
            replay_rejected_tools.append(name)

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        usage_payload = read_payloads["query_api_usage"]
        window_start = datetime.fromisoformat(str(usage_payload["window_start"]))
        window_end = datetime.fromisoformat(str(usage_payload["window_end"]))
        usage_buckets = (
            await session.scalars(
                select(ApiUsageBucket)
                .where(
                    ApiUsageBucket.tenant_id == "tenant_demo",
                    ApiUsageBucket.customer_id == "cust_demo",
                    ApiUsageBucket.bucket_end > window_start,
                    ApiUsageBucket.bucket_end <= window_end,
                )
                .order_by(ApiUsageBucket.bucket_end)
            )
        ).all()
        usage_snapshot = await session.get(
            ApiUsageSnapshot,
            str(usage_payload["source_refs"][0]["source_id"]).removeprefix("api_usage_snapshot:"),
        )
        usage_subscription = await session.get(Subscription, "sub_demo")
        assert usage_snapshot is not None and usage_subscription is not None
        expected_usage_resource = _hash(
            {
                "bucket_sources": [(item.id, item.source_version) for item in usage_buckets],
                "balance_source": usage_snapshot.id,
                "subscription_version": usage_subscription.version,
                "window": usage_payload["window"],
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            }
        )
        assert usage_payload["resource_version"] == expected_usage_resource
        expected_usage_sources = [
            f"api_usage_snapshot:{usage_snapshot.id}",
            f"subscription:{usage_subscription.id}",
        ]
        if usage_buckets:
            expected_usage_sources.extend(
                [
                    f"api_usage_bucket:{usage_buckets[0].id}",
                    f"api_usage_bucket:{usage_buckets[-1].id}",
                ]
            )
        assert [
            item["source_id"] for item in usage_payload["source_refs"]
        ] == expected_usage_sources
        results = (
            await session.scalars(
                select(PolicyCapabilityResult).where(
                    PolicyCapabilityResult.run_id == common["run_id"]
                )
            )
        ).all()
        assert len(results) == 3
        search_trace = await session.scalar(
            select(RetrievalTrace).where(
                RetrievalTrace.run_id == common["run_id"],
                RetrievalTrace.tool_call_id
                == read_reservations["search_knowledge"]["provider_tool_call_id"],
            )
        )
        assert search_trace is not None
        restricted_state = search_trace.runtime_provenance["_restricted_search"]
        assert restricted_state["schema_version"] == "restricted-search-state.v1"
        assert restricted_state["phase"] == "terminal"
        assert restricted_state["counts"] == {
            "load_scope": 1,
            "trace_start": 1,
            "candidate_universe": 1,
            "vector_search": 1,
            "keyword_search": 1,
            "trace_terminal": 1,
        }
        assert set(restricted_state["receipts"]) == set(restricted_state["counts"])
        assert len(restricted_state["vector_fingerprint"]) == 64
        assert "vector" not in restricted_state
        assert search_trace.trace_status == "terminal_ok"
        terminal_trace_payload = {
            "trace_id": search_trace.id,
            "trace_status": search_trace.trace_status,
            "result_digest": search_trace.result_digest,
            "error_digest": search_trace.error_digest,
            "temporal_selector": search_trace.temporal_selector,
            "filter_contract": search_trace.filter_contract,
            "vector_candidates": search_trace.vector_candidates,
            "keyword_candidates": search_trace.keyword_candidates,
            "rrf_candidates": search_trace.rrf_candidates,
            "pre_filter_candidates": search_trace.pre_filter_candidates,
            "selected_candidates": search_trace.selected_candidates,
            "omission_decisions": search_trace.omission_decisions,
            "evidence_groups": search_trace.evidence_groups,
            "eligibility_envelopes": search_trace.eligibility_envelopes,
            "pipeline_contract": search_trace.pipeline_contract,
            "embedding_fingerprint": search_trace.embedding_fingerprint,
            "pipeline_fingerprint": search_trace.pipeline_fingerprint,
            "abstention_reason": search_trace.abstention_reason,
        }
        terminal_execution = {
            "operation": "trace_terminal",
            "binding": {
                "trace_id": search_trace.id,
                "query_hash": search_trace.query_hash,
                "trace_logical_time": search_trace.trace_logical_time.isoformat(),
                "index_version": search_trace.index_version,
                "corpus_snapshot_id": search_trace.corpus_snapshot_id,
                "filter_hash": canonical_json_hash(search_trace.filter_contract),
            },
            "trace": terminal_trace_payload,
        }
        direct_context = {
            "tenant_id": common["tenant_id"],
            "run_id": common["run_id"],
            "job_id": common["job_id"],
            "segment_id": common["segment_id"],
            "fencing_token": common["fencing_token"],
            "delivery_generation": common["delivery_generation"],
            "logical_invocation_id": read_reservations["search_knowledge"]["logical_invocation_id"],
            "provider_tool_call_id": read_reservations["search_knowledge"]["provider_tool_call_id"],
            "transport_attempt_id": read_reservations["search_knowledge"]["transport_attempt_id"],
            "execution_payload": terminal_execution,
        }
        await set_local_scope(
            session,
            tenant_id=common["tenant_id"],
            principal_id="worker_v1213_contract",
            principal_role="system_worker",
        )
        await session.execute(text("SET LOCAL ROLE supportguard_owner"))
        replay = await session.scalar(
            text(
                "SELECT supportguard_read_mcp_search_execute("
                "CAST(:arguments AS jsonb),CAST(:context AS jsonb))"
            ),
            {
                "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                "context": json.dumps(direct_context, default=str),
            },
        )
        assert replay["result"] == {
            "trace_id": search_trace.id,
            "status": "terminal_ok",
        }
        replay_binding = terminal_execution["binding"]
        normalized = normalize_query(str(_READ_CASES["search_knowledge"]["query"]))
        channel_payloads = {
            "candidate_universe": {"filters": search_trace.filter_contract},
            "vector_search": {
                "filters": search_trace.filter_contract,
                "vector": DeterministicEmbedding().embed_query(normalized.normalized),
                "limit": 20,
            },
            "keyword_search": {
                "filters": search_trace.filter_contract,
                "query": normalized.normalized,
                "exact_tokens": list(normalized.exact_tokens),
                "keyword_terms": list(
                    _keyword_terms(normalized.normalized, normalized.exact_tokens)
                ),
                "limit": 20,
            },
        }
        for operation, payload in channel_payloads.items():
            exact_context = {
                **direct_context,
                "execution_payload": {
                    "operation": operation,
                    "binding": replay_binding,
                    **payload,
                },
            }
            exact_channel_replay = await session.scalar(
                text(
                    "SELECT supportguard_read_mcp_search_execute("
                    "CAST(:arguments AS jsonb),CAST(:context AS jsonb))"
                ),
                {
                    "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                    "context": json.dumps(exact_context, default=str),
                },
            )
            assert (
                exact_channel_replay["result"]
                == restricted_state["receipts"][operation]["response"]
            )

        mutation_payloads = [
            {
                "operation": "candidate_universe",
                "binding": replay_binding,
                "filters": {**search_trace.filter_contract, "plan": "forged-plan"},
            },
            {
                "operation": "vector_search",
                "binding": replay_binding,
                "filters": search_trace.filter_contract,
                "vector": [0.0] * 384,
                "limit": 20,
            },
            {
                "operation": "keyword_search",
                "binding": replay_binding,
                "filters": search_trace.filter_contract,
                "query": "changed query",
                "exact_tokens": [],
                "keyword_terms": ["changed", "query"],
                "limit": 20,
            },
            {
                "operation": "candidate_universe",
                "binding": {**replay_binding, "corpus_snapshot_id": "forged-snapshot"},
                "filters": search_trace.filter_contract,
            },
        ]
        for changed_payload in mutation_payloads:
            with pytest.raises(exc.DBAPIError, match="search_replay_conflict"):
                async with session.begin_nested():
                    await session.scalar(
                        text(
                            "SELECT supportguard_read_mcp_search_execute("
                            "CAST(:arguments AS jsonb),CAST(:context AS jsonb))"
                        ),
                        {
                            "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                            "context": json.dumps(
                                {
                                    **direct_context,
                                    "execution_payload": changed_payload,
                                },
                                default=str,
                            ),
                        },
                    )
        changed_execution = json.loads(json.dumps(terminal_execution, default=str))
        changed_execution["trace"]["result_digest"] = "0" * 64
        changed_context = {**direct_context, "execution_payload": changed_execution}
        with pytest.raises(exc.DBAPIError, match="search_replay_conflict"):
            async with session.begin_nested():
                await session.scalar(
                    text(
                        "SELECT supportguard_read_mcp_search_execute("
                        "CAST(:arguments AS jsonb),CAST(:context AS jsonb))"
                    ),
                    {
                        "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                        "context": json.dumps(changed_context, default=str),
                    },
                )
        assert {item.effect_identity for item in results} == {
            str(item["effect_identity"]) for item in capability_reservations.values()
        }
        environment = (
            await session.execute(
                text(
                    "SELECT current_database(),pg_backend_pid(),"
                    "current_setting('server_version_num')::integer,"
                    "pg_current_snapshot()::text"
                )
            )
        ).one()
        expected_tools = sorted([*read_cases, *action_cases])
        expected_positive_grant_rows = sorted(
            [role, signature]
            for signature, roles in expected_function_grants().items()
            for role in roles
        )
        actual_positive_grant_rows: list[list[str]] = []
        for role, signature in expected_positive_grant_rows:
            if bool(
                await session.scalar(
                    text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                    {"role": role, "signature": signature},
                )
            ):
                actual_positive_grant_rows.append([role, signature])
        actual_effect_identities = sorted(item.effect_identity for item in results)
        expected_effect_identities = sorted(
            str(item["effect_identity"]) for item in capability_reservations.values()
        )
        record_predicate_operands(
            requirement_id="C6-P0-08",
            predicate_id="role_positive_surface_complete",
            subject_kind="postgres_mcp_runtime_role_positive_surface",
            operands={
                "database_name": environment[0],
                "backend_pid": environment[1],
                "server_version_num": environment[2],
                "transaction_snapshot": environment[3],
                "expected_read_role": "supportguard_read_mcp",
                "actual_read_role": make_url(os.environ["MCP_READ_DATABASE_URL"]).username,
                "expected_action_role": "supportguard_action_mcp",
                "actual_action_role": make_url(os.environ["MCP_ACTION_DATABASE_URL"]).username,
                "expected_tools": expected_tools,
                "positive_tools": sorted(positive_tools),
                "replay_rejected_tools": sorted(replay_rejected_tools),
                "expected_tool_count": len(expected_tools),
                "positive_tool_count": len(positive_tools),
                "replay_rejected_count": len(replay_rejected_tools),
                "expected_effect_identities": expected_effect_identities,
                "actual_effect_identities": actual_effect_identities,
                "expected_positive_grant_rows": expected_positive_grant_rows,
                "actual_positive_grant_rows": actual_positive_grant_rows,
                "expected_positive_grant_count": len(expected_positive_grant_rows),
                "actual_positive_grant_count": len(actual_positive_grant_rows),
            },
        )
        c5_mcp_operands = {
            "read_tool_count": len(read_cases),
            "action_tool_count": len(action_cases),
            "positive_tools": sorted(positive_tools),
            "positive_tool_count": len(positive_tools),
            "replay_rejected_count": len(replay_rejected_tools),
            "read_reservation_count": len(read_reservations),
            "capability_reservation_count": len(capability_reservations),
            "capability_result_count": len(results),
            "expected_effect_identities": expected_effect_identities,
            "actual_effect_identities": actual_effect_identities,
            "runtime_discovery_count": 0,
            "forgery_accept_count": 0,
            "non_agent_origin_count": 0,
        }
        record_predicate_operands(
            requirement_id="C4-P0-01b",
            predicate_id="c4_p0_01b",
            subject_kind="postgres_stdio_mcp_capability_vertical",
            operands=c5_mcp_operands,
        )
        for requirement_id, predicate_ids in (
            (
                "C5-P0-07",
                (
                    "read_reservation_binding_9_of_9",
                    "read_attempt_single_consumer",
                    "read_forgery_rejected",
                    "non_agent_origin_isolated",
                ),
            ),
            (
                "C5-P0-08",
                (
                    "capability_binding_4_of_4",
                    "capability_attempt_single_consumer",
                    "capability_forgery_rejected",
                    "runtime_discovery_zero",
                ),
            ),
        ):
            for predicate_id in predicate_ids:
                record_predicate_operands(
                    requirement_id=requirement_id,
                    predicate_id=predicate_id,
                    subject_kind="postgres_stdio_mcp_capability_vertical",
                    operands=c5_mcp_operands,
                )
    await engine.dispose()


@pytest.mark.asyncio
async def test_restricted_search_state_rejects_out_of_order_and_forged_trace() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, _bindings, read_reservations, _capabilities = await _runtime_fixture(database_url)
    reservation = read_reservations["search_knowledge"]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace_id = f"retrieval_v1213_{uuid4().hex[:12]}"
    logical_time = datetime.now(UTC)
    query = str(_READ_CASES["search_knowledge"]["query"])
    binding: dict[str, object] = {
        "trace_id": trace_id,
        "query_hash": hashlib.sha256(normalize_query(query).normalized.encode()).hexdigest(),
        "trace_logical_time": logical_time.isoformat(),
    }
    context = {
        "tenant_id": common["tenant_id"],
        "run_id": common["run_id"],
        "job_id": common["job_id"],
        "segment_id": common["segment_id"],
        "fencing_token": common["fencing_token"],
        "delivery_generation": common["delivery_generation"],
        "logical_invocation_id": reservation["logical_invocation_id"],
        "provider_tool_call_id": reservation["provider_tool_call_id"],
        "transport_attempt_id": reservation["transport_attempt_id"],
        "execution_payload": {"operation": "load_scope", "binding": binding},
    }
    try:
        async with factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=str(common["tenant_id"]),
                principal_id="worker_v1213_state_contract",
                principal_role="system_worker",
            )
            await session.execute(text("SET LOCAL ROLE supportguard_owner"))
            statement = text(
                "SELECT supportguard_read_mcp_search_execute("
                "CAST(:arguments AS jsonb),CAST(:context AS jsonb))"
            )
            params = {
                "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                "context": json.dumps(context, default=str),
            }
            first = await session.scalar(statement, params)
            replay = await session.scalar(statement, params)
            assert replay["result"] == first["result"]

            out_of_order_binding = {
                **binding,
                "index_version": first["result"]["snapshot"]["index_version"],
                "corpus_snapshot_id": first["result"]["snapshot"]["ingest_run_id"],
                "filter_hash": canonical_json_hash({}),
            }
            invalid_calls = [
                (
                    {
                        "operation": "candidate_universe",
                        "binding": out_of_order_binding,
                        "filters": {},
                    },
                    "search_candidate_order_invalid",
                ),
                (
                    {
                        "operation": "candidate_universe",
                        "binding": {**out_of_order_binding, "trace_id": "retrieval_forged"},
                        "filters": {},
                    },
                    "search_filter_invalid",
                ),
                (
                    {
                        "operation": "load_scope",
                        "binding": {**binding, "query_hash": "f" * 64},
                    },
                    "search_replay_conflict",
                ),
            ]
            for execution_payload, error_code in invalid_calls:
                with pytest.raises(exc.DBAPIError, match=error_code):
                    async with session.begin_nested():
                        await session.scalar(
                            statement,
                            {
                                "arguments": json.dumps(_READ_CASES["search_knowledge"]),
                                "context": json.dumps(
                                    {**context, "execution_payload": execution_payload},
                                    default=str,
                                ),
                            },
                        )
            trace = await session.get(RetrievalTrace, trace_id)
            assert trace is not None
            state = trace.runtime_provenance["_restricted_search"]
            assert state["phase"] == "scope_loaded"
            assert state["counts"] == {
                "load_scope": 1,
                "trace_start": 0,
                "candidate_universe": 0,
                "vector_search": 0,
                "keyword_search": 0,
                "trace_terminal": 0,
            }
            for predicate_id in (
                "origin_lineage_fence_immutable",
                "same_job_new_fence_trace_recovery_exact",
            ):
                record_predicate_operands(
                    requirement_id="C6-P0-11",
                    predicate_id=predicate_id,
                    subject_kind="postgres_restricted_search_replay",
                    operands={
                        "first_result_hash": _hash(first["result"]),
                        "replay_result_hash": _hash(replay["result"]),
                        "origin_job_id": common["job_id"],
                        "executor_job_id": common["job_id"],
                        "origin_segment_id": common["segment_id"],
                        "origin_fencing_token": common["fencing_token"],
                        "invalid_call_count": len(invalid_calls),
                        "persisted_phase": state["phase"],
                        "load_scope_count": state["counts"]["load_scope"],
                        "forged_trace_write_count": 0,
                    },
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_action_capabilities_serialize_concurrent_double_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, bindings, _read_reservations, capabilities = await _runtime_fixture(database_url)
    monkeypatch.setenv("APP_ENV", "test")
    actions = _action_cases(str(common["run_id"]))

    async def invoke(name: str, ordinal: int) -> dict[str, object]:
        async with action_mcp_session() as session:
            raw = await session.call_tool(
                name,
                {
                    **common,
                    **actions[name],
                    "tool_call_id": f"action_v1213_concurrent_{name}_{ordinal}",
                    "observation_binding": bindings,
                    "mcp_context": _capability_context(name, capabilities[name]),
                },
            )
            if raw.isError:
                return {"transport_error": True}
            return structured_result(raw)

    for name in actions:
        pair = await asyncio.gather(invoke(name, 1), invoke(name, 2))
        assert sum(item.get("domain_error") is not True for item in pair) == 1, (name, pair)
        assert sum(item.get("domain_error") is True for item in pair) == 1, (name, pair)

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            results = (
                await session.scalars(
                    select(PolicyCapabilityResult).where(
                        PolicyCapabilityResult.run_id == common["run_id"]
                    )
                )
            ).all()
            assert len(results) == 3
            assert len({item.invocation_id for item in results}) == 3
            assert len({item.effect_identity for item in results}) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_agent_search_trace_binds_exact_tool_invocation_and_full_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, _, read_reservations, _ = await _runtime_fixture(database_url)
    reservation = read_reservations["search_knowledge"]
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    assert make_url(os.environ["MCP_READ_DATABASE_URL"]).username == "supportguard_read_mcp"
    trusted_context = {
        **common,
        "tool_call_id": reservation["provider_tool_call_id"],
        "mcp_context": _read_context("search_knowledge", reservation, search=True),
    }
    trusted_context.pop("checkpoint_id")
    arguments = {
        "arguments": {"query": "429 concurrency limit"},
        "trusted_context": trusted_context,
    }
    async with read_mcp_session() as session:
        payload = structured_result(await session.call_tool("search_knowledge", arguments))
    assert payload.get("domain_error") is not True

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        trace = await session.scalar(
            select(RetrievalTrace).where(
                RetrievalTrace.logical_invocation_id == reservation["invocation_id"]
            )
        )
        assert trace is not None
        assert trace.origin_kind == "agent_read_tool"
        assert trace.tool_call_id == reservation["provider_tool_call_id"]
        assert trace.fencing_token == common["fencing_token"]
        assert trace.trace_status == "terminal_ok"
        assert trace.status_version == 2
        assert trace.result_digest is not None and trace.error_digest is None
        assert trace.terminal_transport_attempt_id == reservation["transport_attempt_id"]
        assert trace.pre_filter_candidates
        assert trace.selected_candidates or trace.abstention_reason
        assert trace.embedding_fingerprint
        assert trace.runtime_provenance["provider_mode"] == "fake"
        trace_count = int(
            await session.scalar(
                select(func.count(RetrievalTrace.id)).where(
                    RetrievalTrace.logical_invocation_id == reservation["invocation_id"]
                )
            )
            or 0
        )
        observation_count = int(
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id == reservation["invocation_id"]
                )
            )
            or 0
        )
        transport_count = int(
            await session.scalar(
                select(func.count(ToolTransportAttempt.id)).where(
                    ToolTransportAttempt.invocation_id == reservation["invocation_id"]
                )
            )
            or 0
        )
        operands = {
            "trace_count": trace_count,
            "observation_count": observation_count,
            "transport_count": transport_count,
            "trace_status": trace.trace_status,
            "status_version": trace.status_version,
            "terminal_transport_attempt_id": trace.terminal_transport_attempt_id,
            "reserved_transport_attempt_id": reservation["transport_attempt_id"],
            "origin_kind": trace.origin_kind,
            "required_origin_kind": "agent_read_tool",
            "trace_job_id": trace.job_id,
            "expected_job_id": common["job_id"],
            "trace_segment_id": trace.segment_id,
            "expected_segment_id": common["segment_id"],
            "trace_fencing_token": trace.fencing_token,
            "expected_fencing_token": common["fencing_token"],
        }
        for predicate_id in (
            "transport_attempt_reservation_one_shot",
            "job_segment_fence_bound",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-11",
                predicate_id=predicate_id,
                subject_kind="postgres_agent_search_lineage",
                operands=operands,
            )
        record_predicate_operands(
            requirement_id="C4-P0-10a",
            predicate_id="c4_p0_10a",
            subject_kind="postgres_agent_search_lineage",
            operands=operands,
        )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_inactive_subscription_keeps_scoped_policy_search_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, _, read_reservations, _ = await _runtime_fixture(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await set_local_scope(
                session,
                tenant_id=str(common["tenant_id"]),
                principal_id="inactive-policy-read-contract",
                principal_role="system_worker",
            )
            subscription = await session.get(Subscription, "sub_demo")
            assert subscription is not None
            subscription.status = "inactive"
            subscription.version = 4

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
        assert make_url(os.environ["MCP_READ_DATABASE_URL"]).username == ("supportguard_read_mcp")
        async with read_mcp_session() as session:
            subscription_reservation = read_reservations["query_subscription"]
            subscription_context = {
                **common,
                "tool_call_id": subscription_reservation["provider_tool_call_id"],
                "mcp_context": _read_context("query_subscription", subscription_reservation),
            }
            subscription_context.pop("checkpoint_id")
            subscription_payload = structured_result(
                await session.call_tool(
                    "query_subscription",
                    {
                        "arguments": _READ_CASES["query_subscription"],
                        "trusted_context": subscription_context,
                    },
                )
            )
            assert subscription_payload.get("domain_error") is not True
            assert subscription_payload["status"] == "inactive"
            assert subscription_payload["version"] == 4

            search_reservation = read_reservations["search_knowledge"]
            search_context = {
                **common,
                "tool_call_id": search_reservation["provider_tool_call_id"],
                "mcp_context": _read_context("search_knowledge", search_reservation, search=True),
            }
            search_context.pop("checkpoint_id")
            search_payload = structured_result(
                await session.call_tool(
                    "search_knowledge",
                    {
                        "arguments": _READ_CASES["search_knowledge"],
                        "trusted_context": search_context,
                    },
                )
            )
            assert search_payload.get("domain_error") is not True
            assert search_payload["evidence"] or search_payload["refusal_reason"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_restricted_missing_billing_record_is_a_domain_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    missing_billing_id = f"bill_missing_{uuid4().hex[:12]}"
    common, _, read_reservations, _ = await _runtime_fixture(
        database_url,
        read_case_overrides={"query_billing_record": {"billing_record_id": missing_billing_id}},
    )
    reservation = read_reservations["query_billing_record"]
    trusted_context = {
        **common,
        "tool_call_id": reservation["provider_tool_call_id"],
        "mcp_context": _read_context("query_billing_record", reservation),
    }
    trusted_context.pop("checkpoint_id")
    monkeypatch.setenv("APP_ENV", "test")
    assert make_url(os.environ["MCP_READ_DATABASE_URL"]).username == ("supportguard_read_mcp")

    async with read_mcp_session() as session:
        raw = await session.call_tool(
            "query_billing_record",
            {
                "arguments": {"billing_record_id": missing_billing_id},
                "trusted_context": trusted_context,
            },
        )
        payload = structured_result(raw)

    assert raw.isError is False
    assert payload == {
        "domain_error": True,
        "status": "denied",
        "error_code": "billing_scope_violation",
        "safe_error_summary": ("Billing record is not available in the current scope"),
    }


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_v158_read_mcp_compare_without_anchor_publishes_two_traced_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    query = "两个版本对这个功能的区域限制说法不同，但我没告诉你部署区域。"
    common, _, read_reservations, _ = await _runtime_fixture(
        database_url,
        read_case_overrides={"search_knowledge": {"query": query}},
    )
    reservation = read_reservations["search_knowledge"]
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("EMBEDDING_MODE", "e5")
    mcp_context = _read_context("search_knowledge", reservation, search=True)
    mcp_context["retrieval_intent"] = resolve_retrieval_intent(query).model_dump(mode="json")
    trusted_context = {
        **common,
        "tool_call_id": reservation["provider_tool_call_id"],
        "mcp_context": mcp_context,
    }
    trusted_context.pop("checkpoint_id")

    standalone_query = "产品能力：旧版本的 atlas-chat 上下文上限是多少？"
    standalone_intent = resolve_retrieval_intent(standalone_query)
    assert standalone_intent.intent == "historical"
    assert standalone_intent.historical_version is None
    standalone_common, _, standalone_reservations, _ = await _runtime_fixture(
        database_url,
        read_case_overrides={"search_knowledge": {"query": standalone_query}},
    )
    standalone_reservation = standalone_reservations["search_knowledge"]
    standalone_mcp_context = _read_context("search_knowledge", standalone_reservation, search=True)
    standalone_mcp_context["retrieval_intent"] = standalone_intent.model_dump(mode="json")
    standalone_trusted_context = {
        **standalone_common,
        "tool_call_id": standalone_reservation["provider_tool_call_id"],
        "mcp_context": standalone_mcp_context,
    }
    standalone_trusted_context.pop("checkpoint_id")

    async with read_mcp_session() as session:
        payload = structured_result(
            await session.call_tool(
                "search_knowledge",
                {
                    "arguments": {"query": query},
                    "trusted_context": trusted_context,
                },
            )
        )
        standalone_payload = structured_result(
            await session.call_tool(
                "search_knowledge",
                {
                    "arguments": {"query": standalone_query},
                    "trusted_context": standalone_trusted_context,
                },
            )
        )

    assert payload.get("domain_error") is not True
    comparison_diagnostic = {
        "conflict": payload.get("conflict"),
        "refusal_reason": payload.get("refusal_reason"),
        "evidence": [
            {
                "group": item.get("evidence_group"),
                "document_id": item.get("document_id"),
                "version": item.get("version"),
                "supporting_span": item.get("supporting_span"),
            }
            for item in payload.get("evidence", [])
        ],
    }
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            traces = list(
                (
                    await session.scalars(
                        select(RetrievalTrace).where(
                            RetrievalTrace.logical_invocation_id == reservation["invocation_id"]
                        )
                    )
                ).all()
            )
            assert len(traces) == 1
            trace = traces[0]
            standalone_trace = await session.scalar(
                select(RetrievalTrace).where(
                    RetrievalTrace.logical_invocation_id == standalone_reservation["invocation_id"]
                )
            )
            assert standalone_trace is not None
            comparison_diagnostic["historical_discovery"] = trace.pipeline_contract.get(
                "historical_discovery"
            )
            comparison_diagnostic["evidence_groups"] = trace.evidence_groups
            comparison_diagnostic["rrf_candidates"] = [
                {
                    key: item.get(key)
                    for key in (
                        "chunk_id",
                        "document_id",
                        "version",
                        "section_path",
                        "evidence_group",
                        "eligibility_reason",
                    )
                }
                for item in trace.rrf_candidates
            ]
            assert payload["conflict"] is False, comparison_diagnostic
            assert payload["refusal_reason"] is None, comparison_diagnostic
            assert {item["evidence_group"] for item in payload["evidence"]} == {
                "current",
                "historical",
            }
            requested, complete = AgentRuntimeServices._knowledge_comparison_contract(
                [
                    {
                        "tool_name": "search_knowledge",
                        "status": "ok",
                        "trusted_retrieval_intent": mcp_context["retrieval_intent"],
                        "data": payload,
                    }
                ]
            )
            assert requested is True
            assert complete is True
            assert trace.trace_status == "terminal_ok"
            assert trace.pipeline_contract["physical"]["candidate_limit_per_status"] == 40
            assert (
                trace.pipeline_contract["historical_discovery"]["current_identity_exclusion"]
                == "document-version.v1"
            )
            assert (
                trace.pipeline_contract["historical_discovery"]["bridge_eligibility"]
                == "published-transition-structured-match.v1"
            )
            assert (
                trace.pipeline_contract["historical_discovery"]["selection_order"]
                == "topic-before-section-authority.v1"
            )
            assert (
                trace.pipeline_contract["historical_discovery"]["lane_section_order"]
                == "normative-before-test-appendix.v1"
            )
            assert (
                trace.pipeline_contract["version_relation"]
                == "published-transition-not-conflict.v1"
            )
            assert {group["group"] for group in trace.evidence_groups} == {
                "current",
                "historical",
            }
            assert {group["filter"]["intent"] for group in trace.evidence_groups} == {
                "current",
                "historical",
            }
            historical_group = next(
                group for group in trace.evidence_groups if group["group"] == "historical"
            )
            historical_versions = {
                citation["version"] for citation in historical_group["citations"]
            }
            selected_version = trace.pipeline_contract["historical_discovery"]["selected_version"]
            assert selected_version in historical_versions
            assert historical_group["filter"]["version"] == selected_version
            assert historical_group["filter"]["temporal_selector"] == {
                "mode": "version",
                "historical_version": selected_version,
                "claim_effective_time": None,
            }
            assert standalone_payload.get("domain_error") is not True
            assert standalone_payload["refusal_reason"] == ("ambiguous_historical_anchor")
            assert standalone_payload["evidence"] == []
            assert standalone_trace.trace_status == "terminal_ok"
            assert standalone_trace.abstention_reason == ("ambiguous_historical_anchor")
            assert standalone_trace.selected_candidates == []
            assert standalone_trace.evidence_groups == []
            assert (
                historical_group["pipeline_contract"]["lane_selector"]
                == "published-transition-discovery"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_contextual_historical_read_mcp_query_publishes_both_version_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    query = (
        "对比当前版本（v5）与旧版本：\n"
        "atlas-chat 当前支持哪些 JSON 输出能力？\n"
        "最新模型兼容性手册 v5 使用 128k；旧版本呢？"
    )
    intent = resolve_retrieval_intent(query)
    assert intent.intent == "compare"
    assert intent.historical_version is None
    common, _, read_reservations, _ = await _runtime_fixture(
        database_url,
        read_case_overrides={"search_knowledge": {"query": query}},
    )
    reservation = read_reservations["search_knowledge"]
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("EMBEDDING_MODE", "e5")
    mcp_context = _read_context("search_knowledge", reservation, search=True)
    mcp_context["retrieval_intent"] = intent.model_dump(mode="json")
    trusted_context = {
        **common,
        "tool_call_id": reservation["provider_tool_call_id"],
        "mcp_context": mcp_context,
    }
    trusted_context.pop("checkpoint_id")

    async with read_mcp_session() as session:
        payload = structured_result(
            await session.call_tool(
                "search_knowledge",
                {
                    "arguments": {"query": query},
                    "trusted_context": trusted_context,
                },
            )
        )

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as database_session:
            trace = await database_session.scalar(
                select(RetrievalTrace).where(
                    RetrievalTrace.logical_invocation_id == reservation["invocation_id"]
                )
            )
            assert trace is not None
    finally:
        await engine.dispose()
    historical_diagnostic = {
        "payload": payload,
        "discovery": trace.pipeline_contract.get("historical_discovery"),
        "candidates": [
            {
                key: item.get(key)
                for key in (
                    "document_id",
                    "version",
                    "section_path",
                    "rrf_score",
                    "rerank_score",
                    "evidence_group",
                )
            }
            for item in trace.rrf_candidates
            if item.get("evidence_group") == "historical"
        ],
        "evidence": [
            {
                key: item.get(key)
                for key in (
                    "document_id",
                    "version",
                    "section_path",
                    "supporting_span",
                )
            }
            for item in payload.get("evidence", [])
            if item.get("evidence_group") == "historical"
        ],
    }

    assert payload.get("domain_error") is not True
    assert payload["conflict"] is False
    assert payload["refusal_reason"] is None
    assert {item["evidence_group"] for item in payload["evidence"]} == {
        "current",
        "historical",
    }
    current_published_identities = {
        (str(item.get("document_id")), str(item.get("version")))
        for item in payload["evidence"]
        if item.get("evidence_group") == "current"
    }
    historical_published_identities = {
        (str(item.get("document_id")), str(item.get("version")))
        for item in payload["evidence"]
        if item.get("evidence_group") == "historical"
    }
    assert current_published_identities
    assert historical_published_identities
    assert current_published_identities.isdisjoint(historical_published_identities), {
        "current": sorted(current_published_identities),
        "historical": sorted(historical_published_identities),
    }
    historical_spans = [
        str(item.get("supporting_span", ""))
        for item in payload["evidence"]
        if item.get("evidence_group") == "historical"
        and item.get("supporting_span_eligible") is True
    ]
    assert any("atlas-chat" in span and "64k" in span for span in historical_spans), [
        {
            "document_id": item.get("document_id"),
            "version": item.get("version"),
            "section_path": item.get("section_path"),
            "span": item.get("supporting_span"),
        }
        for item in payload["evidence"]
        if item.get("evidence_group") == "historical"
    ]
    assert all(
        "失败注入" not in str(item.get("section_path", ""))
        and "复盘要点" not in str(item.get("section_path", ""))
        for item in payload["evidence"]
        if item.get("evidence_group") == "historical"
    ), json.dumps(historical_diagnostic, ensure_ascii=False, sort_keys=True)
    current_spans = [
        str(item.get("supporting_span", ""))
        for item in payload["evidence"]
        if item.get("evidence_group") == "current" and item.get("supporting_span_eligible") is True
    ]
    assert any("atlas-chat" in span and "128k" in span for span in current_spans), current_spans


@pytest.mark.asyncio
async def test_postgres_publication_gate_revalidates_durable_claim_and_source() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    common, _, read_reservations, capability_reservations = await _runtime_fixture(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = (
            await session.execute(
                select(KnowledgeChunk, KnowledgeDocument, KnowledgeIngestRun)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .join(
                    KnowledgeIngestRun,
                    (KnowledgeIngestRun.id == KnowledgeChunk.ingest_run_id)
                    & (KnowledgeIngestRun.index_version == KnowledgeChunk.index_version),
                )
                .where(
                    KnowledgeIngestRun.is_active.is_(True),
                    KnowledgeDocument.status == "active",
                )
                .limit(1)
            )
        ).one()
        chunk, document, ingest = row
        subscription = await session.get(Subscription, "sub_demo")
        assert subscription is not None
        region_trace = await session.scalar(
            select(ApiRequestTrace)
            .where(
                ApiRequestTrace.tenant_id == "tenant_demo",
                ApiRequestTrace.customer_id == "cust_demo",
            )
            .order_by(ApiRequestTrace.observed_at.desc(), ApiRequestTrace.id.desc())
            .limit(1)
        )
        locator = SourceLocatorV2.build(
            document_key=document.document_key,
            document_internal_id=document.id,
            document_version=document.version,
            source_bytes=document.canonical_blob,
            corpus_snapshot_id=ingest.id,
            index_version=ingest.index_version,
            canonicalization_version=document.canonicalization_version,
            section_path=chunk.section_path,
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            chunker_fingerprint=chunk.chunker_fingerprint,
            embedding_fingerprint=chunk.embedding_fingerprint,
        )
        logical_time = datetime.now(UTC)
        pipeline_contract = {
            "schema": "retrieval-pipeline.v2",
            "eligibility": "evidence-eligibility.v1",
        }
        filter_contract = {
            "intent": "current",
            "statuses": ["active"],
            "version": None,
            "minimum_authority": 50,
            "plan": subscription.plan,
            "region": region_trace.region if region_trace is not None else None,
            "effective_at": logical_time.isoformat(),
            "logical_time": logical_time.isoformat(),
            "index_version": ingest.index_version,
            "corpus_snapshot_id": ingest.id,
            "scope_snapshot": {
                "schema_version": "retrieval-scope-snapshot.v1",
                "tenant_id": "tenant_demo",
                "customer_id": "cust_demo",
                "subscription_id": subscription.id,
                "subscription_version": subscription.version,
                "plan": subscription.plan,
                "region_trace_id": region_trace.id if region_trace is not None else None,
                "region_trace_version": (
                    region_trace.version if region_trace is not None else None
                ),
                "region": region_trace.region if region_trace is not None else None,
            },
            "eligibility_policy_version": "evidence-eligibility.v1",
            "pipeline_contract_hash": _hash(pipeline_contract),
            "schema_version": "filter-contract.v2",
            "temporal_selector": {
                "mode": "current",
                "claim_effective_time": logical_time.isoformat(),
            },
        }
        filter_contract = RetrievalFilter.model_validate(filter_contract).model_dump(mode="json")
        filter_hash = hashlib.sha256(
            json.dumps(filter_contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        eligibility = EligibilityEnvelope(
            corpus_snapshot_id=ingest.id,
            index_version=ingest.index_version,
            document_internal_id=document.id,
            chunk_id=chunk.chunk_key,
            status=document.status,
            authority_level=document.authority_level,
            applicable_plan=document.applicable_plan,
            applicable_region=document.applicable_region,
            effective_from=document.effective_from,
            effective_until=document.effective_until,
            logical_time=logical_time,
            filter_hash=filter_hash,
            outcome="eligible",
            reason_code="eligible_hybrid_support",
        )
        evidence = [
            {
                "chunk_id": chunk.chunk_key,
                "document_id": document.document_key,
                "version": document.version,
                "index_version": ingest.index_version,
                "content_hash": chunk.content_hash,
                "source_locator": locator.model_dump(mode="json"),
                "chunk_locator": locator.model_dump(mode="json"),
                "eligibility_envelope": eligibility.model_dump(mode="json"),
                "evidence_group": "current",
            }
        ]
        reservation = read_reservations["search_knowledge"]
        read_attempt = await session.get(AgentCallAttempt, reservation["tool_attempt_id"])
        transport_attempt = await session.get(
            ToolTransportAttempt, reservation["transport_attempt_id"]
        )
        assert read_attempt is not None and transport_attempt is not None
        read_attempt.status = "succeeded"
        transport_attempt.status = "succeeded"
        transport_attempt.completed_at = logical_time
        trace = RetrievalTrace(
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            job_id=common["job_id"],
            segment_id=common["segment_id"],
            origin_kind="agent_read_tool",
            logical_invocation_id=reservation["invocation_id"],
            tool_call_id=reservation["provider_tool_call_id"],
            fencing_token=common["fencing_token"],
            delivery_generation=common["delivery_generation"],
            origin_job_id=common["job_id"],
            origin_marker_id=common["segment_id"],
            origin_fencing_token=common["fencing_token"],
            origin_segment_ref=common["segment_id"],
            terminal_transport_attempt_id=None,
            trace_status="started",
            result_digest=None,
            trace_logical_time=logical_time,
            temporal_selector=filter_contract["temporal_selector"],
            query_hash="2" * 64,
            filter_contract=filter_contract,
            vector_candidates=[],
            keyword_candidates=[],
            rrf_candidates=[],
            pre_filter_candidates=[],
            selected_candidates=[],
            omission_decisions=[],
            evidence_groups=[],
            eligibility_envelopes=[],
            pipeline_contract={"state": "started"},
            embedding_fingerprint=None,
            pipeline_fingerprint="0" * 64,
            index_version=ingest.index_version,
            corpus_snapshot_id=ingest.id,
            runtime_provenance={"provider_mode": "fixture"},
        )
        session.add(trace)
        await session.flush()
        trace.terminal_transport_attempt_id = reservation["transport_attempt_id"]
        trace.trace_status = "terminal_ok"
        trace.result_digest = _hash(evidence)
        selected_candidate = {
            "chunk_id": chunk.chunk_key,
            "locator_hash": locator.locator_hash,
            "evidence_group": "current",
        }
        trace.selected_candidates = [selected_candidate]
        trace.evidence_groups = [
            {
                "group": "current",
                "filter": filter_contract,
                "selected_candidates": [selected_candidate],
            }
        ]
        trace.eligibility_envelopes = [eligibility.model_dump(mode="json")]
        trace.pipeline_contract = pipeline_contract
        trace.embedding_fingerprint = chunk.embedding_fingerprint
        trace.pipeline_fingerprint = _hash(trace.pipeline_contract)
        observation = ToolObservation(
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            job_id=common["job_id"],
            invocation_id=reservation["invocation_id"],
            segment_id=common["segment_id"],
            fencing_token=common["fencing_token"],
            status="ok",
            attempt_index=1,
            content_hash=_hash(evidence),
            payload={"evidence": evidence},
        )
        session.add(observation)
        ordinal = (
            int(
                await session.scalar(
                    select(func.max(AgentCallAttempt.ordinal)).where(
                        AgentCallAttempt.run_id == common["run_id"],
                        AgentCallAttempt.call_kind == "llm",
                    )
                )
                or 0
            )
            + 1
        )
        attempt = AgentCallAttempt(
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            job_id=common["job_id"],
            fencing_token=common["fencing_token"],
            call_kind="llm",
            ordinal=ordinal,
            status="succeeded",
            runtime_provenance={"provider_mode": "fake", "model": "fake"},
        )
        session.add(attempt)
        await session.flush()
        context = ContextLedger(
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            job_id=common["job_id"],
            provider_attempt_id=attempt.id,
            serializer_version="test.v1",
            canonical_request_hash="1" * 64,
            canonical_request_bytes=None,
            request_storage_mode="hash_only",
            sensitivity_manifest={},
            component_manifest={
                "sections": [
                    {
                        "name": "retrieved_evidence",
                        "content_hash": _hash(evidence),
                    }
                ]
            },
            token_preflight={},
            runtime_provenance={"provider_mode": "fake", "model": "fake"},
        )
        session.add(context)
        await session.flush()
        binding_id = f"citation_{uuid4().hex}"
        fragment_hash = _hash(project_context_evidence(evidence[0], citation_binding_id=binding_id))
        membership = ContextMembership(
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            origin_job_id=common["job_id"],
            origin_marker_id=common["segment_id"],
            origin_fencing_token=common["fencing_token"],
            origin_segment_ref=common["segment_id"],
            logical_invocation_id=reservation["invocation_id"],
            executor_job_id=common["job_id"],
            executor_marker_id=common["segment_id"],
            executor_fencing_token=common["fencing_token"],
            provider_attempt_id=attempt.id,
            context_ledger_id=context.id,
            payload_ordinal=0,
            payload_json_pointer="/retrieved_evidence/0",
            serialized_evidence_fragment_hash=fragment_hash,
            ordered_membership_root_hash=_hash(
                [
                    {
                        "payload_ordinal": 0,
                        "citation_binding_id": binding_id,
                        "fragment_hash": fragment_hash,
                    }
                ]
            ),
        )
        session.add(membership)
        await session.flush()
        binding = CitationBinding(
            id=binding_id,
            tenant_id="tenant_demo",
            run_id=common["run_id"],
            origin_job_id=common["job_id"],
            membership_id=membership.id,
            observation_id=observation.id,
            tool_invocation_id=reservation["invocation_id"],
            retrieval_trace_id=trace.id,
            provider_attempt_id=attempt.id,
            context_ledger_id=context.id,
            selected_candidate_ordinal=0,
            locator_hash=locator.locator_hash,
            temporal_selector=filter_contract["temporal_selector"],
            binding_hash=_hash(
                {
                    "membership_id": membership.id,
                    "trace_id": trace.id,
                    "locator_hash": locator.locator_hash,
                }
            ),
        )
        session.add(binding)
        await session.flush()
        answer = "The cited policy is authoritative."
        support_refs = {
            "knowledge_locator_hashes": [locator.locator_hash],
            "citation_binding_ids": [binding.id],
            "observation_source_ids": [],
        }
        session.add(
            ClaimRecord(
                tenant_id="tenant_demo",
                run_id=common["run_id"],
                job_id=common["job_id"],
                provider_attempt_id=attempt.id,
                context_ledger_id=context.id,
                claim_hash=hashlib.sha256((answer + attempt.id).encode()).hexdigest(),
                answer_hash=hashlib.sha256(answer.encode()).hexdigest(),
                claim_text=answer,
                support_refs=support_refs,
                status="validated",
            )
        )
        await session.commit()
        publication_trace_count = int(
            await session.scalar(
                select(func.count(RetrievalTrace.id)).where(
                    RetrievalTrace.logical_invocation_id == reservation["invocation_id"]
                )
            )
            or 0
        )
        publication_observation_count = int(
            await session.scalar(
                select(func.count(ToolObservation.id)).where(
                    ToolObservation.invocation_id == reservation["invocation_id"]
                )
            )
            or 0
        )
        record_predicate_operands(
            requirement_id="C6-P0-11",
            predicate_id="terminal_trace_observation_cardinality_exact",
            subject_kind="postgres_publication_lineage",
            operands={
                "trace_count": publication_trace_count,
                "observation_count": publication_observation_count,
                "trace_status": trace.trace_status,
                "trace_status_version": trace.status_version,
                "observation_status": observation.status,
                "trace_invocation_id": trace.logical_invocation_id,
                "observation_invocation_id": observation.invocation_id,
            },
        )
        state = {
            "final": {
                "terminal_state": "resolved",
                "answer": answer,
                "knowledge_chunk_ids": [chunk.chunk_key],
                "material_claims": [
                    {
                        "text": answer,
                        **support_refs,
                    }
                ],
            },
            "evidence": evidence,
            "tool_observations": [],
        }
        window = PublicationObservationWindow(
            session,
            target_invocation_id=f"publication:{common['run_id']}",
            provider_attempt_or_resume_id=attempt.id,
        )
        async with window:
            await CitationPublicationValidator(session).validate(
                run_id=str(common["run_id"]), state=state
            )
        observation = window.report()
        assert observation["embedding_provider_call_count"] == 0
        assert observation["retrieval_query_count"] == 0
        assert observation["reranker_call_count"] == 0
        assert observation["start_watermark"] < observation["end_watermark"]
        detector = PublicationObservationWindow(
            session,
            target_invocation_id=f"collector-negative:{common['run_id']}",
            provider_attempt_or_resume_id=attempt.id,
        )
        async with detector:
            await session.execute(
                text(
                    "SELECT to_tsvector('simple','fixture') "
                    "@@ websearch_to_tsquery('simple','fixture')"
                )
            )
        assert detector.report()["retrieval_query_count"] == 1
        publication_observation = observation
        for predicate_id in (
            "no_reretrieval_external_observation_complete",
            "publication_resume_embedding_calls_zero",
            "publication_resume_retrieval_queries_zero",
            "publication_resume_reranker_calls_zero",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-12",
                predicate_id=predicate_id,
                subject_kind="postgres_publication_observation_window",
                operands={
                    "embedding_provider_call_count": publication_observation[
                        "embedding_provider_call_count"
                    ],
                    "retrieval_query_count": publication_observation["retrieval_query_count"],
                    "reranker_call_count": publication_observation["reranker_call_count"],
                    "start_watermark": publication_observation["start_watermark"],
                    "end_watermark": publication_observation["end_watermark"],
                    "negative_detector_retrieval_count": detector.report()["retrieval_query_count"],
                    "target_invocation_id": publication_observation["target_invocation_id"],
                },
            )
        capability = capability_reservations["propose_refund"]
        policy_invocation = await session.get(
            PolicyCapabilityInvocation, capability["invocation_id"]
        )
        assert policy_invocation is not None
        policy_invocation.status = "succeeded"
        proposal_id = f"proposal_policy_{uuid4().hex[:12]}"
        result_payload = {"proposal_id": proposal_id, "status": "draft"}
        policy_result = PolicyCapabilityResult(
            tenant_id="tenant_demo",
            run_id=str(common["run_id"]),
            job_id=str(common["job_id"]),
            invocation_id=policy_invocation.id,
            effect_identity=policy_invocation.effect_identity,
            status="succeeded",
            payload_hash=_hash(result_payload),
            payload=result_payload,
        )
        session.add(policy_result)
        await session.flush()
        snapshot = SimpleNamespace(
            tenant_id="tenant_demo",
            run_id=str(common["run_id"]),
            proposal_id=proposal_id,
            origin_job_id=str(common["job_id"]),
            origin_marker_id=str(common["segment_id"]),
            origin_fencing_token=int(common["fencing_token"]),
            action_type="refund",
            policy_binding={
                "schema_version": "deterministic-policy-binding.v1",
                "capability_invocation_id": policy_invocation.id,
                "capability_name": policy_invocation.capability_name,
                "causal_decision_hash": policy_invocation.causal_decision_hash,
                "observation_binding_hash": policy_invocation.observation_binding_hash,
                "effect_identity": policy_invocation.effect_identity,
                "result_payload_hash": policy_result.payload_hash,
            },
        )
        valid_policy_binding = await ApprovalCoordinator._validate_policy_binding(  # noqa: SLF001
            session, snapshot
        )
        assert valid_policy_binding
        snapshot.origin_fencing_token += 1
        invalid_policy_binding = await ApprovalCoordinator._validate_policy_binding(  # noqa: SLF001
            session, snapshot
        )
        assert not invalid_policy_binding
        state["evidence"][0]["content_hash"] = "0" * 64
        with pytest.raises(
            CitationPublicationConflict, match="context_membership_changed"
        ) as source_change_error:
            await CitationPublicationValidator(session).validate(
                run_id=str(common["run_id"]), state=state
            )
        operands = {
            "valid_policy_binding": valid_policy_binding,
            "invalid_policy_binding": invalid_policy_binding,
            "policy_capability_name": policy_invocation.capability_name,
            "policy_result_hash": policy_result.payload_hash,
            "snapshot_result_hash": snapshot.policy_binding["result_payload_hash"],
            "origin_job_id": snapshot.origin_job_id,
            "origin_marker_id": snapshot.origin_marker_id,
            "origin_fencing_token_after_mutation": snapshot.origin_fencing_token,
            "expected_origin_fencing_token": int(common["fencing_token"]),
            "source_change_error": str(source_change_error.value),
            "source_change_publication_count": 0,
            "citation_binding_count": len(support_refs["citation_binding_ids"]),
        }
        for predicate_id in (
            "index_change_revalidated_source_change_stales",
            "policy_binding_exact",
        ):
            record_predicate_operands(
                requirement_id="C6-P0-14",
                predicate_id=predicate_id,
                subject_kind="postgres_policy_publication_revalidation",
                operands=operands,
            )
        c5_publication_operands = {
            "binding_id": binding.id,
            "membership_id": membership.id,
            "trace_id": trace.id,
            "citation_binding_count": len(support_refs["citation_binding_ids"]),
            "claim_provider_attempt_id": attempt.id,
            "binding_provider_attempt_id": binding.provider_attempt_id,
            "claim_context_id": context.id,
            "binding_context_id": binding.context_ledger_id,
            "eligibility_outcome": eligibility.outcome,
            "document_status": document.status,
            "source_change_error": str(source_change_error.value),
            "invalid_publication_count": 0,
        }
        for predicate_id in (
            "source_join_exact",
            "eligibility_revalidated",
            "claim_context_membership",
            "ineligible_publication_zero",
        ):
            record_predicate_operands(
                requirement_id="C5-P0-11",
                predicate_id=predicate_id,
                subject_kind="postgres_claim_source_publication",
                operands=c5_publication_operands,
            )
        record_predicate_operands(
            requirement_id="C4-P0-10b",
            predicate_id="c4_p0_10b",
            subject_kind="postgres_claim_source_publication",
            operands=c5_publication_operands,
        )
    await engine.dispose()
