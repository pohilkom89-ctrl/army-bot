"""Tests for Wave 35 — shared library RAG (library_chunks table)."""
from unittest.mock import AsyncMock, patch


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, bot_type: str = "coach", suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"LibBot{suffix}",
        bot_type=bot_type,
        bot_token=f"999{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


def _fake_embed(texts):
    """Return deterministic fake 1536-dim embeddings."""
    return [[float(i % 100) / 100.0] + [0.0] * 1535 for i in range(len(texts))]


async def test_add_library_knowledge_returns_chunk_count(fresh_db):
    from services.rag import add_library_knowledge
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        n = await add_library_knowledge(
            bot_type="coach",
            raw_text="Habit 1: Be Proactive. " * 50,
            source="7 Habits",
        )
    assert n > 0


async def test_library_chunks_isolated_by_bot_type(fresh_db):
    """Library chunks for 'coach' are not returned when searching for 'support'."""
    from services.rag import add_library_knowledge, search_library_chunks
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "Covey habit text " * 50, "7 Habits")

    with patch("services.rag.embed_text", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.0] * 1536
        with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
            coach_results = await search_library_chunks("coach", "habits")
            support_results = await search_library_chunks("support", "habits")

    assert len(coach_results) > 0
    assert len(support_results) == 0


async def test_list_library_sources_empty(fresh_db):
    from services.rag import list_library_sources
    sources = await list_library_sources()
    assert sources == []


async def test_list_library_sources_after_add(fresh_db):
    from services.rag import add_library_knowledge, list_library_sources
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "text " * 60, "Book A")
        await add_library_knowledge("coach", "more " * 60, "Book B")
        await add_library_knowledge("personal_ai", "ai text " * 60, "AI Guide")

    sources = await list_library_sources()
    bot_types = {s[0] for s in sources}
    assert "coach" in bot_types
    assert "personal_ai" in bot_types


async def test_list_library_sources_filtered_by_type(fresh_db):
    from services.rag import add_library_knowledge, list_library_sources
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "coach text " * 60, "Coach Book")
        await add_library_knowledge("support", "support text " * 60, "Support Book")

    coach_sources = await list_library_sources("coach")
    assert all(s[0] == "coach" for s in coach_sources)
    assert len(coach_sources) == 1


async def test_clear_library(fresh_db):
    from services.rag import add_library_knowledge, clear_library, list_library_sources
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "text " * 60, "Book")
    deleted = await clear_library("coach")
    assert deleted > 0
    sources = await list_library_sources("coach")
    assert sources == []


async def test_clear_library_does_not_affect_other_types(fresh_db):
    from services.rag import add_library_knowledge, clear_library, list_library_sources
    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "coach text " * 60, "Coach Book")
        await add_library_knowledge("support", "support text " * 60, "Support Book")
    await clear_library("coach")
    support_sources = await list_library_sources("support")
    assert len(support_sources) == 1


async def test_search_knowledge_includes_library(fresh_db):
    """search_knowledge with bot_type returns library chunks in addition to client chunks."""
    from db.repository import get_or_create_client
    from services.rag import add_library_knowledge, search_knowledge
    client = await get_or_create_client(123456, None)

    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "Proactive habits leadership " * 50, "7 Habits")

    with patch("services.rag.embed_text", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.0] * 1536
        results = await search_knowledge(
            client_id=client.id,
            bot_id=None,
            query="habits",
            limit=3,
            bot_type="coach",
        )
    assert len(results) > 0


async def test_search_knowledge_without_bot_type_skips_library(fresh_db):
    """search_knowledge without bot_type does not return library chunks."""
    from db.repository import get_or_create_client
    from services.rag import add_library_knowledge, search_knowledge
    client = await get_or_create_client(234567, None)

    with patch("services.rag._embed_batch_sync", side_effect=_fake_embed):
        await add_library_knowledge("coach", "Proactive habits " * 50, "7 Habits")

    with patch("services.rag.embed_text", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.0] * 1536
        results = await search_knowledge(
            client_id=client.id,
            bot_id=None,
            query="habits",
            limit=3,
        )
    # No client chunks and no bot_type → empty
    assert results == []
