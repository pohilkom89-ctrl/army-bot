"""0007_add_merged_into

Revision ID: 0007_add_merged_into
Revises: 0006_add_limit_alerts
Create Date: 2026-05-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_add_merged_into"
down_revision: Union[str, None] = "0006_add_limit_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bot_configs",
        sa.Column("merged_into", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_configs", "merged_into")
