from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from supportguard.api.auth import Principal
from supportguard.api.endpoints import approvals as approval_endpoints

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _PostgresApprovalSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def scalar(
        self,
        statement: object,
        parameters: dict[str, object],
    ) -> dict[str, Any]:
        assert "supportguard_api_get_approval" in str(statement)
        assert parameters == {"approval_id": "approval_route_projection"}
        return self.payload


def _approval_payload(*, include_source: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "approval_route_projection",
        "ticket_id": "ticket_route_projection",
        # Deliberately stale legacy fields. The trusted source bundle below
        # must win before the public Approval DTO is constructed.
        "status": "executed",
        "status_version": 99,
        "action_type": "refund",
        "resource_type": "billing_record_id",
        "resource_id": "bill_route_projection",
        "origin_turn_id": "turn_route_projection",
        "requested_change": {},
        "resource_facts": {
            "status": "charged",
            "amount": "49.00",
            "currency": "USD",
            "version": 2,
        },
        "risk": "high",
        "business_version": 2,
        "actionable": True,
        "proposal_summary": {"status": "bound", "resource_version": 2},
        "snapshot_summary": {
            "resource_version": 2,
            "policy_bound": True,
            "citation_count": 0,
        },
        "ticket_summary": {"status": "awaiting_approval"},
        "created_at": NOW,
        "updated_at": NOW,
    }
    if include_source:
        payload["conversation_action_sources"] = {
            "schema_version": "conversation-action-source-bundle.v1",
            "approval": {
                "id": "approval_route_projection",
                "tenant_id": "tenant_route_projection",
                "ticket_id": "ticket_route_projection",
                "customer_id": "customer_route_projection",
                "proposal_id": "proposal_route_projection",
                "run_id": "run_route_projection",
                "action_type": "refund",
                "resource_type": "billing_record_id",
                "resource_id": "bill_route_projection",
                "origin_turn_id": "turn_route_projection",
                "business_version": 2,
                "status": "pending",
                "status_version": 3,
                "created_at": NOW,
                "updated_at": NOW,
            },
            "proposal": {
                "id": "proposal_route_projection",
                "tenant_id": "tenant_route_projection",
                "run_id": "run_route_projection",
                "action_type": "refund",
                "resource_id": "bill_route_projection",
                "resource_version": 2,
                "status": "bound",
                "created_at": NOW,
                "updated_at": NOW,
            },
            "decision": None,
            "business_action": None,
            "withdrawal": None,
            "runtime_job": None,
        }
    return payload


def _identity() -> Principal:
    return Principal(
        role="approver",
        subject_id="approver_route_projection",
        tenant_id="tenant_route_projection",
        membership_role="support_approver",
    )


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    session = _PostgresApprovalSession(payload)

    @asynccontextmanager
    async def fake_request_session(
        request: object,
        identity: Principal,
    ) -> AsyncIterator[_PostgresApprovalSession]:
        del request, identity
        yield session

    monkeypatch.setattr(approval_endpoints, "request_session", fake_request_session)


@pytest.mark.asyncio
async def test_postgres_approval_detail_uses_trusted_action_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _approval_payload()
    _install_session(monkeypatch, payload)

    projected = await approval_endpoints.approval_detail(
        "approval_route_projection",
        object(),  # type: ignore[arg-type]
        _identity(),
    )

    assert projected.status == "pending"
    assert projected.status_version == 3
    assert projected.actionable is True
    assert projected.allowed_actions == ["approve", "edit_and_approve", "reject"]
    assert "conversation_action_sources" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("source_mutation", ["missing", "cross_tenant"])
async def test_postgres_approval_detail_fails_closed_without_valid_action_source(
    monkeypatch: pytest.MonkeyPatch,
    source_mutation: str,
) -> None:
    payload = _approval_payload(include_source=source_mutation != "missing")
    if source_mutation == "cross_tenant":
        payload["conversation_action_sources"]["approval"]["tenant_id"] = "tenant_other"
    _install_session(monkeypatch, payload)

    with pytest.raises(HTTPException) as exc_info:
        await approval_endpoints.approval_detail(
            "approval_route_projection",
            object(),  # type: ignore[arg-type]
            _identity(),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "projection_invalid"
