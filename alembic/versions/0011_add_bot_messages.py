"""add bot_messages table

Revision ID: 0011_add_bot_messages
Revises: 0010_add_scheduled_broadcasts
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_add_bot_messages"
down_revision = "0010_add_scheduled_broadcasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_id",
            sa.Integer(),
            sa.ForeignKey("bot_configs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_bot_messages_bot_id_created_at",
        "bot_messages",
        ["bot_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_bot_messages_bot_id_created_at", table_name="bot_messages")
    op.drop_table("bot_messages")
