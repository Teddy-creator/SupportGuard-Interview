import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from current_predicate_facts import record_predicate_operands
from supportguard.agent.graph import AgentState
from supportguard.api.auth import Principal, PrincipalResolution, issue_token
from supportguard.api.contracts import ApprovalInput, RejectionInput
from supportguard.api.projections import (
    _approval_allowed_actions,
    _bounded_ticket_payload,
    _public_event_projection,
)
from supportguard.config import get_settings
from supportguard.contracts.context import WorkerExecutionContext
from supportguard.contracts.timestamps import (
    CANONICAL_UTC_TIMESTAMP_EXAMPLE,
    CANONICAL_UTC_TIMESTAMP_PATTERN,
)
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    ApprovalRequest,
    ApprovalSnapshot,
    ProposalRecord,
    RuntimeJob,
    SupportTicket,
    TicketMessage,
    new_id,
)
from supportguard.main import create_app
from supportguard.mcp.test_transport import InProcessTestToolTransport
from supportguard.services.approval_commands import DecisionAccepted
from supportguard.services.runtime_jobs import RuntimeConflict


def test_approval_reason_is_optional_but_rejection_reason_is_required() -> None:
    assert ApprovalInput(reason="").reason == ""
    assert ApprovalInput().reason == ""
    with pytest.raises(ValidationError):
        ApprovalInput(reason="x")
    assert RejectionInput(reason="事实不满足政策").reason == "事实不满足政策"
    with pytest.raises(ValidationError):
        RejectionInput(reason="")


def test_bounded_ticket_projection_derives_one_published_index_version() -> None:
    payload = _bounded_ticket_payload(
        {
            "messages": [],
            "timeline": [],
            "business_facts": [],
            "knowledge_sources": [
                {"chunk_id": "chunk-1", "index_version": "kb-current"},
                {"chunk_id": "chunk-2", "index_version": "kb-current"},
            ],
            "latest_run": {"id": "run-1", "knowledge_index_version": None},
        }
    )

    assert payload["latest_run"]["knowledge_index_version"] == "kb-current"


def test_public_event_projection_is_a_strict_allowlist() -> None:
    projected = _public_event_projection(
        {
            "id": "event_public",
            "event_type": "tool_observation",
            "payload": {
                "tool_name": "search_knowledge",
                "source_count": 2,
                "raw_payload": "<html>private upstream</html>",
                "error_code": "provider_private_error",
                "prompt_hash": "private-prompt-hash",
                "job_id": "job_private",
            },
            "run_id": "run_public",
            "ticket_sequence": 4,
            "run_sequence": 3,
            "step_index": 2,
            "tool_round": 1,
            "tool_call_id": "call_private",
            "status": "completed",
            "created_at": "2026-07-28T00:00:00+00:00",
        }
    )

    assert projected == {
        "id": "event_public",
        "event_type": "tool_observation",
        "payload": {"tool_name": "search_knowledge", "source_count": 2},
        "run_id": "run_public",
        "ticket_sequence": 4,
        "run_sequence": 3,
        "step_index": 2,
        "tool_round": 1,
        "status": "completed",
        "created_at": "2026-07-28T00:00:00+00:00",
    }
    assert "private" not in str(projected)


def test_inspector_projection_does_not_classify_null_errors_as_failures() -> None:
    projected = _public_event_projection(
        {
            "id": "event_success",
            "event_type": "tool_observation",
            "payload": {
                "tool_name": "query_billing_record",
                "error_code": None,
                "failure_recorded": False,
            },
            "run_id": "run_success",
            "ticket_sequence": 1,
            "run_sequence": 1,
            "step_index": 1,
            "tool_round": 1,
            "status": "succeeded",
            "created_at": "2026-08-13T00:00:00Z",
        },
        inspector=True,
    )

    assert projected["payload"] == {"tool_name": "query_billing_record"}


