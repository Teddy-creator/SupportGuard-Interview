from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from current_identity_convergence import (
    IdentityConvergenceFacts,
    IdentityConvergenceOutcome,
    classify_identity_convergence,
)
from supportguard.config import Settings, get_settings
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApiKeyMetadata,
    ApprovalActionRevision,
    ApprovalRequest,
    ApprovalSnapshot,
    BillingRecord,
    BusinessAction,
    CheckpointCommitMarker,
    CitationBinding,
    ContextMembership,
    FinalizerPayload,
    HumanDecision,
    PolicyCapabilityInvocation,
    PolicyCapabilityResult,
    ProposalRecord,
    RuntimeJob,
    Subscription,
    TicketMessage,
    ToolInvocation,
    ToolObservation,
)
from supportguard.main import create_app
from supportguard.runtime.worker import worker_runtime
from supportguard.services.runtime_queue import OutboxDispatcher

pytestmark = [pytest.mark.postgres, pytest.mark.redis, pytest.mark.mcp]
ROOT = Path(__file__).resolve().parents[2]


def _fixture_seed() -> Callable[[str], Any]:
    path = Path(__file__).parents[2] / "scripts" / "identity_bound_e2e_fixture.py"
    spec = importlib.util.spec_from_file_location("identity_bound_e2e_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("identity_bound_e2e_fixture_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.seed  # type: ignore[no-any-return]


def _database_url(base: str, username: str) -> str:
    return (
        make_url(base)
        .set(username=username, password=username)
        .render_as_string(hide_password=False)
    )


def _redis_url(base: str, username: str, password: str) -> str:
    return (
        make_url(base)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


async def _wait_for_job(
    worker: Any,
    admin_factory: async_sessionmaker[AsyncSession],
    job_id: str,
) -> RuntimeJob:
    for _ in range(30):
        await worker.consume_once(block_ms=100)
        async with admin_factory() as session:
            job = await session.get(RuntimeJob, job_id)
            if job is not None and job.status in {"succeeded", "failed", "dead"}:
                return job
    raise AssertionError(f"job did not reach a terminal state: {job_id}")


def _resource_id(approval: dict[str, Any]) -> str:
    return str(approval["resource_id"])


EXPECTED_CANDIDATE_ACTION = {
    "refund": "refund_proposal",
    "api_key_revocation": "api_key_revocation_proposal",
    "entitlement_change": "entitlement_change_proposal",
}
EXPECTED_CAPABILITY = {
    "refund": "propose_refund",
    "api_key_revocation": "propose_api_key_revocation",
    "entitlement_change": "propose_entitlement_change",
}


class IdentityScenarioFailure(AssertionError):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        super().__init__(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
        self.snapshot = snapshot


def _write_external_diagnostic(payload: dict[str, Any]) -> None:
    raw_path = os.getenv("SUPPORTGUARD_V1218_DIAGNOSTIC_PATH")
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise RuntimeError("v1218_diagnostic_path_must_be_repository_external")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _database_approval_resource_id(approval: ApprovalRequest) -> str:
    return str(
        approval.action_payload.get("billing_record_id")
        or approval.action_payload.get("api_key_id")
        or approval.action_payload.get("subscription_id")
        or ""
    )


async def _authoritative_observation(
    admin_factory: async_sessionmaker[AsyncSession],
    *,
    accepted: dict[str, Any],
    fixture: dict[str, Any],
    action_type: str,
) -> dict[str, Any]:
    async with admin_factory() as session:
        job = await session.get(RuntimeJob, accepted["job_id"])
        run = await session.get(AgentRun, accepted["run_id"])
        events = list(
            (
                await session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == accepted["run_id"])
                    .order_by(AgentEvent.run_sequence)
                )
            ).all()
        )
        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation).where(ToolInvocation.run_id == accepted["run_id"])
                )
            ).all()
        )
        observations = list(
            (
                await session.scalars(
                    select(ToolObservation).where(ToolObservation.run_id == accepted["run_id"])
                )
            ).all()
        )
        capabilities = list(
            (
                await session.scalars(
                    select(PolicyCapabilityInvocation).where(
                        PolicyCapabilityInvocation.run_id == accepted["run_id"],
                        PolicyCapabilityInvocation.capability_name
                        == EXPECTED_CAPABILITY[action_type],
                    )
                )
            ).all()
        )
        capability_results = list(
            (
                await session.scalars(
                    select(PolicyCapabilityResult).where(
                        PolicyCapabilityResult.run_id == accepted["run_id"]
                    )
                )
            ).all()
        )
        proposals = list(
            (
                await session.scalars(
                    select(ProposalRecord).where(ProposalRecord.run_id == accepted["run_id"])
                )
            ).all()
        )
        approvals = list(
            (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.tenant_id == fixture["tenant_id"])
                )
            ).all()
        )
        same_run = [item for item in approvals if item.run_id == accepted["run_id"]]
        exact = [
            item
            for item in same_run
            if item.ticket_id == accepted["ticket_id"]
            and item.action_type == action_type
            and _database_approval_resource_id(item) == fixture["resource_id"]
        ]
        approval_ids = [item.id for item in exact]
        snapshots = list(
            (
                await session.scalars(
                    select(ApprovalSnapshot).where(
                        ApprovalSnapshot.approval_id.in_(approval_ids or ["<none>"])
                    )
                )
            ).all()
        )
        revisions = list(
            (
                await session.scalars(
                    select(ApprovalActionRevision).where(
                        ApprovalActionRevision.approval_id.in_(approval_ids or ["<none>"])
                    )
                )
            ).all()
        )
        markers = list(
            (
                await session.scalars(
                    select(CheckpointCommitMarker).where(
                        CheckpointCommitMarker.run_id == accepted["run_id"]
                    )
                )
            ).all()
        )
        finalizers = list(
            (
                await session.scalars(
                    select(FinalizerPayload)
                    .where(FinalizerPayload.job_id == accepted["job_id"])
                    .order_by(FinalizerPayload.created_at)
                )
            ).all()
        )
        finalizer_states = [
            item.full_payload.get("state_delta", {}).get("state", {}) for item in finalizers
        ]
        finalizer_state: dict[str, Any] = next(
            (item for item in reversed(finalizer_states) if item.get("candidate")), {}
        )
        candidate_action = finalizer_state.get("candidate", {}).get("action")
        policy_routes = [
            str(item.payload.get("route"))
            for item in events
            if item.event_type == "policy_decision" and item.payload.get("route")
        ]

        fixture_valid = False
        resource: Any
        if action_type == "refund":
            resource = await session.get(BillingRecord, fixture["resource_id"])
            fixture_valid = bool(
                resource is not None and resource.version == fixture["resource_version"]
            )
        elif action_type == "api_key_revocation":
            resource = await session.scalar(
                select(ApiKeyMetadata).where(
                    ApiKeyMetadata.tenant_id == fixture["tenant_id"],
                    ApiKeyMetadata.key_id == fixture["resource_id"],
                )
            )
            fixture_valid = bool(
                resource is not None and resource.version == fixture["resource_version"]
            )
        else:
            resource = await session.get(Subscription, fixture["resource_id"])
            fixture_valid = bool(
                resource is not None and resource.version == fixture["resource_version"]
            )

        return {
            "fixture_valid": fixture_valid,
            "candidate_action": candidate_action,
            "policy_route": policy_routes[-1] if policy_routes else None,
            "job": {
                "id": accepted["job_id"],
                "status": job.status if job else "missing",
                "attempt": job.attempt if job else None,
                "outcome": job.outcome if job else None,
                "last_error": job.last_error if job else None,
            },
            "run": {
                "id": accepted["run_id"],
                "status": run.status if run else "missing",
                "finish_reason": run.agent_finish_reason if run else None,
                "checkpoint_stage": run.checkpoint_stage if run else None,
                "checkpoint_id": run.canonical_checkpoint_id if run else None,
                "checkpoint_hash": run.canonical_checkpoint_hash if run else None,
            },
            "tools": [
                {
                    "id": item.id,
                    "name": item.tool_name,
                    "lifecycle": item.lifecycle,
                    "outcome": item.outcome,
                }
                for item in invocations
            ],
            "observations": [
                {
                    "id": item.id,
                    "invocation_id": item.invocation_id,
                    "status": item.status,
                    "error_code": item.payload.get("error_code"),
                    "content_hash": item.content_hash,
                    "source_ids": sorted(
                        str(source.get("source_id"))
                        for source in item.payload.get("source_refs", [])
                        if source.get("source_id")
                    ),
                }
                for item in observations
            ],
            "capabilities": [
                {
                    "id": item.id,
                    "name": item.capability_name,
                    "status": item.status,
                    "error_code": item.error_code,
                }
                for item in capabilities
            ],
            "capability_results": [
                {
                    "invocation_id": item.invocation_id,
                    "status": item.status,
                    "payload_hash": item.payload_hash,
                }
                for item in capability_results
            ],
            "proposals": [
                {
                    "id": item.id,
                    "action_type": item.action_type,
                    "resource_id": item.resource_id,
                    "resource_version": item.resource_version,
                    "status": item.status,
                    "action_hash": item.action_hash,
                }
                for item in proposals
            ],
            "approval_counts": {
                "exact": len(exact),
                "same_run": len(same_run),
                "same_tenant": len(approvals),
            },
            "approvals": [
                {
                    "id": item.id,
                    "ticket_id": item.ticket_id,
                    "run_id": item.run_id,
                    "action_type": item.action_type,
                    "resource_id": _database_approval_resource_id(item),
                    "business_version": item.business_version,
                    "status": item.status,
                    "proposal_id": item.proposal_id,
                    "marker_id": item.marker_id,
                    "selected_revision_id": item.selected_revision_id,
                }
                for item in approvals
            ],
            "snapshot_count": len(snapshots),
            "revision_count": len(revisions),
            "markers": [
                {
                    "id": item.id,
                    "status": item.status,
                    "segment_outcome": item.segment_outcome,
                    "checkpoint_id": item.final_checkpoint_id,
                    "checkpoint_hash": item.final_checkpoint_hash,
                }
                for item in markers
            ],
        }


