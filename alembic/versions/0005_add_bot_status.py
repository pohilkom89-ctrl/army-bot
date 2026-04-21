"""0005_add_bot_status

Revision ID: 0005_add_bot_status
Revises: 0004_add_vector_storage
Create Date: 2026-04-21 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_add_bot_status"
down_revision: Union[str, None] = "0004_add_vector_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bot_configs",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_configs", "status")
