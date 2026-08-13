from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from supportguard.evals.provider_p16 import (
    _diagnostic_snapshot,
    _provider_usage,
    _provider_usage_is_complete,
    _scenario_execution_failure_code,
)
from supportguard.evals.scenario_http import (
    ScenarioHttpClient,
    ScenarioHttpTransportError,
)


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_poll_retries_one_timeout_then_records_a_safe_response() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("do-not-record-this-body", request=request)
        return httpx.Response(
            200,
            json={"turns": []},
            headers={"X-Request-ID": "request_0123456789abcdef0123456789abcdef"},
        )

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P14",
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    try:
        response = await client.poll(
            "/api/conversations/ticket-private",
            deadline=client.deadline_after(5),
        )
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert calls == 2
    assert diagnostics["request_attempts"] == 2
    assert diagnostics["transport_retry_attempts"] == 1
    operations = diagnostics["operations"]
    last_response = operations["conversation_poll"]["last_response"]
    assert isinstance(last_response["elapsed_ms"], int)
    assert last_response["elapsed_ms"] >= 0
    last_response = {key: value for key, value in last_response.items() if key != "elapsed_ms"}
    assert operations["conversation_poll"]["attempts"] == 2
    assert operations["conversation_poll"]["transport_failures"] == 1
    assert last_response == {
        "status_code": 200,
        "request_id": "request_0123456789abcdef0123456789abcdef",
        "trace_id": "trace_ie_p14_conversation_poll_1_2",
    }
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert "do-not-record-this-body" not in serialized
    assert "ticket-private" not in serialized


@pytest.mark.asyncio
async def test_idempotent_submit_retries_the_exact_protected_request() -> None:
    observed: list[tuple[bytes, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.content, request.headers.get("Idempotency-Key")))
        if len(observed) == 1:
            raise httpx.ReadTimeout("response-lost", request=request)
        return httpx.Response(201, json={"ticket_id": "ticket-1"})

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P01",
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    try:
        response = await client.submit(
            "/api/conversations",
            operation="conversation_create",
            payload={"message": "sensitive customer content"},
            headers={"Idempotency-Key": "ie-p01-turn-1", "X-CSRF-Token": "csrf-private"},
            deadline=client.deadline_after(5),
        )
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    assert response.status_code == 201
    assert observed == [observed[0], observed[0]]
    assert observed[0][1] == "ie-p01-turn-1"
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert "sensitive customer content" not in serialized
    assert "csrf-private" not in serialized


@pytest.mark.asyncio
async def test_persistent_timeout_is_bounded_and_diagnostics_never_record_secret_text() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(
            "sk-this-value-must-never-enter-the-receipt",
            request=request,
        )

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P16",
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    try:
        with pytest.raises(ScenarioHttpTransportError) as captured:
            await client.poll("/api/conversations/private", deadline=client.deadline_after(5))
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    assert captured.value.code == "evaluation_http_timeout"
    assert captured.value.operation == "conversation_poll"
    assert calls == 6
    assert diagnostics["request_attempts"] == 6
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert "sk-this-value-must-never-enter-the-receipt" not in serialized
    assert "private" not in serialized


@pytest.mark.asyncio
async def test_submit_without_idempotency_key_fails_before_transport() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P01",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="scenario_http_idempotency_key_required"):
            await client.submit(
                "/api/conversations",
                operation="conversation_create",
                payload={"message": "hello"},
                headers={},
                deadline=client.deadline_after(5),
            )
    finally:
        await client.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_http_status_is_returned_without_transport_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"X-Request-ID": "request-503"})

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P02",
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.poll("/api/conversations/ticket", deadline=client.deadline_after(5))
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    assert response.status_code == 503
    assert calls == 1
    assert diagnostics["transport_retry_attempts"] == 0


@pytest.mark.asyncio
async def test_repeated_successful_polls_receive_unique_trace_ids() -> None:
    traces: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        traces.append(request.headers.get("X-Trace-ID"))
        return httpx.Response(200, json={"turns": []})

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P15",
        transport=httpx.MockTransport(handler),
    )
    try:
        deadline = client.deadline_after(5)
        await client.poll("/api/conversations/ticket", deadline=deadline)
        await client.poll("/api/conversations/ticket", deadline=deadline)
    finally:
        await client.aclose()

    assert traces == [
        "trace_ie_p15_conversation_poll_1_1",
        "trace_ie_p15_conversation_poll_2_1",
    ]


