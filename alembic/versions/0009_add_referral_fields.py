"""add referral fields to clients

Revision ID: 0009_add_referral_fields
Revises: 0008_add_bot_subscribers
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_add_referral_fields"
down_revision = "0008_add_bot_subscribers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("referral_code", sa.String(16), nullable=True),
    )
    op.create_unique_constraint("uq_clients_referral_code", "clients", ["referral_code"])
    op.create_index("ix_clients_referral_code", "clients", ["referral_code"])

    op.add_column(
        "clients",
        sa.Column(
            "referred_by_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.add_column(
        "clients",
        sa.Column(
            "referral_reward_sent",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "referral_reward_sent")
    op.drop_column("clients", "referred_by_id")
    op.drop_index("ix_clients_referral_code", table_name="clients")
    op.drop_constraint("uq_clients_referral_code", "clients", type_="unique")
    op.drop_column("clients", "referral_code")
