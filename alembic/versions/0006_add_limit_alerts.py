"""0006_add_limit_alerts

Revision ID: 0006_add_limit_alerts
Revises: 0005_add_bot_status
Create Date: 2026-04-21 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_add_limit_alerts"
down_revision: Union[str, None] = "0005_add_bot_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "limit_alerts_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "limit_alerts_enabled")
