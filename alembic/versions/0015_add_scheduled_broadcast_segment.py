"""add segment to scheduled_broadcasts

Revision ID: 0015_add_scheduled_broadcast_segment
Revises: 0014_add_subscriber_segments
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_add_scheduled_broadcast_segment"
down_revision = "0014_add_subscriber_segments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_broadcasts", sa.Column("segment", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_broadcasts", "segment")
