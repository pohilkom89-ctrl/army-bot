"""0008_add_bot_subscribers

Revision ID: 0008_add_bot_subscribers
Revises: 0007_add_merged_into
Create Date: 2026-06-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_add_bot_subscribers"
down_revision: Union[str, None] = "0007_add_merged_into"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bot_subscribers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_id",
            sa.Integer(),
            sa.ForeignKey("bot_configs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("bot_id", "telegram_id", name="uq_bot_subscriber"),
    )


def downgrade() -> None:
    op.drop_table("bot_subscribers")
