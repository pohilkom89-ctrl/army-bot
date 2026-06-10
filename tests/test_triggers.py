"""Tests for Wave 13 — keyword triggers."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"TriggerBot{suffix}",
        bot_type="support",
        bot_token=f"999{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_triggers_empty_by_default(fresh_db):
    from db.repository import get_triggers
    client = await _make_client(110001)
    bot = await _make_bot(client.id, 1)
    triggers = await get_triggers(bot.id, client.id)
    assert triggers == {}


async def test_triggers_wrong_owner_returns_none(fresh_db):
    from db.repository import get_triggers
    owner = await _make_client(110002)
    other = await _make_client(110003)
    bot = await _make_bot(owner.id, 2)
    result = await get_triggers(bot.id, other.id)
    assert result is None


async def test_set_trigger(fresh_db):
    from db.repository import set_trigger, get_triggers
    client = await _make_client(110004)
    bot = await _make_bot(client.id, 3)
    ok = await set_trigger(bot.id, client.id, "цена", "Наш прайс: 1000 руб.")
    assert ok is True
    triggers = await get_triggers(bot.id, client.id)
    assert triggers["цена"] == "Наш прайс: 1000 руб."


async def test_set_trigger_updates_existing(fresh_db):
    from db.repository import set_trigger, get_triggers
    client = await _make_client(110005)
    bot = await _make_bot(client.id, 4)
    await set_trigger(bot.id, client.id, "адрес", "ул. Старая, 1")
    await set_trigger(bot.id, client.id, "адрес", "ул. Новая, 2")
    triggers = await get_triggers(bot.id, client.id)
    assert triggers["адрес"] == "ул. Новая, 2"
    assert len(triggers) == 1


async def test_remove_trigger(fresh_db):
    from db.repository import set_trigger, remove_trigger, get_triggers
    client = await _make_client(110006)
    bot = await _make_bot(client.id, 5)
    await set_trigger(bot.id, client.id, "цена", "1000 руб.")
    removed = await remove_trigger(bot.id, client.id, "цена")
    assert removed is True
    triggers = await get_triggers(bot.id, client.id)
    assert "цена" not in triggers


async def test_remove_nonexistent_returns_false(fresh_db):
    from db.repository import remove_trigger
    client = await _make_client(110007)
    bot = await _make_bot(client.id, 6)
    removed = await remove_trigger(bot.id, client.id, "несуществующий")
    assert removed is False


async def test_multiple_triggers(fresh_db):
    from db.repository import set_trigger, get_triggers
    client = await _make_client(110008)
    bot = await _make_bot(client.id, 7)
    await set_trigger(bot.id, client.id, "цена", "1000 руб.")
    await set_trigger(bot.id, client.id, "адрес", "ул. Ленина, 1")
    await set_trigger(bot.id, client.id, "режим", "Пн-Пт 9:00-18:00")
    triggers = await get_triggers(bot.id, client.id)
    assert len(triggers) == 3


async def test_set_trigger_wrong_owner(fresh_db):
    from db.repository import set_trigger
    owner = await _make_client(110009)
    other = await _make_client(110010)
    bot = await _make_bot(owner.id, 8)
    ok = await set_trigger(bot.id, other.id, "цена", "hack")
    assert ok is False


async def test_triggers_bots_isolated(fresh_db):
    from db.repository import set_trigger, get_triggers
    client = await _make_client(110011)
    bot_a = await _make_bot(client.id, 9)
    bot_b = await _make_bot(client.id, 10)
    await set_trigger(bot_a.id, client.id, "цена", "1000")
    trig_a = await get_triggers(bot_a.id, client.id)
    trig_b = await get_triggers(bot_b.id, client.id)
    assert "цена" in trig_a
    assert "цена" not in trig_b
