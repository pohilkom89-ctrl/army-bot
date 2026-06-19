"""RAG — retrieval-augmented generation over per-client knowledge chunks.

Pipeline:
    add_knowledge(client_id, bot_id, text, source)
        → _chunk_text (approximate 500-token chunks with 50-token overlap)
        → OpenAI-compatible /v1/embeddings (text-embedding-3-small, 1536d)
        → INSERT into knowledge_chunks (with CAST(:emb AS vector))

    search_knowledge(client_id, bot_id, query, limit=3)
        → embed query
        → cosine distance search via pgvector `<=>` operator
        → return top-N raw chunk texts (caller is responsible for formatting
          them into a system prompt block)

Embeddings default to OpenRouter's base URL so we share the key with
run_pipeline / run_bot_query, but both the base URL and the model name are
overridable via env vars — if OpenRouter doesn't proxy /v1/embeddings we can
point EMBEDDING_BASE_URL at https://api.openai.com/v1 without touching code.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from loguru import logger
from openai import OpenAI
from sqlalchemy import text

from db.database import get_session
from settings import settings

OPENROUTER_BASE_URL = settings.openrouter_base_url

EMBEDDING_BASE_URL = settings.embedding_base_url
EMBEDDING_MODEL = settings.embedding_model
EMBEDDING_DIM = 1536

# pgvector <=> operator is unavailable in SQLite (used in tests via fresh_db fixture)
_USE_SQLITE = "sqlite" in os.getenv("DATABASE_URL", "").lower()

CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
# Rough EN/RU mix: one whitespace-delimited word ≈ 1.5 tokens. Avoids a
# tiktoken dependency at the cost of ~20% chunk-size variance, which is fine
# for 500-token targets with 50-token overlap.
TOKENS_PER_WORD = 1.5

SEARCH_LIMIT_DEFAULT = 3


_client: OpenAI | None = None


def _get_embedding_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv(
            "OPENROUTER_API_KEY"
        )
        if not api_key:
            raise RuntimeError(
                "EMBEDDING_API_KEY or OPENROUTER_API_KEY env var is required"
            )
        _client = OpenAI(api_key=api_key, base_url=EMBEDDING_BASE_URL)
    return _client


def _embed_batch_sync(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    client = _get_embedding_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL, input=texts
    )
    return [item.embedding for item in response.data]


async def embed_text(text_input: str) -> list[float]:
    vectors = await asyncio.to_thread(_embed_batch_sync, [text_input])
    if not vectors:
        raise RuntimeError("embed_text: empty response from embedding API")
    return vectors[0]


def _chunk_text(
    raw: str,
    chunk_size: int = CHUNK_SIZE_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    words = raw.split()
    if not words:
        return []
    words_per_chunk = max(1, int(chunk_size / TOKENS_PER_WORD))
    overlap_words = max(0, int(overlap / TOKENS_PER_WORD))
    step = max(1, words_per_chunk - overlap_words)

    chunks: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        chunk = " ".join(words[i : i + words_per_chunk]).strip()
        if chunk:
            chunks.append(chunk)
        if i + words_per_chunk >= n:
            break
        i += step
    return chunks


def _vector_to_sql(vec: list[float]) -> str:
    """Serialise a list[float] into pgvector's literal format: '[f1,f2,...]'.

    Avoids registering an asyncpg codec — we just CAST a string to vector on
    every INSERT/SELECT. Cheap and keeps db/database.py untouched.
    """
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def add_knowledge(
    client_id: int,
    bot_id: int | None,
    raw_text: str,
    source: str,
) -> int:
    """Chunk, embed, and store raw_text as knowledge_chunks rows.

    Returns the number of chunks actually stored.
    """
    chunks = _chunk_text(raw_text)
    if not chunks:
        logger.warning(
            "rag.add_knowledge: no chunks produced (source='{}', client_id={})",
            source,
            client_id,
        )
        return 0

    vectors = await asyncio.to_thread(_embed_batch_sync, chunks)
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"rag.add_knowledge: embedding count mismatch "
            f"({len(vectors)} vs {len(chunks)} chunks)"
        )

    async with get_session() as session:
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            await session.execute(
                text(
                    "INSERT INTO knowledge_chunks "
                    "(client_id, bot_id, content, embedding, source, chunk_index) "
                    "VALUES (:cid, :bid, :content, CAST(:emb AS vector), :source, :idx)"
                ),
                {
                    "cid": client_id,
                    "bid": bot_id,
                    "content": chunk,
                    "emb": _vector_to_sql(vec),
                    "source": source,
                    "idx": idx,
                },
            )

    logger.info(
        "rag.add_knowledge: stored {} chunks from '{}' (client_id={}, bot_id={})",
        len(chunks),
        source,
        client_id,
        bot_id,
    )
    return len(chunks)


async def search_knowledge(
    client_id: int,
    bot_id: int | None,
    query: str,
    limit: int = SEARCH_LIMIT_DEFAULT,
    bot_type: str | None = None,
) -> list[str]:
    """Return top-N chunk texts nearest to `query` under cosine distance.

    Scope:
        client_id — always filter (per-client isolation, 152-ФЗ).
        bot_id    — if provided, match rows where bot_id = N OR bot_id IS NULL.
        bot_type  — if provided, also fetch shared library chunks for this type
                    and append them after client-specific results (up to limit).
    """
    query = (query or "").strip()
    if not query:
        return []

    query_vec = await embed_text(query)
    emb_sql = _vector_to_sql(query_vec)

    if _USE_SQLITE:
        if bot_id is not None:
            sql = (
                "SELECT content FROM knowledge_chunks "
                "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL) "
                "LIMIT :lim"
            )
            params: dict = {"cid": client_id, "bid": bot_id, "lim": limit}
        else:
            sql = (
                "SELECT content FROM knowledge_chunks "
                "WHERE client_id = :cid LIMIT :lim"
            )
            params = {"cid": client_id, "lim": limit}
    elif bot_id is not None:
        sql = (
            "SELECT content FROM knowledge_chunks "
            "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL) "
            "ORDER BY embedding <=> CAST(:emb AS vector) "
            "LIMIT :lim"
        )
        params = {"cid": client_id, "bid": bot_id, "emb": emb_sql, "lim": limit}
    else:
        sql = (
            "SELECT content FROM knowledge_chunks "
            "WHERE client_id = :cid "
            "ORDER BY embedding <=> CAST(:emb AS vector) "
            "LIMIT :lim"
        )
        params = {"cid": client_id, "emb": emb_sql, "lim": limit}

    async with get_session() as session:
        result = await session.execute(text(sql), params)
        rows = [row[0] for row in result.all()]

    library_rows: list[str] = []
    if bot_type:
        library_rows = await search_library_chunks(bot_type, query, limit, emb_sql)

    combined = rows + [r for r in library_rows if r not in rows]

    logger.info(
        "rag.search_knowledge: {} client + {} library hits "
        "for client_id={} bot_id={} bot_type={} query_len={}",
        len(rows),
        len(library_rows),
        client_id,
        bot_id,
        bot_type,
        len(query),
    )
    return combined


async def add_library_knowledge(
    bot_type: str,
    raw_text: str,
    source: str,
) -> int:
    """Chunk, embed, and store raw_text in the shared library for bot_type.

    Returns the number of chunks stored.
    """
    chunks = _chunk_text(raw_text)
    if not chunks:
        logger.warning(
            "rag.add_library_knowledge: no chunks produced (source='{}', bot_type={})",
            source,
            bot_type,
        )
        return 0

    vectors = await asyncio.to_thread(_embed_batch_sync, chunks)
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"rag.add_library_knowledge: embedding count mismatch "
            f"({len(vectors)} vs {len(chunks)} chunks)"
        )

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            await session.execute(
                text(
                    "INSERT INTO library_chunks "
                    "(bot_type, content, embedding, source, chunk_index, created_at) "
                    "VALUES (:bt, :content, CAST(:emb AS vector), :source, :idx, :ts)"
                ),
                {
                    "bt": bot_type,
                    "content": chunk,
                    "emb": _vector_to_sql(vec),
                    "source": source,
                    "idx": idx,
                    "ts": now,
                },
            )

    logger.info(
        "rag.add_library_knowledge: stored {} chunks from '{}' (bot_type={})",
        len(chunks),
        source,
        bot_type,
    )
    return len(chunks)


async def search_library_chunks(
    bot_type: str,
    query: str,
    limit: int = SEARCH_LIMIT_DEFAULT,
    precomputed_emb_sql: str | None = None,
) -> list[str]:
    """Return top-N library chunk texts for bot_type nearest to query."""
    if precomputed_emb_sql is None:
        query_vec = await embed_text(query)
        precomputed_emb_sql = _vector_to_sql(query_vec)

    if _USE_SQLITE:
        sql = "SELECT content FROM library_chunks WHERE bot_type = :bt LIMIT :lim"
        params: dict = {"bt": bot_type, "lim": limit}
    else:
        sql = (
            "SELECT content FROM library_chunks "
            "WHERE bot_type = :bt "
            "ORDER BY embedding <=> CAST(:emb AS vector) "
            "LIMIT :lim"
        )
        params = {"bt": bot_type, "emb": precomputed_emb_sql, "lim": limit}
    async with get_session() as session:
        result = await session.execute(text(sql), params)
        return [row[0] for row in result.all()]


async def list_library_sources(bot_type: str | None = None) -> list[tuple[str, str, int]]:
    """Return [(bot_type, source, chunk_count)] sorted by bot_type, count desc."""
    if bot_type:
        sql = (
            "SELECT bot_type, COALESCE(source, '(без названия)') AS src, COUNT(*) AS n "
            "FROM library_chunks WHERE bot_type = :bt "
            "GROUP BY bot_type, src ORDER BY n DESC"
        )
        params: dict = {"bt": bot_type}
    else:
        sql = (
            "SELECT bot_type, COALESCE(source, '(без названия)') AS src, COUNT(*) AS n "
            "FROM library_chunks "
            "GROUP BY bot_type, src ORDER BY bot_type, n DESC"
        )
        params = {}
    async with get_session() as session:
        result = await session.execute(text(sql), params)
        return [(row[0], row[1], int(row[2])) for row in result.all()]


async def clear_library(bot_type: str) -> int:
    """Delete all library chunks for bot_type. Returns count deleted."""
    async with get_session() as session:
        result = await session.execute(
            text("DELETE FROM library_chunks WHERE bot_type = :bt"),
            {"bt": bot_type},
        )
        deleted = int(result.rowcount or 0)
    logger.info("rag.clear_library: deleted {} chunks for bot_type={}", deleted, bot_type)
    return deleted


async def count_knowledge(
    client_id: int, bot_id: int | None = None
) -> int:
    if bot_id is not None:
        sql = (
            "SELECT COUNT(*) FROM knowledge_chunks "
            "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL)"
        )
        params = {"cid": client_id, "bid": bot_id}
    else:
        sql = "SELECT COUNT(*) FROM knowledge_chunks WHERE client_id = :cid"
        params = {"cid": client_id}
    async with get_session() as session:
        result = await session.execute(text(sql), params)
        return int(result.scalar() or 0)


async def list_knowledge_sources(
    client_id: int, bot_id: int | None = None
) -> list[tuple[str, int]]:
    """Return [(source, chunk_count), ...] sorted by chunk_count desc."""
    if bot_id is not None:
        sql = (
            "SELECT COALESCE(source, '(без названия)') AS src, COUNT(*) AS n "
            "FROM knowledge_chunks "
            "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL) "
            "GROUP BY src ORDER BY n DESC"
        )
        params = {"cid": client_id, "bid": bot_id}
    else:
        sql = (
            "SELECT COALESCE(source, '(без названия)') AS src, COUNT(*) AS n "
            "FROM knowledge_chunks "
            "WHERE client_id = :cid "
            "GROUP BY src ORDER BY n DESC"
        )
        params = {"cid": client_id}
    async with get_session() as session:
        result = await session.execute(text(sql), params)
        return [(row[0], int(row[1])) for row in result.all()]


async def clear_knowledge(
    client_id: int, bot_id: int | None = None
) -> int:
    """Delete all knowledge chunks for the client (scoped by bot if given).

    Returns the number of rows deleted.
    """
    if bot_id is not None:
        sql = (
            "DELETE FROM knowledge_chunks "
            "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL)"
        )
        params = {"cid": client_id, "bid": bot_id}
    else:
        sql = "DELETE FROM knowledge_chunks WHERE client_id = :cid"
        params = {"cid": client_id}
    async with get_session() as session:
        result = await session.execute(text(sql), params)
        deleted = int(result.rowcount or 0)
    logger.info(
        "rag.clear_knowledge: deleted {} chunks (client_id={}, bot_id={})",
        deleted,
        client_id,
        bot_id,
    )
    return deleted