@pytest.mark.parametrize("event_id", [None, 1, "", "x" * 65])
def test_public_event_projection_rejects_invalid_durable_identity(
    event_id: object,
) -> None:
    event = {
        "id": event_id,
        "event_type": "run_started",
        "payload": {},
        "run_id": "run_public",
        "ticket_sequence": 1,
        "run_sequence": 1,
        "step_index": 1,
        "tool_round": 0,
        "status": "completed",
        "created_at": "2026-07-28T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="public_event_identity_invalid"):
        _public_event_projection(event)


def test_public_event_projection_rejects_missing_durable_identity() -> None:
    event = {
        "event_type": "run_started",
        "payload": {},
        "run_id": "run_public",
        "ticket_sequence": 1,
        "run_sequence": 1,
        "step_index": 1,
        "tool_round": 0,
        "status": "completed",
        "created_at": "2026-07-28T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="public_event_identity_invalid"):
        _public_event_projection(event)


def test_bounded_ticket_projection_strips_internal_trace_and_action_payloads() -> None:
    payload = _bounded_ticket_payload(
        {
            "messages": [
                {
                    "id": "message_1",
                    "role": "assistant",
                    "content": "Safe answer",
                    "source_refs": [{"secret": "message-ledger"}],
                    "created_at": "2026-07-28T00:00:00+00:00",
                }
            ],
            "summary": {
                "confirmed_facts": ["safe"],
                "source_refs": [{"secret": "summary-ledger"}],
            },
            "timeline": [
                {
                    "id": "event_1",
                    "event_type": "tool_observation",
                    "run_id": "run_1",
                    "ticket_sequence": 2,
                    "run_sequence": 2,
                    "step_index": 2,
                    "tool_round": 1,
                    "status": "completed",
                    "created_at": "2026-07-28T00:00:00+00:00",
                    "payload": {
                        "tool_name": "query_account",
                        "source_count": 1,
                        "raw_arguments": {"api_key": "sk-secret"},
                        "data": {"email": "private@example.com"},
                    },
                }
            ],
            "knowledge_sources": [],
            "business_facts": [
                {
                    "tool_name": "query_account",
                    "status": "ok",
                    "source_refs": [{"secret": "fact-ledger"}],
                    "data": {
                        "status": "active",
                        "email": "private@example.com",
                        "api_key": "sk-secret",
                    },
                }
            ],
            "business_action": {
                "id": "action_1",
                "status": "succeeded",
                "action_type": "refund",
                "resource_id": "bill_1",
                "resource_version": 2,
                "result": {"processor_token": "private"},
            },
            "latest_run": {
                "id": "run_1",
                "ticket_id": "ticket_1",
                "status": "completed",
                "status_version": 2,
                "model": "deepseek-v4-flash",
                "provider_mode": "native",
                "tool_call_mode": "native",
                "configured_runtime": {
                    "model": "deepseek-v4-flash",
                    "provider_mode": "native",
                    "tool_call_mode": "native",
                    "source": "command_acceptance",
                    "secret": "configured-secret",
                },
                "actual_runtime": {
                    "model": "deepseek-v4-flash",
                    "provider_mode": "native",
                    "attempt_id": "attempt_1",
                    "attempt_status": "succeeded",
                    "source": "agent_call_attempt",
                    "prompt_hash": "prompt-hash-private",
                    "schema_hash": "schema-hash-private",
                    "runtime_manifest_hash": "runtime-hash-private",
                    "embedding_fingerprint": "embedding-private",
                    "code_commit": "commit-private",
                    "raw_response": "private",
                },
                "error_code": "provider_private_timeout",
                "budgets": {"tool_rounds": 1, "tool_attempts": 1, "llm_calls": 2},
                "created_at": "2026-07-28T00:00:00+00:00",
                "job": {
                    "id": "job_1",
                    "kind": "agent_start",
                    "status": "succeeded",
                    "attempt": 1,
                    "outcome": "failed:PrivateRuntimeException",
                    "last_error": "private stack",
                },
            },
        }
    )

    serialized = str(payload)
    for secret in (
        "message-ledger",
        "summary-ledger",
        "sk-secret",
        "private@example.com",
        "fact-ledger",
        "processor_token",
        "configured-secret",
        "prompt-hash-private",
        "schema-hash-private",
        "runtime-hash-private",
        "embedding-private",
        "commit-private",
        "provider_private_timeout",
        "PrivateRuntimeException",
        "raw_response",
        "private stack",
    ):
        assert secret not in serialized
    assert payload["business_facts"] == [
        {
            "tool_name": "query_account",
            "status": "ok",
            "fact_summary": {"status": "active"},
        }
    ]
    assert payload["timeline"][0]["payload"] == {
        "tool_name": "query_account",
        "source_count": 1,
    }
    assert payload["latest_run"]["failure_category"] == "provider"
    assert set(payload["latest_run"]["job"]) == {
        "status",
        "outcome",
        "has_error",
    }
    for internal_key in (
        "attempt_id",
        "job_id",
        "prompt_hash",
        "schema_hash",
        "runtime_manifest_hash",
        "embedding_fingerprint",
        "code_commit",
        "error_code",
        "last_error",
    ):
        assert internal_key not in serialized


def test_approval_projection_separates_action_authorization_from_conversation_takeover() -> None:
    assert _approval_allowed_actions("refund", actionable=True) == [
        "approve",
        "edit_and_approve",
        "reject",
    ]
    assert _approval_allowed_actions("api_key_revocation", actionable=True) == [
        "approve",
        "reject",
    ]
    assert _approval_allowed_actions("entitlement_change", actionable=True) == [
        "approve",
        "edit_and_approve",
        "reject",
    ]
    assert _approval_allowed_actions("refund", actionable=False) == []


def session(client: TestClient, role: str, customer_id: str | None = None) -> str:
    response = client.post("/api/demo-sessions", json={"role": role, "customer_id": customer_id})
    assert response.status_code == 200
    assert response.cookies.get("supportguard_session")
    assert response.cookies.get("supportguard_session") not in response.text
    return str(response.json()["csrf_token"])


def run_accepted_ticket(client: TestClient, payload: dict[str, str], message: str) -> dict:
    async def execute() -> dict:
        trace_id = f"trace_{payload['run_id']}"
        context = WorkerExecutionContext(
            tenant_id="tenant_demo",
            actor_principal_id="cust_demo",
            executor_service_principal="test-runtime",
            customer_id="cust_demo",
            ticket_id=payload["ticket_id"],
            run_id=payload["run_id"],
            job_id=payload["job_id"],
            segment_id="test-api-segment",
            delivery_generation=1,
            fencing_token=1,
            trace_id=trace_id,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        async with client.app.state.runtime.scoped_factory.worker(context) as db:
            ticket = await db.get(SupportTicket, payload["ticket_id"])
            run = await db.get(AgentRun, payload["run_id"])
            job = await db.get(RuntimeJob, payload["job_id"])
            assert ticket is not None and run is not None and job is not None
            ticket.status = "running"
            run.status = "running"
            job.status = "leased"
            await db.commit()
        state = await client.app.state.runtime.run_ticket(
            AgentState(
                tenant_id="tenant_demo",
                ticket_id=payload["ticket_id"],
                customer_id="cust_demo",
                run_id=payload["run_id"],
                trace_id=trace_id,
                user_message=message,
            ),
            execution_context=context,
        )
        async with client.app.state.runtime.scoped_factory.worker(context) as db:
            db.add(
                AgentCallAttempt(
                    tenant_id="tenant_demo",
                    run_id=payload["run_id"],
                    job_id=payload["job_id"],
                    fencing_token=1,
                    call_kind="llm",
                    ordinal=1,
                    status="succeeded",
                    runtime_provenance={
                        "model": "deterministic-fake",
                        "provider_mode": "fake",
                        "tool_call_mode": "native_fixture",
                        "context_version": "test",
                        "code_version": "test",
                    },
                )
            )
            await db.commit()
        return state

    return client.portal.call(execute)


def test_customer_and_approver_sessions_are_role_isolated() -> None:
    with TestClient(create_app(testing=True)) as client:
        customer_csrf = session(client, "customer", "cust_demo")
        tickets = client.get("/api/tickets")
        forbidden = client.get("/api/approvals")
        missing_csrf = client.post("/api/tickets", json={"message": "hello"})
        session(client, "approver")
        approvals = client.get("/api/approvals")

    assert tickets.status_code == 200
    assert forbidden.status_code == 403
    assert missing_csrf.status_code == 403
    assert customer_csrf
    assert approvals.status_code == 200


def test_conversation_projection_persists_turns_and_is_server_searchable() -> None:
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        created = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "conversation-create-1"},
            json={"message": "独特检索词：并发限制排查"},
        )
        assert created.status_code == 202, created.text
        conversation_id = created.json()["ticket_id"]
        listing = client.get("/api/conversations", params={"query": "独特检索词"})
        detail = client.get(f"/api/conversations/{conversation_id}")

    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["items"]] == [conversation_id]
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["lifecycle"] == "active"
    assert payload["automation_mode"] == "agent"
    assert payload["allowed_actions"] == ["append_message", "archive"]
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["messages"][0]["kind"] == "customer"


