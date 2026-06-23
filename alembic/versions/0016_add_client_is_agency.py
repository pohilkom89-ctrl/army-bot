"""add is_agency to clients

Revision ID: 0016_add_client_is_agency
Revises: 0015_sched_broadcast_seg
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_add_client_is_agency"
down_revision = "0015_sched_broadcast_seg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("is_agency", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("clients", "is_agency")
