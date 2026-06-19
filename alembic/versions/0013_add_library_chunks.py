"""add library_chunks table for shared RAG knowledge base

Revision ID: 0013
Revises: 0012_add_platform_to_bot_configs
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0013_add_library_chunks"
down_revision = "0012_add_platform_to_bot_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_library_chunks_bot_type", "library_chunks", ["bot_type"])
    op.execute(
        "CREATE INDEX ix_library_chunks_embedding "
        "ON library_chunks USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_library_chunks_embedding")
    op.drop_index("ix_library_chunks_bot_type", table_name="library_chunks")
    op.drop_table("library_chunks")