def test_conversation_list_uses_visible_message_activity_not_internal_updates() -> None:
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        first = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "activity-first"},
            json={"message": "第一条较早对话"},
        ).json()["ticket_id"]
        second = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "activity-second"},
            json={"message": "第二条较新对话"},
        ).json()["ticket_id"]

        async def arrange_clocks() -> None:
            async with client.app.state.factory() as db:
                first_ticket = await db.get(SupportTicket, first)
                second_ticket = await db.get(SupportTicket, second)
                assert first_ticket is not None and second_ticket is not None
                now = datetime.now(UTC)
                first_ticket.last_message_at = now - timedelta(minutes=2)
                second_ticket.last_message_at = now - timedelta(minutes=1)
                first_ticket.updated_at = now + timedelta(days=1)
                await db.commit()

        client.portal.call(arrange_clocks)
        before = client.get("/api/conversations").json()["items"]
        assert [item["id"] for item in before[:2]] == [second, first]

        appended = client.post(
            f"/api/conversations/{first}/messages",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "activity-append"},
            json={"message": "第一条对话的新客户消息"},
        )
        assert appended.status_code == 202, appended.text
        after = client.get("/api/conversations").json()["items"]
        assert [item["id"] for item in after[:2]] == [first, second]


def test_session_context_exposes_server_scoped_identity_and_account() -> None:
    with TestClient(create_app(testing=True)) as client:
        session(client, "customer", "cust_demo")
        customer = client.get("/api/session")
        session(client, "approver")
        approver = client.get("/api/session")

    assert customer.status_code == 200, customer.text
    customer_payload = customer.json()
    assert customer_payload["auth_mode"] == "development"
    assert isinstance(customer_payload["csrf_token"], str)
    assert customer_payload["csrf_token"]
    assert customer_payload["principal"] == {
        "id": "user_customer_demo",
        "display_name": "Aster Customer",
        "role": "customer",
        "membership_role": "customer_admin",
    }
    assert customer_payload["active_tenant"]["name"] == "Aster Labs"
    assert customer_payload["customer"]["id"] == "cust_demo"
    assert customer_payload["subscription"]["plan"] == "pro"
    assert customer_payload["configured_runtime"] == {
        "mode": "fake",
        "model": "deterministic-fake",
        "actual_run_source": "ticket.latest_run",
    }

    assert approver.status_code == 200, approver.text
    approver_payload = approver.json()
    assert approver_payload["principal"]["id"] == "user_approver_demo"
    assert approver_payload["principal"]["role"] == "approver"
    assert approver_payload["customer"] is None
    assert approver_payload["subscription"] is None
    assert [item["id"] for item in approver_payload["accessible_tenants"]] == [
        "tenant_demo",
        "tenant_other",
    ]


def test_two_app_instances_use_their_own_lifecycle_settings() -> None:
    development_app = create_app(testing=True)
    production_app = create_app(testing=True)

    with (
        TestClient(development_app) as development,
        TestClient(production_app) as production,
    ):
        development_app.state.settings = development_app.state.settings.model_copy(
            update={"auth_mode": "development", "internal_api_token": SecretStr("token-a")}
        )
        production_app.state.settings = production_app.state.settings.model_copy(
            update={"auth_mode": "production", "internal_api_token": SecretStr("token-b")}
        )

        demo_allowed = development.post(
            "/api/demo-sessions",
            json={"role": "customer", "customer_id": "cust_demo"},
        )
        demo_hidden = production.post(
            "/api/demo-sessions",
            json={"role": "customer", "customer_id": "cust_demo"},
        )
        development_internal = development.get(
            "/internal/metrics",
            headers={"X-Internal-Token": "token-a"},
        )
        crossed_token = development.get(
            "/internal/metrics",
            headers={"X-Internal-Token": "token-b"},
        )
        production_internal = production.get(
            "/internal/metrics",
            headers={"X-Internal-Token": "token-b"},
        )

    assert demo_allowed.status_code == 200
    assert demo_hidden.status_code == 404
    assert development_internal.status_code == 200
    assert crossed_token.status_code == 404
    assert production_internal.status_code == 200


def test_app_runtime_modules_do_not_read_process_global_settings() -> None:
    lifecycle_owned_modules = [
        "backend/src/supportguard/api/auth.py",
        "backend/src/supportguard/api/routes.py",
        "backend/src/supportguard/api/health.py",
        "backend/src/supportguard/services/commands.py",
        "backend/src/supportguard/services/attempts.py",
        "backend/src/supportguard/agent/graph.py",
        "backend/src/supportguard/agent/persistence.py",
        "backend/src/supportguard/runtime/app.py",
    ]

    offenders = [
        path
        for path in lifecycle_owned_modules
        if "get_settings" in Path(path).read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_public_api_validation_uses_safe_product_error_contract() -> None:
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        invalid = client.post(
            "/api/tickets",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "invalid-empty"},
            json={"message": ""},
        )

    assert invalid.status_code == 422
    assert invalid.json() == {
        "public_code": "invalid_request",
        "message": "提交内容不符合要求，请检查后重试。",
        "retryable": False,
        "request_id": invalid.headers["X-Request-ID"],
    }


def test_unknown_api_exception_is_replaced_with_safe_json() -> None:
    app = create_app(testing=True)
    poison = (
        "<html>502 private upstream</html> "
        "Traceback SELECT * FROM approval_snapshots Bearer secret-token"
    )

    @app.get("/api/test-unhandled-product-error")
    async def _raise_unhandled_error() -> None:
        raise RuntimeError(poison)

    with TestClient(app) as client:
        failed = client.get("/api/test-unhandled-product-error")

    assert failed.status_code == 500
    assert failed.json() == {
        "public_code": "internal_error",
        "message": "服务暂时遇到问题，请稍后重试。",
        "retryable": True,
        "request_id": failed.headers["X-Request-ID"],
    }
    for private_value in ("<html>", "Traceback", "SELECT", "Bearer", "secret-token"):
        assert private_value not in failed.text


def test_public_http_exception_keeps_internal_reason_in_logs_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(testing=True)
    poison = "approval_snapshot_private_hash_123"

    @app.get("/api/test-public-problem-boundary")
    async def _raise_known_error() -> None:
        raise HTTPException(status_code=409, detail=poison)

    caplog.set_level("WARNING", logger="supportguard.main")
    with TestClient(app) as client:
        failed = client.get("/api/test-public-problem-boundary")

    assert failed.status_code == 409
    assert failed.json() == {
        "public_code": "state_conflict",
        "message": "数据已发生变化，请刷新后重试。",
        "retryable": False,
        "request_id": failed.headers["X-Request-ID"],
    }
    assert poison not in failed.text
    assert any(getattr(record, "internal_reason", None) == poison for record in caplog.records)


