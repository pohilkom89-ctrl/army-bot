"""Tests for Wave 9 — upsert_subscriber returns is_new flag,
get_bot_owner_telegram_id returns correct owner."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"NotifyBot{suffix}",
        bot_type="support",
        bot_token=f"555{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_upsert_returns_true_for_new(fresh_db):
    from db.repository import upsert_subscriber
    client = await _make_client(77001)
    bot = await _make_bot(client.id, 1)
    is_new = await upsert_subscriber(bot.id, 12345)
    assert is_new is True


async def test_upsert_returns_false_for_existing(fresh_db):
    from db.repository import upsert_subscriber
    client = await _make_client(77002)
    bot = await _make_bot(client.id, 2)
    await upsert_subscriber(bot.id, 12346)
    is_new = await upsert_subscriber(bot.id, 12346)
    assert is_new is False


async def test_upsert_different_bots_both_new(fresh_db):
    from db.repository import upsert_subscriber
    client = await _make_client(77003)
    bot_a = await _make_bot(client.id, 3)
    bot_b = await _make_bot(client.id, 4)
    assert await upsert_subscriber(bot_a.id, 12347) is True
    assert await upsert_subscriber(bot_b.id, 12347) is True


async def test_get_bot_owner_telegram_id(fresh_db):
    from db.repository import get_bot_owner_telegram_id
    client = await _make_client(77004)
    bot = await _make_bot(client.id, 5)
    owner_tg_id = await get_bot_owner_telegram_id(bot.id)
    assert owner_tg_id == 77004


async def test_get_bot_owner_telegram_id_not_found(fresh_db):
    from db.repository import get_bot_owner_telegram_id
    result = await get_bot_owner_telegram_id(999999)
    assert result is None


async def test_upsert_count_matches(fresh_db):
    from db.repository import upsert_subscriber, count_subscribers
    client = await _make_client(77005)
    bot = await _make_bot(client.id, 6)
    await upsert_subscriber(bot.id, 1)
    await upsert_subscriber(bot.id, 2)
    await upsert_subscriber(bot.id, 2)  # duplicate
    count = await count_subscribers(bot.id)
    assert count == 2