async def _bounded_observation_b(
    admin_factory: async_sessionmaker[AsyncSession],
    *,
    accepted: dict[str, Any],
    fixture: dict[str, Any],
    action_type: str,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    history: list[dict[str, int]] = []
    latest: dict[str, Any] | None = None
    for _ in range(5):
        latest = await _authoritative_observation(
            admin_factory,
            accepted=accepted,
            fixture=fixture,
            action_type=action_type,
        )
        history.append(dict(latest["approval_counts"]))
        if latest["approval_counts"]["exact"]:
            break
        await asyncio.sleep(0.02)
    assert latest is not None
    return latest, history


async def _exercise_action(
    *,
    client: TestClient,
    dispatcher: OutboxDispatcher,
    worker: Any,
    admin_factory: async_sessionmaker[AsyncSession],
    action_type: str,
    fixture: dict[str, Any],
    message: str,
    expected_effect: Callable[[Any], bool],
    suffix: str,
) -> dict[str, Any]:
    customer_login = client.post(
        "/api/demo-sessions",
        json={
            "role": "customer",
            "customer_id": fixture["customer_id"],
            "tenant_id": fixture["tenant_id"],
            "external_subject": fixture["customer_subject"],
        },
    )
    assert customer_login.status_code == 200, customer_login.text
    customer_csrf = str(customer_login.json()["csrf_token"])
    accepted_response = client.post(
        "/api/tickets",
        headers={
            "X-CSRF-Token": customer_csrf,
            "Idempotency-Key": f"v1218-{action_type}-{suffix}",
        },
        json={"message": message},
    )
    assert accepted_response.status_code == 202, accepted_response.text
    accepted = accepted_response.json()
    assert accepted["reused"] is False
    assert await dispatcher.dispatch_once(batch_size=50) >= 1
    initial_job = await _wait_for_job(worker, admin_factory, accepted["job_id"])
    assert initial_job.status == "succeeded"

    observation_a = await _authoritative_observation(
        admin_factory,
        accepted=accepted,
        fixture=fixture,
        action_type=action_type,
    )
    observation_b, observation_b_history = await _bounded_observation_b(
        admin_factory,
        accepted=accepted,
        fixture=fixture,
        action_type=action_type,
    )

    approver_login = client.post(
        "/api/demo-sessions",
        json={
            "role": "approver",
            "tenant_id": fixture["tenant_id"],
            "external_subject": fixture["approver_subject"],
        },
    )
    assert approver_login.status_code == 200, approver_login.text
    approver_csrf = str(approver_login.json()["csrf_token"])
    approvals_response = client.get("/api/approvals")
    assert approvals_response.status_code == 200
    listed = approvals_response.json()
    listed_exact = [
        item
        for item in listed
        if item["ticket_id"] == accepted["ticket_id"]
        and item["action_type"] == action_type
        and item["resource_summary"] == fixture["resource_id"]
    ]
    projected: list[dict[str, Any]] = []
    for item in listed_exact:
        detail_response = client.get(f"/api/approvals/{item['id']}")
        assert detail_response.status_code == 200, detail_response.text
        projected.append(detail_response.json())
    exact = [
        item
        for item in projected
        if item["ticket_id"] == accepted["ticket_id"]
        and item["action_type"] == action_type
        and _resource_id(item) == fixture["resource_id"]
    ]
    db_exact = [
        item
        for item in observation_b["approvals"]
        if item["ticket_id"] == accepted["ticket_id"]
        and item["run_id"] == accepted["run_id"]
        and item["action_type"] == action_type
        and item["resource_id"] == fixture["resource_id"]
    ]
    db_contract_valid = bool(
        len(db_exact) == 1
        and db_exact[0]["status"] == "pending"
        and db_exact[0]["business_version"] == fixture["resource_version"]
        and db_exact[0]["proposal_id"]
        and db_exact[0]["marker_id"]
        and observation_b["snapshot_count"] == 1
        and observation_b["revision_count"] == 1
    )
    http_contract_valid = bool(
        len(exact) == 1
        and exact[0]["status"] == "pending"
        and exact[0]["actionable"] is True
        and exact[0]["business_version"] == fixture["resource_version"]
    )
    facts = IdentityConvergenceFacts(
        fixture_valid=observation_b["fixture_valid"],
        candidate_action=observation_b["candidate_action"],
        expected_candidate_action=EXPECTED_CANDIDATE_ACTION[action_type],
        policy_route=observation_b["policy_route"],
        capability_terminal_statuses=tuple(
            item["status"] for item in observation_b["capabilities"]
        ),
        db_exact_a=observation_a["approval_counts"]["exact"],
        db_same_run_a=observation_a["approval_counts"]["same_run"],
        db_same_tenant_a=observation_a["approval_counts"]["same_tenant"],
        db_exact_b=observation_b["approval_counts"]["exact"],
        db_same_run_b=observation_b["approval_counts"]["same_run"],
        db_same_tenant_b=observation_b["approval_counts"]["same_tenant"],
        http_exact=len(exact),
        http_total=len(listed),
        approval_contract_valid=db_contract_valid,
        http_contract_valid=http_contract_valid,
    )
    outcome = classify_identity_convergence(facts)
    diagnostic = {
        "schema_version": "identity-convergence-diagnostic.v1",
        "action_type": action_type,
        "expected": {
            "tenant_id": fixture["tenant_id"],
            "customer_id": fixture["customer_id"],
            "ticket_id": accepted["ticket_id"],
            "run_id": accepted["run_id"],
            "job_id": accepted["job_id"],
            "resource_id": fixture["resource_id"],
            "resource_version": fixture["resource_version"],
        },
        "approver_scope": {
            "tenant_id": fixture["tenant_id"],
            "principal_id": fixture["approver_subject"],
            "revalidated": approvals_response.status_code == 200,
        },
        "observation_a": observation_a,
        "observation_b": observation_b,
        "observation_b_history": observation_b_history,
        "http_projection": {
            "total": len(listed),
            "list_exact": len(listed_exact),
            "exact": len(exact),
            "contract_valid": http_contract_valid,
            "rows": [
                {
                    "id": item["id"],
                    "ticket_id": item["ticket_id"],
                    "origin_turn_id": item["origin_turn_id"],
                    "action_type": item["action_type"],
                    "resource_id": _resource_id(item),
                    "business_version": item["business_version"],
                    "status": item["status"],
                    "actionable": item["actionable"],
                }
                for item in projected
            ],
        },
        "facts": facts.model_dump(mode="json"),
        "outcome": outcome.value,
    }
    if outcome != IdentityConvergenceOutcome.CONTRACT_PASS:
        raise IdentityScenarioFailure(diagnostic)

    assert len(exact) == 1
    approval = exact[0]
    assert approval["status"] == "pending"
    assert approval["actionable"] is True
    assert approval["business_version"] == fixture["resource_version"]

    decision_key = f"v1218-approve-{action_type}-{suffix}"
    headers = {
        "X-CSRF-Token": approver_csrf,
        "Idempotency-Key": decision_key,
    }
    body = {"reason": "Exact identity and resource snapshot verified."}
    first, concurrent_replay = await asyncio.gather(
        asyncio.to_thread(
            client.post,
            f"/api/approvals/{approval['id']}/approve",
            headers=headers,
            json=body,
        ),
        asyncio.to_thread(
            client.post,
            f"/api/approvals/{approval['id']}/approve",
            headers=headers,
            json=body,
        ),
    )
    assert first.status_code == concurrent_replay.status_code == 202
    decisions = [first.json(), concurrent_replay.json()]
    assert {item["approval_id"] for item in decisions} == {approval["id"]}
    assert {item["ticket_id"] for item in decisions} == {accepted["ticket_id"]}
    assert {item["run_id"] for item in decisions} == {accepted["run_id"]}
    assert len({item["job_id"] for item in decisions}) == 1
    assert sorted(item["reused"] for item in decisions) == [False, True]
    resume_job_id = str(decisions[0]["job_id"])
    assert resume_job_id != accepted["job_id"]

    assert await dispatcher.dispatch_once(batch_size=50) >= 1
    resume_job = await _wait_for_job(worker, admin_factory, resume_job_id)
    assert resume_job.status == "succeeded"
    replay = client.post(
        f"/api/approvals/{approval['id']}/approve",
        headers=headers,
        json=body,
    )
    assert replay.status_code == 202
    assert replay.json() == {**decisions[0], "reused": True}

    async with admin_factory() as session:
        approval_row = await session.get(ApprovalRequest, approval["id"])
        run = await session.get(AgentRun, accepted["run_id"])
        assert approval_row is not None and approval_row.status == "executed"
        assert run is not None and run.status == "completed"
        actions = list(
            (
                await session.scalars(
                    select(BusinessAction).where(
                        BusinessAction.tenant_id == fixture["tenant_id"],
                        BusinessAction.approval_id == approval["id"],
                        BusinessAction.ticket_id == accepted["ticket_id"],
                        BusinessAction.customer_id == fixture["customer_id"],
                        BusinessAction.resource_id == fixture["resource_id"],
                    )
                )
            ).all()
        )
        assert len(actions) == 1
        action = actions[0]
        assert action.status == "succeeded"
        assert action.effect_identity
        assert action.human_decision_id
        decision_count = await session.scalar(
            select(func.count(HumanDecision.id)).where(
                HumanDecision.tenant_id == fixture["tenant_id"],
                HumanDecision.approval_id == approval["id"],
                HumanDecision.id == action.human_decision_id,
            )
        )
        assert int(decision_count or 0) == 1
        resource: Any
        if action_type == "refund":
            resource = await session.get(BillingRecord, fixture["resource_id"])
        elif action_type == "api_key_revocation":
            resource = await session.scalar(
                select(ApiKeyMetadata).where(
                    ApiKeyMetadata.tenant_id == fixture["tenant_id"],
                    ApiKeyMetadata.key_id == fixture["resource_id"],
                )
            )
        else:
            resource = await session.get(Subscription, fixture["resource_id"])
        assert resource is not None and expected_effect(resource)

        invocations = list(
            (
                await session.scalars(
                    select(ToolInvocation).where(ToolInvocation.run_id == accepted["run_id"])
                )
            ).all()
        )
        observations = list(
            (
                await session.scalars(
                    select(ToolObservation).where(ToolObservation.run_id == accepted["run_id"])
                )
            ).all()
        )
        assert len(invocations) == len(observations) >= 2
        assert {item.invocation_id for item in observations} == {item.id for item in invocations}
        assert all(item.lifecycle == "terminal" for item in invocations)
        assert all(item.status == "ok" for item in observations)
        assert await session.scalar(
            select(func.count(ContextMembership.id)).where(
                ContextMembership.run_id == accepted["run_id"]
            )
        )
        assert await session.scalar(
            select(func.count(CitationBinding.id)).where(
                CitationBinding.run_id == accepted["run_id"]
            )
        )
        events = list(
            (
                await session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == accepted["run_id"])
                    .order_by(AgentEvent.run_sequence)
                )
            ).all()
        )
        event_types = [item.event_type for item in events]
        assert event_types.count("agent_decision") == 1
        assert event_types.count("evidence_synthesized") == 1
        assert event_types.count("action_candidate_assembled") == 1
        first_decision = event_types.index("agent_decision")
        observation_index = event_types.index("tool_observation")
        synthesis_index = event_types.index("evidence_synthesized")
        assembly_index = event_types.index("action_candidate_assembled")
        policy_index = event_types.index("policy_decision")
        final_index = len(event_types) - 1 - event_types[::-1].index("final_outcome")
        assert (
            first_decision
            < observation_index
            < synthesis_index
            < assembly_index
            < policy_index
            < final_index
        )
        assert events[first_decision].payload["decision_type"] == "tool_calls"
        assert events[policy_index].payload["route"] == "await_human_approval"
        assert events[final_index].payload["terminal_state"] == "resolved"
        markers = list(
            (
                await session.scalars(
                    select(CheckpointCommitMarker).where(
                        CheckpointCommitMarker.run_id == accepted["run_id"]
                    )
                )
            ).all()
        )
        assert len(markers) == 2
        assert all(marker.status == "finalized" for marker in markers)
    return diagnostic


