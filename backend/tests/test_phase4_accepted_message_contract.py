from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from supportguard.api.messages import AcceptedMessage
from supportguard.main import create_app

MESSAGES_PATH = Path("backend/src/supportguard/api/messages.py")
ACTIONS_PATH = Path("backend/src/supportguard/api/endpoints/actions.py")


def _message(**updates: object) -> AcceptedMessage:
    payload: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "customer_id": "cust_demo",
        "principal_id": "user_customer_demo",
        "idempotency_key": "message-command-1",
        "message": "请检查当前并发限制。",
        "trace_id": "trace_message_1",
    }
    payload.update(updates)
    return AcceptedMessage.model_validate(payload)


def test_accepted_message_is_the_frozen_authority_free_ingress_stage() -> None:
    message = _message()

    assert message.schema_version == "accepted-message.v1"
    assert message.ticket_id is None
    assert message.grants_action_authority is False
    with pytest.raises(ValidationError):
        message.message = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _message(grants_action_authority=True)


def test_new_and_follow_up_messages_share_one_canonical_entry() -> None:
    source = MESSAGES_PATH.read_text()
    legacy_source = ACTIONS_PATH.read_text()

    assert "class AcceptedMessage" in source
    assert source.count("await _accept_message(") == 2
    assert "CommandCoordinator" not in legacy_source
    assert '"/tickets"' not in legacy_source
    assert '"/tickets/{ticket_id}/messages"' not in legacy_source


def test_message_entry_functions_respect_the_phase4_decision_budget() -> None:
    tree = ast.parse(MESSAGES_PATH.read_text())
    functions = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
    }

    assert functions
    assert max(functions.values()) < 200
    assert functions["_accept_message"] < 50


def test_message_route_surface_is_unchanged() -> None:
    schema = create_app(testing=True).openapi()

    for path in (
        "/api/conversations",
        "/api/tickets",
        "/api/conversations/{ticket_id}/messages",
        "/api/tickets/{ticket_id}/messages",
    ):
        assert "post" in schema["paths"][path]