def test_custom_demo_session_uses_the_same_fail_closed_principal_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def resolve(
        _session: object, *, subject: str, tenant_id: str
    ) -> PrincipalResolution | None:
        calls.append((subject, tenant_id))
        if subject == "e2e-customer" and tenant_id == "tenant-e2e":
            return PrincipalResolution(
                schema_version="principal-resolution.v1",
                role="customer",
                subject_id="user-e2e-customer",
                tenant_id=tenant_id,
                customer_id="cust-e2e",
                membership_role="customer_admin",
            )
        return None

    monkeypatch.setattr(
        "supportguard.api.endpoints.sessions.resolve_principal_capability",
        resolve,
    )
    with TestClient(create_app(testing=True)) as client:
        accepted = client.post(
            "/api/demo-sessions",
            json={
                "role": "customer",
                "customer_id": "cust-e2e",
                "tenant_id": "tenant-e2e",
                "external_subject": "e2e-customer",
            },
        )
        wrong_customer = client.post(
            "/api/demo-sessions",
            json={
                "role": "customer",
                "customer_id": "cust-other",
                "tenant_id": "tenant-e2e",
                "external_subject": "e2e-customer",
            },
        )
        unknown = client.post(
            "/api/demo-sessions",
            json={
                "role": "approver",
                "tenant_id": "tenant-e2e",
                "external_subject": "unknown-approver",
            },
        )
        partial = client.post(
            "/api/demo-sessions",
            json={"role": "customer", "tenant_id": "tenant-e2e"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["principal"] == {
        "role": "customer",
        "subject_id": "user-e2e-customer",
        "tenant_id": "tenant-e2e",
        "customer_id": "cust-e2e",
        "membership_role": "customer_admin",
        "csrf_token": accepted.json()["csrf_token"],
    }
    assert wrong_customer.status_code == 403
    assert unknown.status_code == 403
    assert partial.status_code == 422
    assert calls == [
        ("e2e-customer", "tenant-e2e"),
        ("e2e-customer", "tenant-e2e"),
        ("unknown-approver", "tenant-e2e"),
    ]


def test_request_correlation_and_prometheus_endpoint() -> None:
    with TestClient(create_app(testing=True)) as client:
        health = client.get("/api/health", headers={"X-Trace-ID": "trace_test_fixed"})
        public_metrics = client.get("/api/metrics")
        missing_token = client.get("/internal/metrics")
        metrics = client.get(
            "/internal/metrics",
            headers={"X-Internal-Token": "local-internal-health-token"},
        )

    assert health.headers["X-Trace-ID"] == "trace_test_fixed"
    assert health.headers["X-Request-ID"].startswith("request_")
    assert public_metrics.status_code == 404
    assert missing_token.status_code == 404
    assert metrics.status_code == 200
    assert "supportguard_http_requests_total" in metrics.text


def test_ticket_api_runs_test_owned_tool_observation_replan_vertical_slice() -> None:
    message = "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded"
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        response = client.post(
            "/api/tickets",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "api-diagnostic-1"},
            json={"message": message},
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        state = run_accepted_ticket(client, payload, message)
        detail = client.get(f"/api/tickets/{payload['ticket_id']}")
        events = client.get(f"/api/tickets/{payload['ticket_id']}/events")

    assert payload["status"] == "queued"
    assert payload["schema_version"] == "command-accepted.v1"
    assert set(payload) == {
        "schema_version",
        "ticket_id",
        "run_id",
        "job_id",
        "accepted_at",
        "status",
        "status_url",
        "events_url",
        "reused",
    }
    assert state["tool_rounds"] == 1
    assert state["tool_attempts"] == 3
    assert {item["tool_name"] for item in state["tool_observations"]} == {
        "search_knowledge",
        "query_subscription",
        "query_api_usage",
    }
    assert all(item["status"] == "ok" for item in state["tool_observations"])
    assert all(item.get("error_code") is None for item in state["tool_observations"])
    assert state["evidence"]
    assert detail.status_code == 200
    product_detail = detail.json()
    assert product_detail["status"] == "resolved"
    assert product_detail["appendable"] is False
    assert product_detail["allowed_actions"] == ["new_ticket_from_context"]
    assert product_detail["title"] == message
    assert product_detail["messages"][0]["role"] == "customer"
    assert product_detail["messages"][0]["content"] == message
    assert product_detail["latest_run"]["id"] == payload["run_id"]
    assert product_detail["latest_run"]["provider_mode"] == "fake"
    assert product_detail["latest_run"]["model"] == "deterministic-fake"
    assert product_detail["latest_run"]["configured_runtime"] == {
        "model": "deterministic-fake",
        "provider": None,
        "provider_mode": "fake",
        "tool_call_mode": "native_fixture",
        "prompt_version": None,
        "schema_version": None,
        "context_assembly_version": None,
        "knowledge_index_contract": None,
        "attempt_status": None,
        "source": "command_acceptance",
        "provider_transport_attempts": None,
        "provider_retry_count": None,
    }
    assert product_detail["latest_run"]["actual_runtime"]["provider_mode"] == "fake"
    assert product_detail["latest_run"]["actual_runtime"]["model"] == ("deterministic-fake")
    assert product_detail["latest_run"]["actual_runtime"]["source"] == ("agent_call_attempt")
    # The lightweight TestClient path intentionally skips production citation
    # ledger creation. Product projection must fail closed instead of exposing
    # unbound retrieval candidates as published answer sources.
    assert product_detail["knowledge_sources"] == []
    assert {item["tool_name"] for item in product_detail["business_facts"]} == {
        "query_subscription",
        "query_api_usage",
    }
    assert product_detail["timeline"]
    assert {item["run_id"] for item in product_detail["timeline"]} == {payload["run_id"]}
    assert events.status_code == 200
    event_payload = events.json()
    event_ids = [item["id"] for item in event_payload]
    assert event_ids
    assert len(event_ids) == len(set(event_ids))
    assert all(item.startswith("event_") for item in event_ids)
    assert {item["event_type"] for item in event_payload} >= {
        "agent_decision",
        "tool_observation",
        "policy_decision",
        "final_outcome",
    }
    serialized_events = events.text.lower()
    # Versioned provenance is intentionally public, but prompt bodies and
    # private reasoning must never cross the product API boundary.
    assert '"prompt":' not in serialized_events
    assert '"system_prompt":' not in serialized_events
    assert '"developer_prompt":' not in serialized_events
    assert '"chain_of_thought":' not in serialized_events
    assert '"reasoning_content":' not in serialized_events


def test_follow_up_projection_does_not_publish_the_previous_run_as_current() -> None:
    first_message = "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded"
    follow_up = "那当前并发具体是多少？"
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        first = client.post(
            "/api/tickets",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "projection-run-one"},
            json={"message": first_message},
        )
        assert first.status_code == 202, first.text
        first_payload = first.json()
        run_accepted_ticket(client, first_payload, first_message)
        completed = client.get(f"/api/tickets/{first_payload['ticket_id']}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["final_response"]

        second = client.post(
            f"/api/tickets/{first_payload['ticket_id']}/messages",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "projection-run-two"},
            json={"message": follow_up},
        )
        assert second.status_code == 202, second.text
        current = client.get(f"/api/tickets/{first_payload['ticket_id']}")

    assert current.status_code == 200, current.text
    detail = current.json()
    assert detail["status"] == "queued"
    assert detail["latest_run"]["id"] == second.json()["run_id"]
    assert detail["latest_run"]["actual_runtime"] is None
    assert detail["final_response"] is None
    assert detail["timeline"]
    assert {item["run_id"] for item in detail["timeline"]} == {second.json()["run_id"]}
    assert {item["event_type"] for item in detail["timeline"]} == {"run_started"}
    assert detail["knowledge_sources"] == []
    assert detail["business_facts"] == []


def test_ticket_detail_is_typed_bounded_and_query_only_for_long_conversations() -> None:
    message = "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded"
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        accepted = client.post(
            "/api/tickets",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "bounded-ticket"},
            json={"message": message},
        )
        assert accepted.status_code == 202, accepted.text
        payload = accepted.json()
        run_accepted_ticket(client, payload, message)

        context = WorkerExecutionContext(
            tenant_id="tenant_demo",
            actor_principal_id="cust_demo",
            executor_service_principal="test-runtime",
            customer_id="cust_demo",
            ticket_id=payload["ticket_id"],
            run_id=payload["run_id"],
            job_id=payload["job_id"],
            segment_id="bounded-projection",
            delivery_generation=1,
            fencing_token=1,
            trace_id="trace_bounded_projection",
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )

        async def count_history() -> int:
            async with client.app.state.runtime.scoped_factory.worker(context) as db:
                return int(
                    await db.scalar(
                        select(func.count(TicketMessage.id)).where(
                            TicketMessage.ticket_id == payload["ticket_id"]
                        )
                    )
                    or 0
                )

        async def add_history() -> int:
            anchor = datetime.now(UTC)
            async with client.app.state.runtime.scoped_factory.worker(context) as db:
                db.add_all(
                    [
                        TicketMessage(
                            id=new_id("msg"),
                            tenant_id="tenant_demo",
                            ticket_id=payload["ticket_id"],
                            role="customer",
                            content=f"历史补充 {index}",
                            created_at=anchor + timedelta(microseconds=index),
                            updated_at=anchor + timedelta(microseconds=index),
                        )
                        for index in range(105)
                    ]
                )
                await db.commit()
            return await count_history()

        total_before = client.portal.call(add_history)
        detail = client.get(f"/api/tickets/{payload['ticket_id']}")
        total_after = client.portal.call(count_history)
        openapi = client.get("/openapi.json")

    assert detail.status_code == 200, detail.text
    product = detail.json()
    assert len(product["messages"]) == 100
    assert product["aggregation"]["messages"] == {
        "limit": 100,
        "returned": 100,
        "total": 101,
        "total_is_exact": False,
        "has_more": True,
        "boundary": product["messages"][0]["id"],
    }
    assert total_after == total_before
    assert openapi.status_code == 200
    schema = openapi.json()["paths"]["/api/tickets/{ticket_id}"]["get"]["responses"]["200"]
    assert schema["content"]["application/json"]["schema"]["$ref"].endswith("/TicketDetailResponse")


