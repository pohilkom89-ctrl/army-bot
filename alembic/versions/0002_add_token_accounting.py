"""0002_add_token_accounting

Revision ID: 0002_add_token_accounting
Revises: 0001_initial
Create Date: 2026-04-13 23:02:30.851867

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_token_accounting"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "tier",
            sa.String(length=32),
            nullable=False,
            server_default="starter",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("tokens_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "tokens_reset_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_table(
        "token_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Float(),
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
            name="fk_token_logs_client_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bot_configs.id"],
            name="fk_token_logs_bot_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_token_logs_client_id", "token_logs", ["client_id"], unique=False
    )
    op.create_index(
        "ix_token_logs_bot_id", "token_logs", ["bot_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_token_logs_bot_id", table_name="token_logs")
    op.drop_index("ix_token_logs_client_id", table_name="token_logs")
    op.drop_table("token_logs")

    op.drop_column("subscriptions", "tokens_reset_at")
    op.drop_column("subscriptions", "tokens_used")
    op.drop_column("subscriptions", "tokens_limit")
    op.drop_column("subscriptions", "tier")
