"""add scheduled_broadcasts table

Revision ID: 0010_add_scheduled_broadcasts
Revises: 0009_add_referral_fields
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_add_scheduled_broadcasts"
down_revision = "0009_add_referral_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_broadcasts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_id",
            sa.Integer(),
            sa.ForeignKey("bot_configs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scheduled_broadcasts_send_at",
        "scheduled_broadcasts",
        ["send_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_broadcasts_send_at", table_name="scheduled_broadcasts")
    op.drop_table("scheduled_broadcasts")