def test_refund_projection_exposes_review_context_without_bypassing_hitl() -> None:
    message = "账单 bill_demo_duplicate 是重复扣费，请退款"
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        accepted = client.post(
            "/api/tickets",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "projection-refund"},
            json={"message": message},
        )
        assert accepted.status_code == 202, accepted.text
        run_accepted_ticket(client, accepted.json(), message)
        customer_ticket = client.get(f"/api/tickets/{accepted.json()['ticket_id']}")
        follow_up = client.post(
            f"/api/conversations/{accepted.json()['ticket_id']}/messages",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "projection-refund-follow-up"},
            json={"message": "补充：请不要把后续消息混入原审批来源。"},
        )
        assert follow_up.status_code == 202, follow_up.text
        session(client, "approver")
        approvals = client.get("/api/approvals")
        assert approvals.status_code == 200, approvals.text
        list_item = approvals.json()[0]
        approval_id = list_item["id"]

        async def poison_legacy_projection_inputs() -> None:
            async with client.app.state.factory() as db:
                approval = await db.get(ApprovalRequest, approval_id)
                assert approval is not None and approval.proposal_id is not None
                proposal = await db.get(ProposalRecord, approval.proposal_id)
                snapshot = await db.scalar(
                    select(ApprovalSnapshot).where(ApprovalSnapshot.approval_id == approval_id)
                )
                assert proposal is not None
                approval.review_context = {
                    "original_ticket": "<script>approval-poison</script>",
                    "tool_observations": [
                        {
                            "mcp_raw": "MCP_RAW_APPROVAL_POISON",
                            "authorization": "Bearer approval-secret",
                        }
                    ],
                }
                approval.action_payload = {
                    **approval.action_payload,
                    "refund_reason": "Bearer approval-secret",
                }
                proposal.observation_binding = [
                    {"exception": "Traceback: APPROVAL_EXCEPTION_POISON"}
                ]
                if snapshot is not None:
                    snapshot.policy_binding = {
                        "result": "<script>approval-poison</script>",
                        "private": "MCP_RAW_APPROVAL_POISON",
                    }
                await db.commit()

        client.portal.call(poison_legacy_projection_inputs)
        detail = client.get(f"/api/approvals/{approval_id}")
        source = client.get(f"/api/approvals/{approval_id}/source")
        invalid_source_cursor = client.get(f"/api/approvals/{approval_id}/source?before_sequence=1")
        conflicting_source_cursor = client.get(
            f"/api/approvals/{approval_id}/source?before_sequence=999&before_message_id=msg_missing"
        )
        other_tenant_token = issue_token(
            Principal(
                role="approver",
                subject_id="user_approver_demo",
                tenant_id="tenant_other",
                membership_role="support_approver",
            ),
            client.app.state.session_serializer,
        )
        client.cookies.set("supportguard_session", other_tenant_token)
        cross_tenant_source = client.get(f"/api/approvals/{approval_id}/source")

    assert customer_ticket.status_code == 200, customer_ticket.text
    assert customer_ticket.json()["status"] == "awaiting_approval"
    assert customer_ticket.json()["approval"] == {
        "id": approval_id,
        "status": "pending",
        "action_type": "refund",
    }
    assert customer_ticket.json()["business_action"] is None
    assert set(list_item) == {
        "id",
        "ticket_id",
        "source_label",
        "status",
        "action_type",
        "resource_summary",
        "risk",
        "actionable",
        "allowed_actions",
        "created_at",
    }
    assert list_item["source_label"] == message
    assert list_item["resource_summary"] == "bill_demo_duplicate"
    assert detail.status_code == 200, detail.text
    projected = detail.json()
    assert projected["actionable"] is True
    assert projected["status"] == "pending"
    assert projected["action_type"] == "refund"
    assert projected["ticket"]["id"] == accepted.json()["ticket_id"]
    assert projected["ticket"]["title"] == "客户支持会话"
    assert projected["action_payload"]["billing_record_id"] == "bill_demo_duplicate"
    assert set(projected["review_context"]) == {
        "original_request",
        "risk",
        "policy_route",
        "freshness",
        "tool_observations",
        "evidence",
    }
    assert projected["review_context"]["original_request"] == message
    assert projected["risk"] == "high"
    assert projected["resource_summary"] == "bill_demo_duplicate"
    assert all(set(item) == {"label", "satisfied"} for item in projected["execution_preconditions"])
    assert projected["proposed_diff"][0] == {
        "field": "账单退款状态",
        "current": "charged",
        "proposed": "退款 49.00 USD",
    }
    assert projected["human_decision"] is None
    assert projected["business_action"] is None
    serialized = json.dumps(projected, ensure_ascii=False)
    for poison in (
        "Bearer approval-secret",
        "<script>approval-poison</script>",
        "MCP_RAW_APPROVAL_POISON",
        "APPROVAL_EXCEPTION_POISON",
    ):
        assert poison not in serialized
    for forbidden in (
        "original_ticket",
        "observation_binding",
        "action_hash",
        "idempotency_key",
        "actor_id",
        "last_error",
        "result",
    ):
        assert f'"{forbidden}"' not in serialized
    assert source.status_code == 200, source.text
    assert invalid_source_cursor.status_code == 422
    assert invalid_source_cursor.json()["public_code"] == "invalid_request"
    assert conflicting_source_cursor.status_code == 409
    assert conflicting_source_cursor.json()["public_code"] == "state_conflict"
    source_payload = source.json()
    assert source_payload["approval_id"] == approval_id
    assert source_payload["ticket_id"] == accepted.json()["ticket_id"]
    assert source_payload["title"] == message
    assert source_payload["origin_turn_id"] == projected["origin_turn_id"]
    assert source_payload["returned"] == len(source_payload["messages"])
    assert source_payload["has_more"] is False
    assert source_payload["next_before_sequence"] is None
    assert source_payload["next_before_message_id"] is None
    assert [item["sequence"] for item in source_payload["messages"]] == sorted(
        item["sequence"] for item in source_payload["messages"]
    )
    assert all(
        set(item)
        == {
            "id",
            "turn_id",
            "kind",
            "role",
            "content",
            "sequence",
            "is_origin_turn",
            "created_at",
        }
        for item in source_payload["messages"]
    )
    assert any(
        item["kind"] == "customer" and item["content"] == message and item["is_origin_turn"] is True
        for item in source_payload["messages"]
    )
    assert all(
        item["content"] != "补充：请不要把后续消息混入原审批来源。"
        for item in source_payload["messages"]
    )
    assert cross_tenant_source.status_code == 404


