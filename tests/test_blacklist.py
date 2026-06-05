"""Tests for Wave 8 blacklist management."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"BlacklistBot{suffix}",
        bot_type="support",
        bot_token=f"444{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_blacklist_empty_by_default(fresh_db):
    from db.repository import get_blacklist
    client = await _make_client(66001)
    bot = await _make_bot(client.id, 1)
    bl = await get_blacklist(bot.id, client.id)
    assert bl == []


async def test_blacklist_wrong_owner_returns_none(fresh_db):
    from db.repository import get_blacklist
    owner = await _make_client(66002)
    other = await _make_client(66003)
    bot = await _make_bot(owner.id, 2)
    result = await get_blacklist(bot.id, other.id)
    assert result is None


async def test_add_to_blacklist(fresh_db):
    from db.repository import add_to_blacklist, get_blacklist
    client = await _make_client(66004)
    bot = await _make_bot(client.id, 3)
    added = await add_to_blacklist(bot.id, client.id, 9001)
    assert added is True
    bl = await get_blacklist(bot.id, client.id)
    assert 9001 in bl


async def test_add_duplicate_returns_false(fresh_db):
    from db.repository import add_to_blacklist
    client = await _make_client(66005)
    bot = await _make_bot(client.id, 4)
    await add_to_blacklist(bot.id, client.id, 9002)
    added_again = await add_to_blacklist(bot.id, client.id, 9002)
    assert added_again is False


async def test_remove_from_blacklist(fresh_db):
    from db.repository import add_to_blacklist, remove_from_blacklist, get_blacklist
    client = await _make_client(66006)
    bot = await _make_bot(client.id, 5)
    await add_to_blacklist(bot.id, client.id, 9003)
    removed = await remove_from_blacklist(bot.id, client.id, 9003)
    assert removed is True
    bl = await get_blacklist(bot.id, client.id)
    assert 9003 not in bl


async def test_remove_nonexistent_returns_false(fresh_db):
    from db.repository import remove_from_blacklist
    client = await _make_client(66007)
    bot = await _make_bot(client.id, 6)
    removed = await remove_from_blacklist(bot.id, client.id, 9999)
    assert removed is False


async def test_multiple_ids_in_blacklist(fresh_db):
    from db.repository import add_to_blacklist, get_blacklist
    client = await _make_client(66008)
    bot = await _make_bot(client.id, 7)
    for tid in [1001, 1002, 1003]:
        await add_to_blacklist(bot.id, client.id, tid)
    bl = await get_blacklist(bot.id, client.id)
    assert sorted(bl) == [1001, 1002, 1003]


async def test_bots_isolated(fresh_db):
    from db.repository import add_to_blacklist, get_blacklist
    client = await _make_client(66009)
    bot_a = await _make_bot(client.id, 8)
    bot_b = await _make_bot(client.id, 9)
    await add_to_blacklist(bot_a.id, client.id, 5001)
    bl_a = await get_blacklist(bot_a.id, client.id)
    bl_b = await get_blacklist(bot_b.id, client.id)
    assert 5001 in bl_a
    assert 5001 not in bl_b


async def test_add_wrong_owner_returns_false(fresh_db):
    from db.repository import add_to_blacklist
    owner = await _make_client(66010)
    other = await _make_client(66011)
    bot = await _make_bot(owner.id, 10)
    added = await add_to_blacklist(bot.id, other.id, 7777)
    assert added is False


async def test_remove_wrong_owner_returns_false(fresh_db):
    from db.repository import add_to_blacklist, remove_from_blacklist
    owner = await _make_client(66012)
    other = await _make_client(66013)
    bot = await _make_bot(owner.id, 11)
    await add_to_blacklist(bot.id, owner.id, 8888)
    removed = await remove_from_blacklist(bot.id, other.id, 8888)
    assert removed is False
