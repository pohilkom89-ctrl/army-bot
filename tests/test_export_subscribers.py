"""Tests for Wave 10 — CSV export of subscribers."""
from datetime import datetime, timedelta, timezone


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"ExportBot{suffix}",
        bot_type="support",
        bot_token=f"666{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def _add_subscriber(bot_id: int, telegram_id: int, joined_at: datetime):
    from db.database import get_session
    from db.models import BotSubscriber
    async with get_session() as session:
        session.add(BotSubscriber(bot_id=bot_id, telegram_id=telegram_id, joined_at=joined_at))


async def test_export_empty(fresh_db):
    from db.repository import get_subscribers_for_export
    client = await _make_client(88001)
    bot = await _make_bot(client.id, 1)
    rows = await get_subscribers_for_export(bot.id, client.id)
    assert rows == []


async def test_export_wrong_owner_returns_none(fresh_db):
    from db.repository import get_subscribers_for_export
    owner = await _make_client(88002)
    other = await _make_client(88003)
    bot = await _make_bot(owner.id, 2)
    result = await get_subscribers_for_export(bot.id, other.id)
    assert result is None


async def test_export_returns_all_subscribers(fresh_db):
    from db.repository import get_subscribers_for_export
    now = datetime.now(timezone.utc)
    client = await _make_client(88004)
    bot = await _make_bot(client.id, 3)
    await _add_subscriber(bot.id, 1001, now - timedelta(days=5))
    await _add_subscriber(bot.id, 1002, now - timedelta(days=3))
    await _add_subscriber(bot.id, 1003, now - timedelta(days=1))
    rows = await get_subscribers_for_export(bot.id, client.id)
    assert len(rows) == 3
    telegram_ids = [r["telegram_id"] for r in rows]
    assert sorted(telegram_ids) == [1001, 1002, 1003]


async def test_export_ordered_by_joined_at(fresh_db):
    from db.repository import get_subscribers_for_export
    now = datetime.now(timezone.utc)
    client = await _make_client(88005)
    bot = await _make_bot(client.id, 4)
    await _add_subscriber(bot.id, 2002, now - timedelta(days=1))
    await _add_subscriber(bot.id, 2001, now - timedelta(days=3))
    rows = await get_subscribers_for_export(bot.id, client.id)
    assert [r["telegram_id"] for r in rows] == [2001, 2002]


async def test_export_has_joined_at_field(fresh_db):
    from db.repository import get_subscribers_for_export
    now = datetime.now(timezone.utc)
    client = await _make_client(88006)
    bot = await _make_bot(client.id, 5)
    await _add_subscriber(bot.id, 3001, now)
    rows = await get_subscribers_for_export(bot.id, client.id)
    assert len(rows) == 1
    assert "joined_at" in rows[0]
    assert rows[0]["joined_at"] is not None


async def test_export_bots_isolated(fresh_db):
    from db.repository import get_subscribers_for_export
    now = datetime.now(timezone.utc)
    client = await _make_client(88007)
    bot_a = await _make_bot(client.id, 6)
    bot_b = await _make_bot(client.id, 7)
    await _add_subscriber(bot_a.id, 4001, now)
    await _add_subscriber(bot_a.id, 4002, now)
    await _add_subscriber(bot_b.id, 4003, now)
    rows_a = await get_subscribers_for_export(bot_a.id, client.id)
    rows_b = await get_subscribers_for_export(bot_b.id, client.id)
    assert len(rows_a) == 2
    assert len(rows_b) == 1
