"""Tests for Wave 12 — bot conversation log."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"ConvBot{suffix}",
        bot_type="support",
        bot_token=f"888{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_conversations_empty_by_default(fresh_db):
    from db.repository import get_bot_recent_conversations
    client = await _make_client(101001)
    bot = await _make_bot(client.id, 1)
    msgs = await get_bot_recent_conversations(bot.id, client.id)
    assert msgs == []


async def test_conversations_wrong_owner_returns_none(fresh_db):
    from db.repository import get_bot_recent_conversations
    owner = await _make_client(101002)
    other = await _make_client(101003)
    bot = await _make_bot(owner.id, 2)
    result = await get_bot_recent_conversations(bot.id, other.id)
    assert result is None


async def test_log_and_retrieve_message(fresh_db):
    from db.repository import log_bot_message, get_bot_recent_conversations
    client = await _make_client(101004)
    bot = await _make_bot(client.id, 3)
    await log_bot_message(bot.id, 12345, "alice", "user", "Привет!")
    await log_bot_message(bot.id, 12345, "alice", "bot", "Здравствуйте!")
    msgs = await get_bot_recent_conversations(bot.id, client.id)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "bot"   # newest first
    assert msgs[1]["role"] == "user"


async def test_message_fields(fresh_db):
    from db.repository import log_bot_message, get_bot_recent_conversations
    client = await _make_client(101005)
    bot = await _make_bot(client.id, 4)
    await log_bot_message(bot.id, 99999, "bob", "user", "Тест")
    msgs = await get_bot_recent_conversations(bot.id, client.id)
    m = msgs[0]
    assert m["telegram_id"] == 99999
    assert m["username"] == "bob"
    assert m["role"] == "user"
    assert m["text"] == "Тест"
    assert m["created_at"] is not None


async def test_text_truncated_at_2000(fresh_db):
    from db.repository import log_bot_message, get_bot_recent_conversations
    client = await _make_client(101006)
    bot = await _make_bot(client.id, 5)
    long_text = "x" * 5000
    await log_bot_message(bot.id, 11111, None, "user", long_text)
    msgs = await get_bot_recent_conversations(bot.id, client.id)
    assert len(msgs[0]["text"]) == 2000


async def test_bots_isolated(fresh_db):
    from db.repository import log_bot_message, get_bot_recent_conversations
    client = await _make_client(101007)
    bot_a = await _make_bot(client.id, 6)
    bot_b = await _make_bot(client.id, 7)
    await log_bot_message(bot_a.id, 1111, None, "user", "msg A")
    msgs_a = await get_bot_recent_conversations(bot_a.id, client.id)
    msgs_b = await get_bot_recent_conversations(bot_b.id, client.id)
    assert len(msgs_a) == 1
    assert len(msgs_b) == 0


async def test_limit_parameter(fresh_db):
    from db.repository import log_bot_message, get_bot_recent_conversations
    client = await _make_client(101008)
    bot = await _make_bot(client.id, 8)
    for i in range(10):
        await log_bot_message(bot.id, 2222, None, "user", f"msg {i}")
    msgs = await get_bot_recent_conversations(bot.id, client.id, limit=5)
    assert len(msgs) == 5
