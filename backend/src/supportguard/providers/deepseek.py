from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from supportguard.config import Settings, get_settings
from supportguard.providers.base import (
    TERMINAL_CANDIDATE_FUNCTION,
    ProviderCallResult,
    ProviderTransportRecord,
    ProviderUsage,
    RawProviderDecision,
    RawProviderToolCall,
    native_terminal_candidate_schema,
)
from supportguard.providers.limiter import ProviderLimitError, RedisProviderLimiter

OutputT = TypeVar("OutputT", bound=BaseModel)


def _terminal_candidate_content(
    tool_calls: Sequence[Any],
) -> str | None:
    """Normalize exactly one reserved terminal call into strict JSON content.

    Invalid arguments remain content for the existing bounded schema-repair path. Mixed or
    repeated calls are application tool batches and therefore continue to fail closed downstream.
    """

    if len(tool_calls) != 1:
        return None
    call = tool_calls[0]
    if call.function.name != TERMINAL_CANDIDATE_FUNCTION:
        return None
    arguments = call.function.arguments
    return arguments if isinstance(arguments, str) else None


class ProviderError(RuntimeError):
    pass


class ProviderRequestError(ProviderError):
    """A terminal provider request failure with bounded transport-attempt provenance."""

    def __init__(self, transport_attempts: int, *, error_code: str) -> None:
        super().__init__(error_code)
        self.transport_attempts = transport_attempts
        self.error_code = error_code


class ProviderStructuredOutputError(ProviderError):
    """A transport-successful response that failed strict JSON/schema intake."""

    def __init__(
        self,
        *,
        error_paths: tuple[str, ...],
        transport: ProviderTransportRecord,
        usage: ProviderUsage,
        transport_attempts: int = 1,
        parsed_payload: Any | None = None,
    ) -> None:
        super().__init__("provider_structured_output_invalid")
        self.error_paths = error_paths
        self.transport = transport
        self.usage = usage
        self.transport_attempts = transport_attempts
        # Deliberately ephemeral: the bounded repair path may remove only
        # fields Pydantic classified as ``extra_forbidden`` without persisting
        # the Provider body in logs, ContextLedgers, or Inspector payloads.
        self.parsed_payload = parsed_payload


def _unwrap_exact_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        return stripped[len("```json\n") : -len("\n```")].strip()
    if stripped.startswith("```\n") and stripped.endswith("\n```"):
        return stripped[len("```\n") : -len("\n```")].strip()
    return stripped


def _structured_error_paths(exc: Exception) -> tuple[str, ...]:
    if isinstance(exc, ValidationError):
        paths = []
        for item in exc.errors(include_url=False, include_context=False, include_input=False):
            location = ".".join(str(part) for part in item.get("loc", ())) or "$"
            paths.append(f"{location}:{item.get('type', 'schema_error')}")
        return tuple(paths[:12]) or ("$:schema_error",)
    if isinstance(exc, json.JSONDecodeError):
        return (f"$:json_decode:{exc.msg}",)
    return ("$:structured_output_invalid",)


_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_SAFE_PROVIDER_ERROR_CODES = {
    "provider_timeout",
    "provider_connection_error",
    "provider_limiter_unavailable",
    "provider_transport_budget_exceeded",
    "provider_request_failed",
    *{f"provider_http_{status}" for status in _RETRYABLE_STATUS_CODES},
}


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def provider_error_code(exc: BaseException) -> str:
    """Return a stable, body-free category from a wrapped provider failure."""

    chain = _exception_chain(exc)
    for current in chain:
        stored = getattr(current, "error_code", None)
        if isinstance(stored, str) and stored in _SAFE_PROVIDER_ERROR_CODES:
            return stored
        if isinstance(current, ProviderError) and str(current) == (
            "provider_transport_budget_exceeded"
        ):
            return "provider_transport_budget_exceeded"
    for current in chain:
        if isinstance(current, (APITimeoutError, httpx.TimeoutException)):
            return "provider_timeout"
        if isinstance(current, APIStatusError):
            if current.status_code in _RETRYABLE_STATUS_CODES:
                return f"provider_http_{current.status_code}"
            return "provider_request_failed"
        if isinstance(current, ProviderLimitError):
            return "provider_limiter_unavailable"
        if isinstance(current, (APIConnectionError, httpx.NetworkError)):
            return "provider_connection_error"
    return "provider_request_failed"