@pytest.mark.asyncio
async def test_untrusted_response_request_id_is_not_recorded() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"turns": []},
            headers={"X-Request-ID": "sk-this-header-must-not-be-recorded"},
        )

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P15",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.poll("/api/conversations/ticket", deadline=client.deadline_after(5))
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    serialized = json.dumps(diagnostics, sort_keys=True)
    assert "sk-this-header-must-not-be-recorded" not in serialized
    assert diagnostics["operations"]["conversation_poll"]["last_response"]["request_id"] is None


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.value += delay


@pytest.mark.asyncio
async def test_deadline_stops_retry_before_an_unbounded_second_send() -> None:
    clock = _AdvancingClock()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = ScenarioHttpClient(
        "http://supportguard.invalid",
        scenario_id="IE-P03",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
    )
    try:
        with pytest.raises(ScenarioHttpTransportError) as captured:
            await client.poll("/api/conversations/ticket", deadline=client.deadline_after(0.1))
        diagnostics = client.diagnostics()
    finally:
        await client.aclose()

    assert captured.value.code == "evaluation_http_deadline_exceeded"
    assert calls == 1
    assert diagnostics["request_attempts"] == 2
    assert diagnostics["transport_failures"][-1]["outcome"] == "deadline_exceeded"


def test_failure_code_and_snapshot_usage_are_body_free_and_observed() -> None:
    request = httpx.Request("GET", "http://supportguard.invalid/private")
    response = httpx.Response(502, request=request)
    response_error = httpx.HTTPStatusError("private response", request=request, response=response)
    timeout = ScenarioHttpTransportError(
        code="evaluation_http_timeout",
        operation="conversation_poll",
    )

    assert _scenario_execution_failure_code(timeout) == (
        "evaluation_http_timeout:conversation_poll"
    )
    assert _scenario_execution_failure_code(response_error) == "evaluation_http_status_502"

    snapshot: dict[str, Any] = {
        "runs": [{"id": "run-secret", "status": "failed", "error_code": "provider_timeout"}],
        "attempts": [
            {
                "run_id": "run-secret",
                "call_kind": "llm",
                "status": "succeeded",
                "count": 2,
                "prompt_tokens": 123,
                "completion_tokens": 45,
            }
        ],
        "tools": [{"run_id": "run-secret", "name": "query", "count": 2}],
        "proposals": [],
        "approval_count": 0,
        "pending_approval_count": 0,
        "action_count": 0,
        "citation_binding_count": 1,
        "claim_count": 1,
        "unsupported_material_claim_count": 0,
    }
    assert _provider_usage(snapshot) == {"prompt_tokens": 123, "completion_tokens": 45}
    assert _provider_usage_is_complete(snapshot) is True
    diagnostic = _diagnostic_snapshot(snapshot)
    assert diagnostic["tool_invocation_count"] == 2
    assert "run-secret" not in json.dumps(diagnostic, sort_keys=True)

    snapshot["runs"][0]["error_code"] = "sk-this-error-must-not-be-recorded"
    redacted = _diagnostic_snapshot(snapshot)
    assert redacted["run_facts"][0]["error_code"] is None

    snapshot["runs"][0]["status"] = "running"
    assert _provider_usage_is_complete(snapshot) is False
    snapshot["runs"][0]["status"] = "failed"
    snapshot["attempts"][0]["status"] = "unknown"
    assert _provider_usage_is_complete(snapshot) is False
    snapshot["attempts"][0]["status"] = "failed"
    snapshot["attempts"][0]["prompt_tokens"] = 0
    snapshot["attempts"][0]["completion_tokens"] = 0
    assert _provider_usage_is_complete(snapshot) is False
    snapshot["attempts"][0]["status"] = "succeeded"
    snapshot["attempts"][0]["prompt_tokens"] = 123
    snapshot["attempts"][0]["completion_tokens"] = 45
    snapshot["attempts"].append(
        {
            "run_id": "run-secret",
            "call_kind": "read_mcp",
            "status": "unknown",
            "count": 1,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    )
    assert _provider_usage_is_complete(snapshot) is True
