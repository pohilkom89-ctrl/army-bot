"""add subscriber segments

Revision ID: 0014_add_subscriber_segments
Revises: 0013_add_library_chunks
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_add_subscriber_segments"
down_revision = "0013_add_library_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_subscribers", sa.Column("segment", sa.String(64), nullable=True))
    op.create_index(
        "ix_bot_subscribers_bot_segment", "bot_subscribers", ["bot_id", "segment"]
    )


def downgrade() -> None:
    op.drop_index("ix_bot_subscribers_bot_segment", table_name="bot_subscribers")
    op.drop_column("bot_subscribers", "segment")
