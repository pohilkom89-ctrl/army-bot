"""0003_add_chat_history

Revision ID: 0003_add_chat_history
Revises: 0002_add_token_accounting
Create Date: 2026-04-15 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_chat_history"
down_revision: Union[str, None] = "0002_add_token_accounting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_chat_history_client_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bot_configs.id"],
            name="fk_chat_history_bot_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_chat_history_client_id", "chat_history", ["client_id"], unique=False
    )
    op.create_index(
        "ix_chat_history_bot_id", "chat_history", ["bot_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_chat_history_bot_id", table_name="chat_history")
    op.drop_index("ix_chat_history_client_id", table_name="chat_history")
    op.drop_table("chat_history")
