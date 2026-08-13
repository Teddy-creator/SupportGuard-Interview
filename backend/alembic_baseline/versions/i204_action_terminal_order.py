"""Converge action terminal state after the enclosing capability finishes.

Revision ID: i204_action_terminal_order
Revises: i203_demo_truthful_refund

The historical approval and withdrawal capabilities update the conversation
turn after inserting their decision record.  A normal AFTER INSERT trigger can
therefore be overwritten later in the same statement.  Deferred constraint
triggers run at transaction completion and make the typed terminal state the
last database-owned projection.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from supportguard.db.interview_baseline import execute_interview_migration_sql

revision: str = "i204_action_terminal_order"
down_revision: str | None = "i203_demo_truthful_refund"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFER_ACTION_TERMINAL_STATE_SQL = r"""
DROP TRIGGER trg_conversation_rejected_state_v203 ON public.human_decisions;
DROP TRIGGER trg_conversation_withdrawn_state_v203 ON public.proposal_withdrawals;

CREATE CONSTRAINT TRIGGER trg_conversation_rejected_state_v204
AFTER INSERT ON public.human_decisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.supportguard_conversation_action_terminal_state_v203();

CREATE CONSTRAINT TRIGGER trg_conversation_withdrawn_state_v204
AFTER INSERT ON public.proposal_withdrawals
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.supportguard_conversation_action_terminal_state_v203();
"""


def upgrade() -> None:
    op.execute("SET LOCAL ROLE supportguard_owner")
    execute_interview_migration_sql(op.get_bind(), _DEFER_ACTION_TERMINAL_STATE_SQL)


def downgrade() -> None:
    raise RuntimeError("interview_action_terminal_order_downgrade_forbidden")
