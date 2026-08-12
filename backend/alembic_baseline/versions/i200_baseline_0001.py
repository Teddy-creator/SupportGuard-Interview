"""Install the static Interview Edition empty-database baseline.

Revision ID: i200_baseline_0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from supportguard.db.interview_baseline import install_interview_baseline

revision: str = "i200_baseline_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    install_interview_baseline(
        op.get_bind(), artifact_directory=Path(__file__).resolve().parents[1]
    )


def downgrade() -> None:
    raise RuntimeError("interview_baseline_downgrade_forbidden")
