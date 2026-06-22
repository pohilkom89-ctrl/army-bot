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


def _make_mock_bot(platform: str, bot_id: int = 1):
    from unittest.mock import MagicMock
    bot = MagicMock()
    bot.id = bot_id
    bot.platform = platform
    bot.status = "active"
    return bot


def test_bot_detail_keyboard_telegram_has_library_button():
    """Telegram bots show the '📚 База знаний' button."""
    from main import _bot_detail_keyboard
    kb = _bot_detail_keyboard(_make_mock_bot("telegram"))
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("База знаний" in t for t in texts)


def test_bot_detail_keyboard_vk_hides_library_button():
    """VK bots must NOT show the '📚 База знаний' button — library is Telegram-only."""
    from main import _bot_detail_keyboard
    kb = _bot_detail_keyboard(_make_mock_bot("vk"))
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert not any("База знаний" in t for t in texts)


def test_bot_detail_keyboard_has_subscribers_button():
    """All bots show the '👥 Подписчики' button."""
    from main import _bot_detail_keyboard
    kb = _bot_detail_keyboard(_make_mock_bot("telegram"))
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Подписчики" in t for t in texts)


# ---------------------------------------------------------------------------
# Wave 39: subscriber segments
# ---------------------------------------------------------------------------

async def test_get_segments_empty(fresh_db):
    """No segments returned for a bot with untagged subscribers."""
    from db.repository import get_segments_for_bot, upsert_subscriber, get_or_create_client, save_bot_config
    client = await get_or_create_client(7001, None)
    bot = await save_bot_config(client.id, "SegBot1", "coach", "7001:FAKE", "sys", {})
    await upsert_subscriber(bot.id, 10001)
    segs = await get_segments_for_bot(bot.id)
    assert segs == []


async def test_get_segments_with_tagged_subscribers(fresh_db):
    """get_segments_for_bot returns segments with correct counts."""
    from db.repository import (
        get_segments_for_bot, set_subscriber_segment, upsert_subscriber,
        get_or_create_client, save_bot_config,
    )
    client = await get_or_create_client(7002, None)
    bot = await save_bot_config(client.id, "SegBot2", "coach", "7002:FAKE", "sys", {})
    await upsert_subscriber(bot.id, 20001)
    await upsert_subscriber(bot.id, 20002)
    await upsert_subscriber(bot.id, 20003)
    await set_subscriber_segment(bot.id, 20001, "VIP")
    await set_subscriber_segment(bot.id, 20002, "VIP")
    await set_subscriber_segment(bot.id, 20003, "Новый")
    segs = await get_segments_for_bot(bot.id)
    seg_map = {s["segment"]: s["count"] for s in segs}
    assert seg_map["VIP"] == 2
    assert seg_map["Новый"] == 1


async def test_get_subscriber_ids_by_segment_all(fresh_db):
    """Passing segment=None returns all subscribers."""
    from db.repository import (
        get_subscriber_ids_by_segment, set_subscriber_segment, upsert_subscriber,
        get_or_create_client, save_bot_config,
    )
    client = await get_or_create_client(7003, None)
    bot = await save_bot_config(client.id, "SegBot3", "coach", "7003:FAKE", "sys", {})
    await upsert_subscriber(bot.id, 30001)
    await upsert_subscriber(bot.id, 30002)
    await set_subscriber_segment(bot.id, 30001, "VIP")
    ids = await get_subscriber_ids_by_segment(bot.id, segment=None)
    assert set(ids) == {30001, 30002}


async def test_get_subscriber_ids_by_segment_filtered(fresh_db):
    """Passing a segment returns only subscribers in that segment."""
    from db.repository import (
        get_subscriber_ids_by_segment, set_subscriber_segment, upsert_subscriber,
        get_or_create_client, save_bot_config,
    )
    client = await get_or_create_client(7004, None)
    bot = await save_bot_config(client.id, "SegBot4", "coach", "7004:FAKE", "sys", {})
    await upsert_subscriber(bot.id, 40001)
    await upsert_subscriber(bot.id, 40002)
    await set_subscriber_segment(bot.id, 40001, "VIP")
    vip_ids = await get_subscriber_ids_by_segment(bot.id, segment="VIP")
    assert vip_ids == [40001]
    new_ids = await get_subscriber_ids_by_segment(bot.id, segment="Новый")
    assert new_ids == []


async def test_set_subscriber_segment_returns_false_for_unknown(fresh_db):
    """set_subscriber_segment returns False if subscriber doesn't exist."""
    from db.repository import set_subscriber_segment, get_or_create_client, save_bot_config
    client = await get_or_create_client(7005, None)
    bot = await save_bot_config(client.id, "SegBot5", "coach", "7005:FAKE", "sys", {})
    ok = await set_subscriber_segment(bot.id, 99999, "VIP")
    assert ok is False


async def test_upsert_subscriber_with_segment(fresh_db):
    """upsert_subscriber sets segment on new subscriber."""
    from db.repository import (
        upsert_subscriber, get_segments_for_bot, get_or_create_client, save_bot_config,
    )
    client = await get_or_create_client(7006, None)
    bot = await save_bot_config(client.id, "SegBot6", "coach", "7006:FAKE", "sys", {})
    is_new = await upsert_subscriber(bot.id, 60001, segment="Premium")
    assert is_new is True
    segs = await get_segments_for_bot(bot.id)
    assert segs[0]["segment"] == "Premium"
    assert segs[0]["count"] == 1
