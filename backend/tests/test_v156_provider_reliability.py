from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from supportguard.agent.nodes.decision_support import AgentRuntimeServices
from supportguard.agent.schemas import Classification
from supportguard.config import Settings
from supportguard.providers.deepseek import (
    DeepSeekProvider,
    ProviderRequestError,
    provider_error_code,
)
from supportguard.providers.limiter import ProviderLimitError


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        deepseek_api_key=SecretStr("test-only-not-a-real-secret"),
        llm_model="deepseek-v4-flash",
        llm_temperature=0,
    )


def _bounded_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        deepseek_api_key=SecretStr("test-only-not-a-real-secret"),
        llm_model="deepseek-v4-flash",
        llm_temperature=0,
        provider_max_input_tokens=1_000,
    )


def _classification_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-v156",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "issue_type": "api_diagnostics",
                                "risk": "low",
                                "policy_boundary": "allowed",
                                "requested_action": "none",
                                "requested_concurrency_limit": None,
                                "needs_realtime_facts": True,
                                "support_subject": "customer_problem",
                                "rationale": "A scoped API failure needs diagnosis.",
                            }
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


async def _generate(provider: DeepSeekProvider) -> Any:
    return await provider.generate(
        system="classify",
        user='{"ticket":"429"}',
        output_schema=Classification,
        trace_metadata={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised_type", "expected"),
    [
        (httpx.ConnectError, "provider_connection_error"),
        (httpx.ReadTimeout, "provider_timeout"),
    ],
)
async def test_v156_transport_failure_exposes_safe_category(
    raised_type: type[httpx.RequestError], expected: str
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise raised_type("safe transport failure", request=request)

    provider = DeepSeekProvider(_settings(), http_transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(ProviderRequestError) as captured:
            await _generate(provider)
        assert captured.value.error_code == expected
        assert captured.value.transport_attempts == 2
        assert AgentRuntimeServices._provider_failure_error_code(captured.value) == expected
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retry_after", "expected_delay"),
    [
        (429, "3", 3.0),
        (503, None, 1.0),
        (503, "not-a-delay", 1.0),
        (503, "60", 5.0),
    ],
)
async def test_v156_retry_wait_is_bounded_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    retry_after: str | None,
    expected_delay: float,
) -> None:
    calls = 0
    delays: list[float] = []

    def transport(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return httpx.Response(status, headers=headers, json={"error": {"message": "safe"}})
        return _classification_response()

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("supportguard.providers.deepseek.asyncio.sleep", record_sleep)
    provider = DeepSeekProvider(_settings(), http_transport=httpx.MockTransport(transport))
    try:
        result = await _generate(provider)
        assert result.transport_attempts == 2
        assert calls == 2
        assert delays == [expected_delay]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected", "attempts"),
    [
        (401, "provider_request_failed", 1),
        (429, "provider_http_429", 2),
        (503, "provider_http_503", 2),
    ],
)
async def test_v156_terminal_http_error_is_classified_without_response_body(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: str,
    attempts: int,
) -> None:
    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr("supportguard.providers.deepseek.asyncio.sleep", no_wait)
    provider = DeepSeekProvider(
        _settings(),
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status,
                json={"error": {"message": "must not be persisted"}},
            )
        ),
    )
    try:
        with pytest.raises(ProviderRequestError) as captured:
            await _generate(provider)
        assert captured.value.error_code == expected
        assert captured.value.transport_attempts == attempts
        assert "must not be persisted" not in str(captured.value)
    finally:
        await provider.aclose()


def test_v156_limiter_failure_has_a_stable_safe_category() -> None:
    assert (
        provider_error_code(ProviderLimitError("provider_limiter_unavailable"))
        == "provider_limiter_unavailable"
    )


@pytest.mark.asyncio
async def test_v156_transport_budget_failure_survives_sdk_wrapping() -> None:
    provider = DeepSeekProvider(
        _bounded_settings(),
        http_transport=httpx.MockTransport(lambda _request: _classification_response()),
    )
    try:
        with pytest.raises(ProviderRequestError) as captured:
            await provider.generate(
                system="classify",
                user="x" * 12_000,
                output_schema=Classification,
                trace_metadata={},
            )
        assert captured.value.transport_attempts == 1
        assert captured.value.error_code == "provider_transport_budget_exceeded"
    finally:
        await provider.aclose()
