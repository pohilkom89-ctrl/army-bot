"""add miniapp_url to bot_configs

Revision ID: 0017_add_miniapp_url
Revises: 0016_add_client_is_agency
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_add_miniapp_url"
down_revision = "0016_add_client_is_agency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_configs",
        sa.Column("miniapp_url", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_configs", "miniapp_url")