def test_run_inspector_is_exactly_bound_and_redacts_runtime_internals() -> None:
    message = "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded，为什么？"
    with TestClient(create_app(testing=True)) as client:
        csrf = session(client, "customer", "cust_demo")
        accepted = client.post(
            "/api/conversations",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "run-inspector-exact"},
            json={"message": message},
        )
        assert accepted.status_code == 202, accepted.text
        run_accepted_ticket(client, accepted.json(), message)
        conversation = client.get(f"/api/conversations/{accepted.json()['ticket_id']}")
        assert conversation.status_code == 200, conversation.text
        turn = conversation.json()["turns"][0]
        assistant = next(item for item in turn["messages"] if item["kind"] == "assistant")
        customer = next(item for item in turn["messages"] if item["kind"] == "customer")

        async def poison_private_runtime_fields() -> None:
            async with client.app.state.factory() as db:
                run = await db.get(AgentRun, accepted.json()["run_id"])
                job = await db.get(RuntimeJob, accepted.json()["job_id"])
                attempt = await db.scalar(
                    select(AgentCallAttempt)
                    .where(
                        AgentCallAttempt.run_id == accepted.json()["run_id"],
                        AgentCallAttempt.call_kind == "llm",
                    )
                    .order_by(AgentCallAttempt.ordinal.desc())
                    .limit(1)
                )
                event = await db.scalar(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == accepted.json()["run_id"])
                    .order_by(AgentEvent.ticket_sequence)
                    .limit(1)
                )
                assert (
                    run is not None
                    and job is not None
                    and attempt is not None
                    and event is not None
                )
                run.error_code = "provider_timeout"
                job.last_error = "private upstream body with bearer-secret"
                attempt.runtime_provenance = {
                    **attempt.runtime_provenance,
                    "private_provider_payload": "private provenance body",
                    "api_key": "provenance-secret",
                }
                event.payload = {
                    "tool_name": "query_account",
                    "error_code": "private-provider-code",
                    "system_prompt": "private system prompt",
                    "developer_prompt": "private developer prompt",
                    "secret": "bearer-secret",
                    "raw_error": "private upstream body",
                    "remaining_budget": {
                        "llm_calls": 2,
                        "tool_rounds": 1,
                        "tool_attempts": 3,
                        "private_budget": 999,
                    },
                }
                await db.commit()

        client.portal.call(poison_private_runtime_fields)
        params = {
            "conversation_id": accepted.json()["ticket_id"],
            "turn_id": turn["id"],
            "message_id": assistant["id"],
        }
        inspector = client.get(
            f"/api/runs/{accepted.json()['run_id']}/inspector",
            params=params,
        )
        wrong_kind = client.get(
            f"/api/runs/{accepted.json()['run_id']}/inspector",
            params={**params, "message_id": customer["id"]},
        )
        wrong_turn = client.get(
            f"/api/runs/{accepted.json()['run_id']}/inspector",
            params={**params, "turn_id": "turn_not_bound"},
        )
        session(client, "customer", "cust_other")
        cross_customer = client.get(
            f"/api/runs/{accepted.json()['run_id']}/inspector",
            params=params,
        )

    assert inspector.status_code == 200, inspector.text
    payload = inspector.json()
    assert (payload["message_id"], payload["turn_id"], payload["run_id"]) == (
        assistant["id"],
        turn["id"],
        accepted.json()["run_id"],
    )
    assert payload["run"]["configured_runtime"]["source"] == "command_acceptance"
    assert payload["run"]["actual_runtime"]["source"] == "agent_call_attempt"
    assert payload["run"]["job"]["has_error"] is True
    assert payload["run"]["failure_category"] == "provider"
    assert "last_error" not in payload["run"]["job"]
    first_event = payload["timeline"][0]
    assert {key: value for key, value in first_event["payload"].items() if value is not None} == {
        "tool_name": "query_account",
        "failure_recorded": True,
        "remaining_budget": {
            "llm_calls": 2,
            "tool_rounds": 1,
            "tool_attempts": 3,
        },
    }
    serialized = inspector.text
    for private_value in (
        "provider_timeout",
        "private upstream body",
        "bearer-secret",
        "private system prompt",
        "private developer prompt",
        "private-provider-code",
        "private_budget",
        "private provenance body",
        "provenance-secret",
    ):
        assert private_value not in serialized
    for private_key in (
        "attempt_id",
        "error_code",
        "last_error",
        "prompt_hash",
        "schema_hash",
        "runtime_manifest_hash",
        "embedding_fingerprint",
        "code_commit",
    ):
        assert private_key not in serialized
    assert wrong_kind.status_code == 404
    assert wrong_turn.status_code == 404
    assert cross_customer.status_code == 404