async def _exercise_refunded_terminal_follow_up(
    *,
    client: TestClient,
    dispatcher: OutboxDispatcher,
    worker: Any,
    admin_factory: async_sessionmaker[AsyncSession],
    fixture: dict[str, Any],
    suffix: str,
) -> dict[str, Any]:
    customer_login = client.post(
        "/api/demo-sessions",
        json={
            "role": "customer",
            "customer_id": fixture["customer_id"],
            "tenant_id": fixture["tenant_id"],
            "external_subject": fixture["customer_subject"],
        },
    )
    assert customer_login.status_code == 200, customer_login.text
    csrf = str(customer_login.json()["csrf_token"])
    first = client.post(
        "/api/tickets",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"v1510-terminal-clarify-{suffix}",
        },
        json={"message": "请帮我退款。"},
    )
    assert first.status_code == 202, first.text
    first_accepted = first.json()
    assert await dispatcher.dispatch_once(batch_size=50) >= 1
    first_job = await _wait_for_job(
        worker,
        admin_factory,
        first_accepted["job_id"],
    )
    assert first_job.status == "succeeded"

    second = client.post(
        f"/api/tickets/{first_accepted['ticket_id']}/messages",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"v1510-terminal-resource-{suffix}",
        },
        json={"message": (f"账单 ID 是 {fixture['resource_id']}，请继续按政策退款。")},
    )
    assert second.status_code == 202, second.text
    accepted = second.json()
    assert accepted["run_id"] != first_accepted["run_id"]
    assert await dispatcher.dispatch_once(batch_size=50) >= 1
    second_job = await _wait_for_job(worker, admin_factory, accepted["job_id"])
    assert second_job.status == "succeeded"

    detail_response = client.get(f"/api/tickets/{accepted['ticket_id']}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["status"] == "resolved"
    assert "已经退款" in detail["final_response"]
    assert "不会再次创建退款申请" in detail["final_response"]
    assert "没有创建审批" in detail["final_response"]

    async with admin_factory() as session:
        run = await session.get(AgentRun, accepted["run_id"])
        assert run is not None
        assert run.status == "completed"
        assert run.agent_finish_reason == "terminal_business_outcome"
        proposal_count = await session.scalar(
            select(func.count(ProposalRecord.id)).where(ProposalRecord.run_id == accepted["run_id"])
        )
        approval_count = await session.scalar(
            select(func.count(ApprovalRequest.id)).where(
                ApprovalRequest.run_id == accepted["run_id"]
            )
        )
        action_count = await session.scalar(
            select(func.count(BusinessAction.id)).where(
                BusinessAction.ticket_id == accepted["ticket_id"]
            )
        )
        capability_count = await session.scalar(
            select(func.count(PolicyCapabilityInvocation.id)).where(
                PolicyCapabilityInvocation.run_id == accepted["run_id"]
            )
        )
        assert int(proposal_count or 0) == 0
        assert int(approval_count or 0) == 0
        assert int(action_count or 0) == 0
        assert int(capability_count or 0) == 0
        events = list(
            (
                await session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == accepted["run_id"])
                    .order_by(AgentEvent.run_sequence)
                )
            ).all()
        )
        event_types = [event.event_type for event in events]
        assert event_types.count("terminal_business_outcome_derived") == 1
        assert event_types.count("terminal_business_outcome_projected") == 1
        assert event_types.count("proposal_drafted") == 0
        terminal_event = next(
            event for event in events if event.event_type == "terminal_business_outcome_derived"
        )
        assert terminal_event.payload["outcome_code"] == ("refund_status_not_actionable")
        assistant = await session.scalar(
            select(TicketMessage)
            .where(
                TicketMessage.run_id == accepted["run_id"],
                TicketMessage.role == "assistant",
            )
            .order_by(TicketMessage.conversation_sequence.desc())
            .limit(1)
        )
        assert assistant is not None
        assert {item.get("source_id") for item in assistant.source_refs} == {
            f"billing_record:{fixture['resource_id']}"
        }
    return {
        "ticket_id": accepted["ticket_id"],
        "run_id": accepted["run_id"],
        "outcome_code": "refund_status_not_actionable",
        "proposal_count": 0,
        "approval_count": 0,
        "action_count": 0,
    }


