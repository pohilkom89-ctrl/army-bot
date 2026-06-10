"""add platform to bot_configs

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_configs",
        sa.Column(
            "platform",
            sa.String(16),
            nullable=False,
            server_default="telegram",
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_configs", "platform")
