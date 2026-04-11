"""initial schema: clients, bot_configs, consent_logs, subscriptions

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("consent_given", sa.Boolean(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_text", sa.Text(), nullable=True),
        sa.Column("data_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_id", name="uq_clients_telegram_id"),
    )
    op.create_index(
        "ix_clients_telegram_id", "clients", ["telegram_id"], unique=False
    )

    op.create_table(
        "bot_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("bot_name", sa.String(length=128), nullable=False),
        sa.Column("bot_type", sa.String(length=32), nullable=False),
        sa.Column("bot_token", sa.String(length=128), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_bot_configs_client_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_bot_configs_client_id", "bot_configs", ["client_id"], unique=False
    )

    op.create_table(
        "consent_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_consent_logs_client_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_consent_logs_client_id", "consent_logs", ["client_id"], unique=False
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("yukassa_payment_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_subscriptions_client_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_subscriptions_client_id",
        "subscriptions",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_yukassa_payment_id",
        "subscriptions",
        ["yukassa_payment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_yukassa_payment_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_client_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_consent_logs_client_id", table_name="consent_logs")
    op.drop_table("consent_logs")

    op.drop_index("ix_bot_configs_client_id", table_name="bot_configs")
    op.drop_table("bot_configs")

    op.drop_index("ix_clients_telegram_id", table_name="clients")
    op.drop_table("clients")
