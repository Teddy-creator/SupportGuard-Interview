from __future__ import annotations

import pytest
from pydantic import ValidationError

from supportguard.api.auth import Principal
from supportguard.db.models import (
    AgentEvent,
    AgentRun,
    ApiUsageSnapshot,
    ApprovalRequest,
    BillingRecord,
    Customer,
    EscalationRecord,
    Subscription,
    SupportTicket,
    TicketMessage,
    TicketSummary,
    tenant_resource_fk,
)

TENANT_OWNED_MODELS = (
    Customer,
    Subscription,
    ApiUsageSnapshot,
    BillingRecord,
    SupportTicket,
    TicketMessage,
    AgentRun,
    AgentEvent,
    EscalationRecord,
    ApprovalRequest,
    TicketSummary,
)


def test_runtime_models_have_no_implicit_demo_tenant_default() -> None:
    for model in TENANT_OWNED_MODELS:
        column = model.__table__.c.tenant_id
        assert column.default is None, model.__name__
        assert column.server_default is None, model.__name__
        assert column.nullable is False, model.__name__


def test_trusted_principal_requires_an_explicit_tenant() -> None:
    with pytest.raises(ValidationError):
        Principal.model_validate({"role": "approver", "subject_id": "approver"})


def test_tenant_owned_relationships_are_composite_in_orm_metadata() -> None:
    expected = {
        ("subscriptions", "fk_subscriptions_tenant_customers"),
        ("support_tickets", "fk_support_tickets_tenant_customers"),
        ("ticket_messages", "fk_ticket_messages_tenant_support_tickets"),
        ("agent_runs", "fk_agent_runs_tenant_customers"),
        ("agent_runs", "fk_agent_runs_tenant_support_tickets"),
        ("agent_events", "fk_agent_events_tenant_agent_runs"),
        ("approval_requests", "fk_approval_requests_tenant_customers"),
        ("approval_requests", "fk_approval_requests_tenant_support_tickets"),
        ("runtime_jobs", "fk_runtime_jobs_tenant_agent_runs"),
    }
    observed: set[tuple[str, str]] = set()
    for table in Customer.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if (
                constraint.name is not None
                and len(constraint.columns) == 2
                and "tenant_id" in constraint.columns
            ):
                observed.add((table.name, str(constraint.name)))
    assert expected <= observed


def test_composite_tenant_fk_helper_never_accepts_an_implicit_tenant() -> None:
    constraint = tenant_resource_fk(
        "customer_id", "customers", name="fk_contract_probe"
    )
    assert tuple(element.target_fullname for element in constraint.elements) == (
        "customers.tenant_id",
        "customers.id",
    )