@pytest.mark.asyncio
async def test_three_actions_bind_exact_http_runtime_and_resource_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_REDIS_URL are required")

    suffix = uuid4().hex[:12]
    stream = f"supportguard:test:v1213:identity:{suffix}"
    group = f"supportguard-v1213-{suffix}"
    api_database_url = _database_url(database_url, "supportguard_api")
    dispatcher_database_url = _database_url(database_url, "supportguard_dispatcher")
    worker_database_url = _database_url(database_url, "supportguard_worker")
    read_database_url = _database_url(database_url, "supportguard_read_mcp")
    action_database_url = _database_url(database_url, "supportguard_action_mcp")
    api_redis_url = _redis_url(redis_url, "api", "api_dev")
    dispatcher_redis_url = _redis_url(redis_url, "dispatcher", "dispatcher_dev")
    worker_redis_url = _redis_url(redis_url, "worker", "worker_dev")

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.setenv("ASYNC_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("DEMO_FAKE_PROVIDER", "true")
    monkeypatch.setenv("DATABASE_URL", api_database_url)
    monkeypatch.setenv("REDIS_URL", api_redis_url)
    monkeypatch.setenv("REDIS_STREAM", stream)
    monkeypatch.setenv("MCP_READ_DATABASE_URL", read_database_url)
    monkeypatch.setenv("MCP_ACTION_DATABASE_URL", action_database_url)
    monkeypatch.setenv("APP_SECRET_KEY", f"v1213-test-secret-{suffix}")
    monkeypatch.setenv("INTERNAL_API_TOKEN", f"v1213-internal-{suffix}")
    monkeypatch.setenv("MAX_DURABLE_BACKLOG", "100000")
    get_settings.cache_clear()

    seed = _fixture_seed()
    fixtures = {
        action_type: await seed(action_type)
        for action_type in ("refund", "api_key_revocation", "entitlement_change")
    }
    messages = {
        "refund": lambda item: f"{item['resource_id']} 是重复扣费，请按政策退款",
        "api_key_revocation": lambda item: (
            f"{item['resource_id']} 疑似泄露，请立即撤销这个 API Key"
        ),
        "entitlement_change": lambda item: (
            f"请把订阅 {item['resource_id']} 的并发配额从当前值明确提升到 60"
        ),
    }
    expected_effects: dict[str, Callable[[Any], bool]] = {
        "refund": lambda item: item.status == "refunded" and item.version == 3,
        "api_key_revocation": lambda item: item.status == "revoked" and item.version == 3,
        "entitlement_change": lambda item: item.concurrency_limit == 60 and item.version == 4,
    }

    admin_engine = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    dispatcher_engine = create_async_engine(dispatcher_database_url)
    dispatcher_factory = async_sessionmaker(dispatcher_engine, expire_on_commit=False)
    dispatcher_redis = Redis.from_url(dispatcher_redis_url, decode_responses=False)
    cleanup_redis = Redis.from_url(
        _redis_url(redis_url, "integration", "integration_dev"),
        decode_responses=False,
    )
    worker_settings = Settings(
        _env_file=None,
        app_env="development",
        auth_mode="development",
        async_runtime_enabled=True,
        demo_fake_provider=True,
        database_url=worker_database_url,
        redis_url=worker_redis_url,
        redis_stream=stream,
        redis_consumer_group=group,
        service_instance_id=f"v1213-worker-{suffix}",
        mcp_read_database_url=read_database_url,
        mcp_action_database_url=action_database_url,
        code_version="v1.2.13-identity-bound-e2e",
    )

    try:
        with TestClient(create_app()) as client:
            dispatcher = OutboxDispatcher(
                dispatcher_factory,
                dispatcher_redis,
                stream=stream,
            )
            async with worker_runtime(worker_settings) as worker:
                diagnostics: list[dict[str, Any]] = []
                terminal_diagnostics: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                for action_type, fixture in fixtures.items():
                    try:
                        diagnostic = await _exercise_action(
                            client=client,
                            dispatcher=dispatcher,
                            worker=worker,
                            admin_factory=admin_factory,
                            action_type=action_type,
                            fixture=fixture,
                            message=messages[action_type](fixture),
                            expected_effect=expected_effects[action_type],
                            suffix=suffix,
                        )
                        diagnostics.append(diagnostic)
                        if action_type == "refund":
                            terminal_diagnostics.append(
                                await _exercise_refunded_terminal_follow_up(
                                    client=client,
                                    dispatcher=dispatcher,
                                    worker=worker,
                                    admin_factory=admin_factory,
                                    fixture=fixture,
                                    suffix=suffix,
                                )
                            )
                    except IdentityScenarioFailure as exc:
                        failures.append(exc.snapshot)
                    except Exception as exc:
                        failures.append(
                            {
                                "schema_version": "identity-convergence-diagnostic.v1",
                                "action_type": action_type,
                                "outcome": "diagnostic_ambiguous",
                                "stable_error_code": type(exc).__name__,
                            }
                        )
                attempt_payload = {
                    "schema_version": "identity-convergence-attempt.v1",
                    "attempt_id": suffix,
                    "workflow_count": len(fixtures),
                    "contract_pass_count": len(diagnostics),
                    "terminal_contract_pass_count": len(terminal_diagnostics),
                    "failure_count": len(failures),
                    "diagnostics": diagnostics,
                    "terminal_diagnostics": terminal_diagnostics,
                    "failures": failures,
                }
                _write_external_diagnostic(attempt_payload)
                assert not failures, json.dumps(
                    attempt_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
    finally:
        await cleanup_redis.delete(stream)
        await dispatcher_redis.aclose()
        await cleanup_redis.aclose()
        await dispatcher_engine.dispose()
        await admin_engine.dispose()
        get_settings.cache_clear()
