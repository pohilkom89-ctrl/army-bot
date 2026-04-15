"""0004_add_vector_storage

Revision ID: 0004_add_vector_storage
Revises: 0003_add_chat_history
Create Date: 2026-04-15 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "0004_add_vector_storage"
down_revision: Union[str, None] = "0003_add_chat_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extension is created on the server out-of-band, but keep this call
    # idempotent so `alembic upgrade head` on a fresh DB also works.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=True),
        sa.Column(
            "chunk_index",
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
            name="fk_knowledge_chunks_client_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bot_configs.id"],
            name="fk_knowledge_chunks_bot_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_client_id",
        "knowledge_chunks",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_chunks_bot_id",
        "knowledge_chunks",
        ["bot_id"],
        unique=False,
    )

    # IVFFlat index for approximate cosine similarity search. lists=100 is
    # a reasonable default for up to ~1M rows. The index works on an empty
    # table; pgvector builds cluster centroids lazily after enough inserts.
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding "
        "ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding")
    op.drop_index(
        "ix_knowledge_chunks_bot_id", table_name="knowledge_chunks"
    )
    op.drop_index(
        "ix_knowledge_chunks_client_id", table_name="knowledge_chunks"
    )
    op.drop_table("knowledge_chunks")
