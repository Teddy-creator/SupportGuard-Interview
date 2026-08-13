from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

ScenarioHttpOperation = Literal[
    "session_bootstrap",
    "conversation_create",
    "conversation_append",
    "conversation_poll",
]
ScenarioHttpOutcome = Literal["response", "timeout", "transport_error", "deadline_exceeded"]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

_OPERATIONS: tuple[ScenarioHttpOperation, ...] = (
    "session_bootstrap",
    "conversation_create",
    "conversation_append",
    "conversation_poll",
)
_POLL_MAX_ATTEMPTS = 6
_SAFE_REQUEST_ID = re.compile(r"^request_[0-9a-f]{32}$")


@dataclass(frozen=True)
class ScenarioHttpEvent:
    operation: ScenarioHttpOperation
    method: str
    request_ordinal: int
    attempt: int
    outcome: ScenarioHttpOutcome
    elapsed_ms: int
    trace_id: str
    status_code: int | None = None
    request_id: str | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "method": self.method,
            "request_ordinal": self.request_ordinal,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
            "trace_id": self.trace_id,
            "status_code": self.status_code,
            "request_id": self.request_id,
        }


class ScenarioHttpTransportError(RuntimeError):
    """A body-free terminal error from the evaluation HTTP boundary."""

    def __init__(self, *, code: str, operation: ScenarioHttpOperation) -> None:
        super().__init__(code)
        self.code = code
        self.operation = operation


