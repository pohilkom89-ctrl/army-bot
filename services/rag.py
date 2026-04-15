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

from loguru import logger
from openai import OpenAI
from sqlalchemy import text

from db.database import get_session

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", OPENROUTER_BASE_URL)
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "openai/text-embedding-3-small"
)
EMBEDDING_DIM = 1536

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
) -> list[str]:
    """Return top-N chunk texts nearest to `query` under cosine distance.

    Scope:
        client_id — always filter (per-client isolation, 152-ФЗ).
        bot_id    — if provided, match rows where bot_id = N OR bot_id IS NULL
                    (client-global chunks are visible to any of their bots).
    """
    query = (query or "").strip()
    if not query:
        return []

    query_vec = await embed_text(query)
    emb_sql = _vector_to_sql(query_vec)

    if bot_id is not None:
        sql = (
            "SELECT content FROM knowledge_chunks "
            "WHERE client_id = :cid AND (bot_id = :bid OR bot_id IS NULL) "
            "ORDER BY embedding <=> CAST(:emb AS vector) "
            "LIMIT :lim"
        )
        params = {
            "cid": client_id,
            "bid": bot_id,
            "emb": emb_sql,
            "lim": limit,
        }
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

    logger.info(
        "rag.search_knowledge: {} hits for client_id={} bot_id={} query_len={}",
        len(rows),
        client_id,
        bot_id,
        len(query),
    )
    return rows
