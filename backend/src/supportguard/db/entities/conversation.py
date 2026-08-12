"""Conversation domain ORM entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from supportguard.db.base import Base, TimestampMixin
from supportguard.db.entities.foundation import (
    new_id,
    tenant_resource_fk,
    ticket_customer_scope_fk,
)


class SupportTicket(TimestampMixin, Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_ticket_customer_status", "customer_id", "status"),
        Index(
            "ix_ticket_customer_last_message",
            "tenant_id",
            "customer_id",
            "last_message_at",
            "id",
        ),
        Index(
            "ix_support_tickets_title_search",
            text("to_tsvector('simple', coalesce(title,''))"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint("tenant_id", "id", name="uq_support_tickets_tenant_id_id"),
        CheckConstraint(
            "next_dispatch_sequence >= 0",
            name="support_ticket_dispatch_sequence_nonnegative",
        ),
        tenant_resource_fk("customer_id", "customers", name="fk_support_tickets_tenant_customers"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ticket"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    automation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    title: Mapped[str | None] = mapped_column(String(200))
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    risk: Mapped[str] = mapped_column(String(32), nullable=False, default="low")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    next_message_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    next_dispatch_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_response: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TicketMessage(TimestampMixin, Base):
    __tablename__ = "ticket_messages"
    __table_args__ = (
        Index("ix_message_ticket_created", "ticket_id", "created_at"),
        Index(
            "ix_v1512_ticket_message_approval_alias",
            "tenant_id",
            "ticket_id",
            "approval_id",
            postgresql_where=text("approval_id IS NOT NULL"),
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_ticket_messages_content_search",
            text("to_tsvector('simple', content)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "ticket_id", "conversation_sequence", name="uq_ticket_message_conversation_sequence"
        ),
        UniqueConstraint("tenant_id", "publication_key", name="uq_ticket_message_publication"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_ticket_messages_tenant_support_tickets"
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_ticket_messages_tenant_agent_runs"),
        tenant_resource_fk(
            "approval_id",
            "approval_requests",
            name="fk_ticket_messages_tenant_approval_requests",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("msg"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversation_turns.id", use_alter=True, name="fk_ticket_message_turn"),
    )
    run_id: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(64))
    conversation_sequence: Mapped[int | None] = mapped_column(BigInteger)
    message_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="customer")
    publication_key: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)


class ConversationTurn(TimestampMixin, Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint(
            "result_state IS NULL OR result_state IN ("
            "'answered','answered_limited','needs_clarification','refused',"
            "'proposal_created','human_queue','failed','stale'"
            ")",
            name="ck_conversation_turn_result_state",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversation_turns_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "ticket_id",
            "run_id",
            name="uq_conversation_turn_origin_scope",
        ),
        UniqueConstraint("ticket_id", "ordinal", name="uq_conversation_turn_ticket_ordinal"),
        UniqueConstraint("customer_message_id", name="uq_conversation_turn_customer_message"),
        UniqueConstraint("run_id", name="uq_conversation_turn_run"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_conversation_turn_ticket_scope"
        ),
        tenant_resource_fk("run_id", "agent_runs", name="fk_conversation_turn_run_scope"),
        Index("ix_conversation_turn_dispatch", "ticket_id", "activity_state", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("turn"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ticket_messages.id"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activity_state: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    result_state: Mapped[str | None] = mapped_column(String(32))
    automation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    model: Mapped[str | None] = mapped_column(String(128))
    provider_mode: Mapped[str | None] = mapped_column(String(32))
    tool_call_mode: Mapped[str | None] = mapped_column(String(32))
    context_version: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketSummary(TimestampMixin, Base):
    __tablename__ = "ticket_summaries"
    __table_args__ = (
        ticket_customer_scope_fk(name="fk_ticket_summary_ticket_customer_scope"),
        UniqueConstraint("ticket_id", name="uq_ticket_summary_ticket"),
        tenant_resource_fk(
            "ticket_id", "support_tickets", name="fk_ticket_summaries_tenant_support_tickets"
        ),
        tenant_resource_fk("customer_id", "customers", name="fk_ticket_summaries_tenant_customers"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("summary"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("support_tickets.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    attempted_actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    open_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    freshness_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