class ScenarioHttpClient:
    """Bounded HTTP owner for one IE-P16 scenario.

    Request bodies, response bodies, cookies, and raw URLs never enter diagnostics.
    Retries are restricted to transport failures on operations that are either
    read-only or protected by the product's HTTP idempotency contract.
    """

    def __init__(
        self,
        base_url: str,
        *,
        scenario_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport)
        self._scenario_id = "".join(
            character if character.isalnum() else "_" for character in scenario_id.casefold()
        )
        self._sleep = sleep
        self._clock = clock
        self._events: list[ScenarioHttpEvent] = []
        self._request_ordinals: dict[ScenarioHttpOperation, int] = {
            operation: 0 for operation in _OPERATIONS
        }

    def deadline_after(self, seconds: float) -> float:
        if seconds <= 0:
            raise ValueError("scenario_http_deadline_must_be_positive")
        return self._clock() + seconds

    def before_deadline(self, deadline: float) -> bool:
        return self._clock() < deadline

    async def aclose(self) -> None:
        await self._client.aclose()

    async def bootstrap_session(
        self,
        *,
        payload: Mapping[str, object],
        deadline: float,
    ) -> httpx.Response:
        return await self._request(
            "POST",
            "/api/demo-sessions",
            operation="session_bootstrap",
            deadline=deadline,
            max_attempts=2,
            json=dict(payload),
        )

    async def submit(
        self,
        path: str,
        *,
        operation: Literal["conversation_create", "conversation_append"],
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        deadline: float,
    ) -> httpx.Response:
        if not any(key.casefold() == "idempotency-key" for key in headers):
            raise ValueError("scenario_http_idempotency_key_required")
        return await self._request(
            "POST",
            path,
            operation=operation,
            deadline=deadline,
            max_attempts=2,
            json=dict(payload),
            headers=dict(headers),
        )

    async def poll(self, path: str, *, deadline: float) -> httpx.Response:
        return await self._request(
            "GET",
            path,
            operation="conversation_poll",
            deadline=deadline,
            max_attempts=_POLL_MAX_ATTEMPTS,
        )

    def diagnostics(self) -> dict[str, object]:
        operations: dict[str, object] = {}
        for operation in _OPERATIONS:
            selected = [event for event in self._events if event.operation == operation]
            if not selected:
                continue
            responses = [event for event in selected if event.outcome == "response"]
            latest = responses[-1] if responses else None
            operations[operation] = {
                "attempts": len(selected),
                "transport_failures": sum(
                    event.outcome in {"timeout", "transport_error", "deadline_exceeded"}
                    for event in selected
                ),
                "last_response": (
                    {
                        "status_code": latest.status_code,
                        "request_id": latest.request_id,
                        "trace_id": latest.trace_id,
                        "elapsed_ms": latest.elapsed_ms,
                    }
                    if latest is not None
                    else None
                ),
            }
        failures = [event.public_dict() for event in self._events if event.outcome != "response"]
        return {
            "schema_version": "ie-p16-http-diagnostics.v1",
            "request_attempts": len(self._events),
            "transport_retry_attempts": sum(event.attempt > 1 for event in self._events),
            "operations": operations,
            "transport_failures": failures[:12],
            "transport_failure_overflow": max(0, len(failures) - 12),
            "payload_or_cookie_recorded": False,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: ScenarioHttpOperation,
        deadline: float,
        max_attempts: int,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self._request_ordinals[operation] += 1
        request_ordinal = self._request_ordinals[operation]
        for attempt in range(1, max_attempts + 1):
            trace_id = f"trace_{self._scenario_id}_{operation}_{request_ordinal}_{attempt}"
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._events.append(
                    ScenarioHttpEvent(
                        operation=operation,
                        method=method,
                        request_ordinal=request_ordinal,
                        attempt=attempt,
                        outcome="deadline_exceeded",
                        elapsed_ms=0,
                        trace_id=trace_id,
                    )
                )
                raise ScenarioHttpTransportError(
                    code="evaluation_http_deadline_exceeded",
                    operation=operation,
                )
            started = self._clock()
            request_headers = {**dict(headers or {}), "X-Trace-ID": trace_id}
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    headers=request_headers,
                    timeout=self._timeout_for(remaining),
                )
            except httpx.TimeoutException as exc:
                self._record_failure(
                    operation=operation,
                    method=method,
                    request_ordinal=request_ordinal,
                    attempt=attempt,
                    outcome="timeout",
                    started=started,
                    trace_id=trace_id,
                )
                if attempt == max_attempts:
                    raise ScenarioHttpTransportError(
                        code="evaluation_http_timeout",
                        operation=operation,
                    ) from exc
            except httpx.TransportError as exc:
                self._record_failure(
                    operation=operation,
                    method=method,
                    request_ordinal=request_ordinal,
                    attempt=attempt,
                    outcome="transport_error",
                    started=started,
                    trace_id=trace_id,
                )
                if attempt == max_attempts:
                    raise ScenarioHttpTransportError(
                        code="evaluation_http_transport_error",
                        operation=operation,
                    ) from exc
            else:
                self._events.append(
                    ScenarioHttpEvent(
                        operation=operation,
                        method=method,
                        request_ordinal=request_ordinal,
                        attempt=attempt,
                        outcome="response",
                        elapsed_ms=self._elapsed_ms(started),
                        trace_id=trace_id,
                        status_code=response.status_code,
                        request_id=self._safe_request_id(response.headers.get("X-Request-ID")),
                    )
                )
                return response
            delay = min(1.0, 0.25 * (2 ** (attempt - 1)), max(0.0, deadline - self._clock()))
            if delay > 0:
                await self._sleep(delay)
        raise AssertionError("scenario HTTP retry loop exhausted")

    def _record_failure(
        self,
        *,
        operation: ScenarioHttpOperation,
        method: str,
        request_ordinal: int,
        attempt: int,
        outcome: Literal["timeout", "transport_error"],
        started: float,
        trace_id: str,
    ) -> None:
        self._events.append(
            ScenarioHttpEvent(
                operation=operation,
                method=method,
                request_ordinal=request_ordinal,
                attempt=attempt,
                outcome=outcome,
                elapsed_ms=self._elapsed_ms(started),
                trace_id=trace_id,
            )
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    @staticmethod
    def _safe_request_id(value: str | None) -> str | None:
        return value if value is not None and _SAFE_REQUEST_ID.fullmatch(value) else None

    @staticmethod
    def _timeout_for(remaining: float) -> httpx.Timeout:
        request_budget = max(0.05, min(30.0, remaining))
        return httpx.Timeout(
            connect=min(5.0, request_budget),
            read=request_budget,
            write=min(10.0, request_budget),
            pool=min(5.0, request_budget),
        )
