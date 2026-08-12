from __future__ import annotations

import io
import json
import logging

from current_predicate_facts import record_predicate_operands
from supportguard.observability.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from supportguard.observability.logging import configure_json_logging


def test_json_logging_adds_correlation_and_redacts_secret_like_values() -> None:
    configure_json_logging(service="test-worker")
    root = logging.getLogger()
    stream = io.StringIO()
    root.handlers[0].setStream(stream)
    token = bind_request_context(RequestContext("request_safe", "trace_safe"))
    try:
        logging.getLogger("supportguard.test").error(
            "provider failed Authorization=Bearer sk-this-is-a-secret-value",
            extra={"event": "provider_failed", "job_id": "job_safe"},
        )
    finally:
        reset_request_context(token)

    payload = json.loads(stream.getvalue())
    assert payload["service"] == "test-worker"
    assert payload["event"] == "provider_failed"
    assert payload["request_id"] == "request_safe"
    assert payload["trace_id"] == "trace_safe"
    assert payload["job_id"] == "job_safe"
    assert "sk-this-is-a-secret-value" not in stream.getvalue()
    assert "[REDACTED_SECRET]" in payload["message"]
    record_predicate_operands(
        requirement_id="C5-P0-18",
        predicate_id="message_args_redacted",
        subject_kind="structured_log_redaction",
        operands={
            "unsafe_secret_count": stream.getvalue().count("sk-this-is-a-secret-value"),
            "redacted_secret_count": stream.getvalue().count("[REDACTED_SECRET]"),
            "request_id": payload["request_id"],
            "trace_id": payload["trace_id"],
            "safe_error_code_preserved": payload["event"],
        },
    )


def test_json_logging_recursively_redacts_args_extra_and_exception() -> None:
    configure_json_logging(service="test-worker")
    root = logging.getLogger()
    stream = io.StringIO()
    root.handlers[0].setStream(stream)
    try:
        try:
            raise RuntimeError("upstream sk-exception-secret-12345 for nested@example.com")
        except RuntimeError:
            logging.getLogger("supportguard.test").exception(
                "request failed for %s with %s",
                "person@example.com",
                "Bearer sk-argument-secret-12345",
                extra={
                    "event": "provider_failed",
                    "payload": {
                        "customer": {"email": "nested@example.com"},
                        "authorization": "Bearer sk-extra-secret-12345",
                        "items": ["+8613812345678", {"api_key": "key-extra-secret-12345"}],
                    },
                    "error_code": "provider_unavailable",
                },
            )
    finally:
        output = stream.getvalue()

    payload = json.loads(output)
    for unsafe in (
        "person@example.com",
        "nested@example.com",
        "sk-exception-secret-12345",
        "sk-argument-secret-12345",
        "sk-extra-secret-12345",
        "key-extra-secret-12345",
        "13812345678",
    ):
        assert unsafe not in output
    assert payload["error_code"] == "provider_unavailable"
    assert payload["payload"]["authorization"] == "[REDACTED_SECRET]"
    assert "[REDACTED_PII]" in output
    assert "[REDACTED_SECRET]" in payload["exception"]
    operands = {
        "unsafe_values": [
            unsafe
            for unsafe in (
                "person@example.com",
                "nested@example.com",
                "sk-exception-secret-12345",
                "sk-argument-secret-12345",
                "sk-extra-secret-12345",
                "key-extra-secret-12345",
                "13812345678",
            )
            if unsafe in output
        ],
        "payload_authorization": payload["payload"]["authorization"],
        "redacted_pii_count": output.count("[REDACTED_PII]"),
        "redacted_secret_count": output.count("[REDACTED_SECRET]"),
        "exception": payload["exception"],
        "error_code": payload["error_code"],
    }
    for predicate_id in ("extra_nested_redacted", "exception_redacted"):
        record_predicate_operands(
            requirement_id="C5-P0-18",
            predicate_id=predicate_id,
            subject_kind="structured_log_recursive_redaction",
            operands=operands,
        )
