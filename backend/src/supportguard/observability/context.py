from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    trace_id: str
    traceparent: str | None = None


current_request_context: ContextVar[RequestContext | None] = ContextVar(
    "supportguard_request_context", default=None
)


def bind_request_context(context: RequestContext) -> Token[RequestContext | None]:
    return current_request_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    current_request_context.reset(token)