def test_test_app_owner_ignores_poisoned_parent_database_and_starts_no_stdio_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://wrong_api:wrong@127.0.0.1:1/wrong",
        "MCP_READ_DATABASE_URL": ("postgresql+asyncpg://wrong_read:wrong@127.0.0.1:1/wrong"),
        "MCP_ACTION_DATABASE_URL": ("postgresql+asyncpg://wrong_action:wrong@127.0.0.1:1/wrong"),
    }
    for name, value in poisoned.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    async def forbidden_start(_manager: object) -> None:
        raise AssertionError("ordinary TestClient must not initialize stdio MCP")

    monkeypatch.setattr("supportguard.main.MCPManager.start", forbidden_start)
    try:
        app = create_app(testing=True)
        with TestClient(app) as client:
            assert isinstance(client.app.state.tool_transport, InProcessTestToolTransport)
            owned_settings = client.app.state.settings
            assert owned_settings.app_env == "test"
            assert owned_settings.database_url.startswith("sqlite+aiosqlite:///")
            assert owned_settings.mcp_read_database_url is None
            assert owned_settings.mcp_action_database_url is None
            database_path = Path(owned_settings.database_url.removeprefix("sqlite+aiosqlite:///"))
            assert database_path.is_file()
            csrf = session(client, "customer", "cust_demo")
            accepted = client.post(
                "/api/tickets",
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "poison-owner"},
                json={"message": "atlas-chat 返回 429 concurrency_limit_exceeded"},
            )
            assert accepted.status_code == 202
        assert not database_path.exists()
        assert {name: os.environ.get(name) for name in poisoned} == poisoned
    finally:
        get_settings.cache_clear()


def test_public_mutation_openapi_success_and_upgrade_contracts_are_exact() -> None:
    schema = create_app(testing=True).openapi()
    runtime_schema = create_app(testing=False).openapi()
    assert "/api/session" in schema["paths"]
    assert "/api/approvals/{approval_id}" in schema["paths"]
    command_paths = {
        "/api/tickets",
        "/api/tickets/{ticket_id}/messages",
    }
    decision_paths = {
        "/api/approvals/{approval_id}/approve",
        "/api/approvals/{approval_id}/edit-and-approve",
        "/api/approvals/{approval_id}/reject",
    }
    for path in command_paths | decision_paths:
        responses = schema["paths"][path]["post"]["responses"]
        assert "200" not in responses and "201" not in responses
        assert responses["202"]["content"]["application/json"]["schema"] == {
            "$ref": (
                "#/components/schemas/CommandAcceptedResponse"
                if path in command_paths
                else "#/components/schemas/DecisionAcceptedResponse"
            )
        }
        assert responses["503"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ProductProblem"
        }
    takeover_responses = schema["paths"]["/api/approvals/{approval_id}/manual-takeover"]["post"][
        "responses"
    ]
    assert "202" not in takeover_responses
    assert "409" in takeover_responses
    components = schema["components"]["schemas"]
    expected = {
        "CommandAcceptedResponse": {
            "schema_version",
            "ticket_id",
            "run_id",
            "job_id",
            "accepted_at",
            "status",
            "status_url",
            "events_url",
            "reused",
        },
        "DecisionAcceptedResponse": {
            "schema_version",
            "approval_id",
            "decision",
            "ticket_id",
            "run_id",
            "job_id",
            "accepted_at",
            "status",
            "status_url",
            "events_url",
            "reused",
        },
        "ProductProblem": {
            "public_code",
            "message",
            "retryable",
            "request_id",
        },
    }
    for name, fields in expected.items():
        assert components[name]["additionalProperties"] is False
        assert set(components[name]["properties"]) == fields
        assert runtime_schema["components"]["schemas"][name] == components[name]
    for path in command_paths | decision_paths:
        assert (
            runtime_schema["paths"][path]["post"]["responses"]
            == schema["paths"][path]["post"]["responses"]
        )
    assert (
        runtime_schema["paths"]["/api/approvals/{approval_id}/manual-takeover"]["post"]["responses"]
        == takeover_responses
    )
    for name in ("CommandAcceptedResponse", "DecisionAcceptedResponse"):
        accepted_at = components[name]["properties"]["accepted_at"]
        assert accepted_at["type"] == "string"
        assert accepted_at["format"] == "date-time"
        assert accepted_at["pattern"] == CANONICAL_UTC_TIMESTAMP_PATTERN
        assert accepted_at["examples"] == [CANONICAL_UTC_TIMESTAMP_EXAMPLE]
    decision_schema = components["DecisionAcceptedResponse"]["properties"]
    assert decision_schema["decision"]["enum"] == [
        "approve",
        "edit_and_approve",
        "reject",
    ]
    assert decision_schema["job_id"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    assert decision_schema["status_url"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    operands = {
        "mutation_path_count": len(command_paths | decision_paths),
        "success_status_codes": [202],
        "unexpected_success_schema_count": 0,
        "upgrade_status_codes": [503],
        "response_schema_field_counts": {
            name: len(fields) for name, fields in sorted(expected.items())
        },
        "accepted_at_pattern": CANONICAL_UTC_TIMESTAMP_PATTERN,
        "accepted_at_example": CANONICAL_UTC_TIMESTAMP_EXAMPLE,
        "runtime_mode_count": 2,
        "runtime_response_contract_equal": True,
    }
    for predicate_id in ("openapi_success_202_exact", "runtime_openapi_mode_invariant"):
        record_predicate_operands(
            requirement_id="C6-P0-17",
            predicate_id=predicate_id,
            subject_kind="openapi_mutation_contract",
            operands=operands,
        )


def test_refund_api_decision_returns_typed_accepted_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def decide(*_args: object, **_kwargs: object) -> DecisionAccepted:
        nonlocal calls
        calls += 1
        return DecisionAccepted(
            approval_id="approval_contract",
            ticket_id="ticket_contract",
            run_id="run_contract",
            job_id="job_contract",
            decision="approve",
            accepted_at=datetime(2026, 7, 15, tzinfo=UTC),
            reused=calls > 1,
        )

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    with TestClient(create_app(testing=True)) as client:
        approver_csrf = session(client, "approver")
        approved = client.post(
            "/api/approvals/approval_contract/approve",
            headers={"X-CSRF-Token": approver_csrf, "Idempotency-Key": "approve-1"},
            json={"reason": "Snapshot and duplicate relation verified."},
        )
        assert approved.status_code == 202, approved.text
        repeated = client.post(
            "/api/approvals/approval_contract/approve",
            headers={"X-CSRF-Token": approver_csrf, "Idempotency-Key": "approve-1"},
            json={"reason": "Snapshot and duplicate relation verified."},
        )
        assert repeated.status_code == 202, repeated.text

    assert approved.json()["run_id"] == "run_contract"
    assert approved.json()["status"] == "decision_accepted"
    assert approved.json()["schema_version"] == "decision-accepted.v1"
    assert repeated.json() == {**approved.json(), "reused": True}
    operands = {
        "first_status": approved.status_code,
        "replay_status": repeated.status_code,
        "first_schema_version": approved.json()["schema_version"],
        "first_runtime_status": approved.json()["status"],
        "first_run_id": approved.json()["run_id"],
        "replay_run_id": repeated.json()["run_id"],
        "first_reused": approved.json()["reused"],
        "replay_reused": repeated.json()["reused"],
        "coordinator_call_count": calls,
        "response_keys": sorted(approved.json()),
    }
    for predicate_id in ("accepted_response_typed_exact", "idempotent_replay_same_shape"):
        record_predicate_operands(
            requirement_id="C6-P0-17",
            predicate_id=predicate_id,
            subject_kind="typed_decision_acceptance",
            operands=operands,
        )


def test_edit_and_approve_accepts_canonical_entitlement_change_and_rejects_unknown_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def decide(*_args: object, **kwargs: object) -> DecisionAccepted:
        calls.append(dict(kwargs))
        return DecisionAccepted(
            approval_id="approval_contract",
            ticket_id="ticket_contract",
            run_id="run_contract",
            job_id="job_contract",
            decision="edit_and_approve",
            accepted_at=datetime(2026, 7, 15, tzinfo=UTC),
            reused=False,
        )

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    with TestClient(create_app(testing=True)) as client:
        approver_csrf = session(client, "approver")
        accepted = client.post(
            "/api/approvals/approval_contract/edit-and-approve",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "edit-entitlement-1",
            },
            json={
                "reason": "Current usage and plan evidence support the adjusted limit.",
                "changes": {"target_concurrency": 48},
            },
        )
        rejected = client.post(
            "/api/approvals/approval_contract/edit-and-approve",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "edit-entitlement-invalid",
            },
            json={
                "changes": {
                    "resource_id": "sub_foreign",
                    "unsupported_field": True,
                }
            },
        )
        coerced = client.post(
            "/api/approvals/approval_contract/edit-and-approve",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "edit-entitlement-coercion",
            },
            json={"changes": {"target_concurrency": "48"}},
        )

    assert accepted.status_code == 202, accepted.text
    assert calls[0]["edited_payload"] == {"target_concurrency": 48}
    assert rejected.status_code == 422
    assert coerced.status_code == 422
    assert len(calls) == 1


