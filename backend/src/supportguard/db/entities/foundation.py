"""Shared ORM primitives with no dependency on mapped entities."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import ForeignKeyConstraint


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def tenant_resource_fk(local_id: str, parent_table: str, *, name: str) -> ForeignKeyConstraint:
    """Bind a tenant-owned child to a parent in the same tenant."""

    return ForeignKeyConstraint(
        ["tenant_id", local_id],
        [f"{parent_table}.tenant_id", f"{parent_table}.id"],
        name=name,
    )


def runtime_job_scope_fk(*, name: str) -> ForeignKeyConstraint:
    """Bind tenant, run, and job as one indivisible runtime identity."""

    return ForeignKeyConstraint(
        ["tenant_id", "run_id", "job_id"],
        ["runtime_jobs.tenant_id", "runtime_jobs.run_id", "runtime_jobs.id"],
        name=name,
    )


def tenant_run_scope_fk(*, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.id"],
        name=name,
    )


def ticket_customer_scope_fk(*, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "ticket_id", "customer_id"],
        [
            "support_tickets.tenant_id",
            "support_tickets.id",
            "support_tickets.customer_id",
        ],
        name=name,
    )


def run_ticket_customer_scope_fk(*, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "run_id", "ticket_id", "customer_id"],
        [
            "agent_runs.tenant_id",
            "agent_runs.id",
            "agent_runs.ticket_id",
            "agent_runs.customer_id",
        ],
        name=name,
    )