def _retry_after_seconds(exc: BaseException) -> float:
    """Resolve Retry-After without retaining headers; always return 0..5 seconds."""

    for current in _exception_chain(exc):
        if not isinstance(current, APIStatusError) or current.response is None:
            continue
        raw = current.response.headers.get("Retry-After")
        if not raw:
            break
        try:
            delay = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                break
        return min(5.0, max(0.0, delay))
    return 1.0


class DeepSeekProvider:
    mode = "production"
    tool_call_mode = "native"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        limiter: RedisProviderLimiter | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        secret: SecretStr | None = self.settings.deepseek_api_key
        if secret is None or not secret.get_secret_value():
            raise ProviderError("DEEPSEEK_API_KEY is required for the real provider")
        self._http_client = httpx.AsyncClient(
            event_hooks={"request": [self._capture_request]},
            transport=http_transport,
        )
        self._client = AsyncOpenAI(
            api_key=secret.get_secret_value(),
            base_url=self.settings.llm_base_url,
            timeout=30.0,
            max_retries=0,
            http_client=self._http_client,
        )
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.request_count = 0
        self.model = self.settings.llm_model
        self.limiter = limiter
        self.max_input_tokens = self.settings.provider_max_input_tokens
        self._transport_capture: ContextVar[ProviderTransportRecord | None] = ContextVar(
            "supportguard_deepseek_transport", default=None
        )

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def _capture_request(self, request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            self.request_count += 1
            transport = ProviderTransportRecord.from_bytes(
                request.content, serializer_version="openai-http-json.v1"
            )
            self._transport_capture.set(transport)
            estimated_tokens = max(1, (len(request.content) + 2) // 3)
            if estimated_tokens > self.max_input_tokens:
                raise ProviderError("provider_transport_budget_exceeded")

    async def _create(self, **kwargs: Any) -> tuple[Any, int]:
        create: Any = self._client.chat.completions.create
        for attempt in range(1, 3):
            self._transport_capture.set(None)
            try:
                if self.limiter is None:
                    return await create(**kwargs), attempt
                async with self.limiter.slot():
                    return await create(**kwargs), attempt
            except Exception as exc:
                if attempt == 2 or not self._is_retryable_transport_error(exc):
                    raise ProviderRequestError(
                        attempt,
                        error_code=provider_error_code(exc),
                    ) from exc
                await asyncio.sleep(_retry_after_seconds(exc))
        raise AssertionError("bounded provider retry loop exhausted")

    @staticmethod
    def _is_retryable_transport_error(exc: Exception) -> bool:
        if provider_error_code(exc) == "provider_transport_budget_exceeded":
            return False
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (APIConnectionError, APITimeoutError, httpx.TransportError)):
                return True
            if (
                isinstance(current, APIStatusError)
                and current.status_code in _RETRYABLE_STATUS_CODES
            ):
                return True
            if isinstance(current, ProviderLimitError):
                return "release_unknown" not in str(current)
            current = current.__cause__ or current.__context__
        return False

    async def generate(
        self,
        *,
        system: str,
        user: str,
        output_schema: type[OutputT],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[OutputT]:
        schema = json.dumps(output_schema.model_json_schema(), ensure_ascii=False)
        instruction = (
            f"{system}\nRequired JSON Schema (all required fields must be present):\n{schema}"
        )
        prompt_tokens = 0
        completion_tokens = 0
        response, transport_attempts = await self._create(
            model=self.settings.llm_model,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.provider_max_output_tokens,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        if response.usage is not None:
            self.prompt_tokens += response.usage.prompt_tokens
            self.completion_tokens += response.usage.completion_tokens
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
        content = response.choices[0].message.content
        if not content:
            raise ProviderError("provider returned empty content")
        transport = self._require_transport()
        usage = ProviderUsage(prompt_tokens, completion_tokens)
        payload: Any | None = None
        try:
            payload = json.loads(_unwrap_exact_json_fence(content))
            output = output_schema.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProviderStructuredOutputError(
                error_paths=_structured_error_paths(exc),
                transport=transport,
                usage=usage,
                transport_attempts=transport_attempts,
                parsed_payload=payload,
            ) from exc
        return ProviderCallResult(
            output=output,
            attempts=1,
            usage=usage,
            trace_metadata=dict(trace_metadata),
            transport=transport,
            transport_attempts=transport_attempts,
        )

    async def decide(
        self,
        *,
        system: str,
        context: str,
        tools: list[dict[str, Any]],
        prior_turns: list[dict[str, Any]],
        trace_metadata: dict[str, str],
    ) -> ProviderCallResult[RawProviderDecision]:
        instruction = (
            f"{system}\nUse only the supplied native read functions when another read is "
            f"required. When evidence is complete, call the reserved "
            f"`{TERMINAL_CANDIDATE_FUNCTION}` response function exactly once; it is structured "
            "output, not an application tool, and performs no I/O or effect. Never combine that "
            "response function with a read call. For a clarification, return exactly one JSON "
            "object with decision_type=needs_clarification, a concise decision_summary, empty "
            "tool_calls, null candidate, and one clarification_question. For manual takeover, "
            "use decision_type=manual_takeover with a concise decision_summary, empty tool_calls, "
            "null candidate, and null clarification_question. Never invent a tool result or "
            "request a write tool."
        )
        if any(
            item.get("function", {}).get("name") == TERMINAL_CANDIDATE_FUNCTION for item in tools
        ):
            raise ProviderError("reserved_terminal_candidate_function_collision")
        provider_tools = [*tools, native_terminal_candidate_schema()]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": context},
            *prior_turns,
        ]
        prompt_tokens = 0
        completion_tokens = 0
        try:
            response, transport_attempts = await self._create(
                model=self.settings.llm_model,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.provider_max_output_tokens,
                messages=messages,
                tools=provider_tools,
                tool_choice="auto",
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            if response.usage is not None:
                self.prompt_tokens += response.usage.prompt_tokens
                self.completion_tokens += response.usage.completion_tokens
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens
            message = response.choices[0].message
            if message.tool_calls:
                terminal_content = _terminal_candidate_content(message.tool_calls)
                if terminal_content is not None:
                    output = RawProviderDecision(
                        finish_reason=response.choices[0].finish_reason,
                        content=terminal_content,
                        tool_calls=(),
                    )
                    return ProviderCallResult(
                        output=output,
                        attempts=1,
                        usage=ProviderUsage(prompt_tokens, completion_tokens),
                        trace_metadata={
                            **trace_metadata,
                            "terminal_transport": "native_final_candidate.v1",
                        },
                        transport=self._require_transport(),
                        transport_attempts=transport_attempts,
                    )
                output = RawProviderDecision(
                    finish_reason=response.choices[0].finish_reason,
                    content=message.content,
                    tool_calls=tuple(
                        RawProviderToolCall(
                            provider_tool_call_id=item.id,
                            name=item.function.name,
                            arguments_json=item.function.arguments,
                            ordinal=ordinal,
                        )
                        for ordinal, item in enumerate(message.tool_calls)
                    ),
                )
                return ProviderCallResult(
                    output=output,
                    attempts=1,
                    usage=ProviderUsage(prompt_tokens, completion_tokens),
                    trace_metadata=dict(trace_metadata),
                    transport=self._require_transport(),
                    transport_attempts=transport_attempts,
                )
            if not message.content:
                raise ProviderError("provider returned neither tool calls nor content")
            output = RawProviderDecision(
                finish_reason=response.choices[0].finish_reason,
                content=message.content,
                tool_calls=(),
            )
            return ProviderCallResult(
                output=output,
                attempts=1,
                usage=ProviderUsage(prompt_tokens, completion_tokens),
                trace_metadata=dict(trace_metadata),
                transport=self._require_transport(),
                transport_attempts=transport_attempts,
            )
        except Exception as exc:
            raise ProviderError("native tool decision failed") from exc

    def _require_transport(self) -> ProviderTransportRecord:
        transport = self._transport_capture.get()
        if transport is None:
            raise ProviderError("provider_transport_not_captured")
        return transport
