from __future__ import annotations

import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from supportguard.observability.context import current_request_context

_SECRET_PATTERN = re.compile(
    r"(?i)(?:sk-|key-|bearer\s+)[A-Za-z0-9._-]{12,}|"
    r"(?:DEEPSEEK_API_KEY|authorization|api[_-]?key)\s*[=:]\s*\S+"
)
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}


def _redact_text(value: str) -> str:
    value = _SECRET_PATTERN.sub("[REDACTED_SECRET]", value)
    value = _EMAIL_PATTERN.sub("[REDACTED_PII]", value)
    return _PHONE_PATTERN.sub("[REDACTED_PII]", value)


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY_PATTERN.search(key):
        return "[REDACTED_SECRET]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_redact_value(item) for item in value), key=str)
    return value


class SafeContextFilter(logging.Filter):
    """Attach bounded correlation fields and redact accidental secret-like text."""

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        context = current_request_context.get()
        record.service = self.service
        record.request_id = context.request_id if context is not None else None
        record.trace_id = context.trace_id if context is not None else None
        record.msg = _redact_value(record.msg)
        record.args = _redact_value(record.args)
        for key, value in tuple(record.__dict__.items()):
            if key not in _STANDARD_LOG_RECORD_FIELDS:
                setattr(record, key, _redact_value(value, key=key))
        if record.exc_info is not None:
            rendered_exception = "".join(traceback.format_exception(*record.exc_info))
            record.exception = _redact_text(rendered_exception)
            record.exc_info = None
            record.exc_text = None
        return True


class UTCJsonFormatter(JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.now(UTC).isoformat()
        log_record["level"] = record.levelname.lower()
        log_record.setdefault("event", record.getMessage())
        safe = _redact_value(log_record)
        log_record.clear()
        log_record.update(safe)


def configure_json_logging(*, service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SafeContextFilter(service=service))
    handler.setFormatter(
        UTCJsonFormatter(
            "%(levelname)s %(name)s %(message)s %(service)s %(request_id)s %(trace_id)s"
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