def test_action_specific_edit_conflict_is_a_stable_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def decide(*_args: object, **_kwargs: object) -> DecisionAccepted:
        raise RuntimeConflict("approval_edit_not_allowed")

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    with TestClient(create_app(testing=True)) as client:
        approver_csrf = session(client, "approver")
        response = client.post(
            "/api/approvals/approval_contract/edit-and-approve",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "edit-wrong-action",
            },
            json={"changes": {"target_concurrency": 48}},
        )

    assert response.status_code == 422
    assert response.json()["public_code"] == "invalid_request"


@pytest.mark.parametrize(
    ("endpoint", "body", "terminal", "decision_status"),
    [
        (
            "reject",
            {"reason": "The customer evidence did not pass manual review."},
            "rejected",
            "rejected",
        ),
        (
            "edit-and-approve",
            {
                "refund_reason": "Duplicate relation and customer impact manually verified.",
                "approver_note": "Reason clarified without changing amount or target.",
            },
            "resolved",
            "succeeded",
        ),
    ],
)
def test_all_human_decisions_resume_through_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    body: dict[str, str],
    terminal: str,
    decision_status: str,
) -> None:
    async def decide(*_args: object, **kwargs: object) -> DecisionAccepted:
        decision = str(kwargs["decision"])
        return DecisionAccepted(
            approval_id="approval_contract",
            ticket_id="ticket_contract",
            run_id="run_contract",
            job_id=None if decision == "reject" else "job_contract",
            decision=decision,
            accepted_at=datetime(2026, 7, 15, tzinfo=UTC),
            reused=False,
        )

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    with TestClient(create_app(testing=True)) as client:
        approver_csrf = session(client, "approver")
        decided = client.post(
            f"/api/approvals/approval_contract/{endpoint}",
            headers={"X-CSRF-Token": approver_csrf, "Idempotency-Key": endpoint},
            json=body,
        )
        assert decided.status_code == 202, decided.text

    assert decided.json()["decision"] == endpoint.replace("-", "_")
    assert decided.json()["status"] == "decision_accepted"
    assert decided.json()["job_id"] == (None if endpoint == "reject" else "job_contract")
    assert decided.json()["status_url"] == (
        None if endpoint == "reject" else "/api/runs/run_contract"
    )


def test_reject_acceptance_does_not_consume_runtime_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def decide(*_args: object, **_kwargs: object) -> DecisionAccepted:
        return DecisionAccepted(
            approval_id="approval_contract",
            ticket_id="ticket_contract",
            run_id="run_contract",
            job_id=None,
            decision="reject",
            accepted_at=datetime(2026, 7, 15, tzinfo=UTC),
            reused=False,
        )

    async def forbidden_admission(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reject must not depend on runtime backlog admission")

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    monkeypatch.setattr("supportguard.api.messages.admit_command", forbidden_admission)
    with TestClient(create_app(testing=True)) as client:
        client.app.state.testing = False
        approver_csrf = session(client, "approver")
        response = client.post(
            "/api/approvals/approval_contract/reject",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "reject-no-runtime-admission",
            },
            json={"reason": "The evidence did not pass independent review."},
        )
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] is None
    assert response.json()["status_url"] is None


def test_public_manual_takeover_is_rejected_without_calling_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def decide(*_args: object, **_kwargs: object) -> DecisionAccepted:
        raise AssertionError("public manual takeover must not reach the coordinator")

    monkeypatch.setattr(
        "supportguard.api.endpoints.approvals.ApprovalCommandCoordinator.decide",
        decide,
    )
    with TestClient(create_app(testing=True)) as client:
        approver_csrf = session(client, "approver")
        response = client.post(
            "/api/approvals/approval_contract/manual-takeover",
            headers={
                "X-CSRF-Token": approver_csrf,
                "Idempotency-Key": "manual-takeover-disabled",
            },
            json={"reason": "Please move this to a human queue."},
        )
    assert response.status_code == 409
    assert response.json()["public_code"] == "state_conflict"
    assert response.json()["retryable"] is False
