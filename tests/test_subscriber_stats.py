"""Tests for Wave 7 subscriber stats."""
from datetime import datetime, timedelta, timezone


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"StatsBot{suffix}",
        bot_type="support",
        bot_token=f"333{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def _add_subscriber(bot_id: int, telegram_id: int, joined_at: datetime):
    from db.database import get_session
    from db.models import BotSubscriber
    async with get_session() as session:
        row = BotSubscriber(bot_id=bot_id, telegram_id=telegram_id, joined_at=joined_at)
        session.add(row)


async def test_subscriber_stats_empty(fresh_db):
    from db.repository import get_subscriber_stats
    client = await _make_client(55001)
    bot = await _make_bot(client.id, 1)
    stats = await get_subscriber_stats(bot.id, client.id)
    assert stats == {"total": 0, "new_today": 0, "new_7d": 0, "new_30d": 0}


async def test_subscriber_stats_counts_correctly(fresh_db):
    from db.repository import get_subscriber_stats
    now = datetime.now(timezone.utc)
    client = await _make_client(55002)
    bot = await _make_bot(client.id, 2)

    await _add_subscriber(bot.id, 1001, now - timedelta(minutes=30))   # today, 7d, 30d
    await _add_subscriber(bot.id, 1002, now - timedelta(days=3))        # 7d, 30d
    await _add_subscriber(bot.id, 1003, now - timedelta(days=15))       # 30d only
    await _add_subscriber(bot.id, 1004, now - timedelta(days=45))       # none

    stats = await get_subscriber_stats(bot.id, client.id)
    assert stats["total"] == 4
    assert stats["new_today"] == 1
    assert stats["new_7d"] == 2
    assert stats["new_30d"] == 3


async def test_subscriber_stats_wrong_owner_returns_none(fresh_db):
    from db.repository import get_subscriber_stats
    owner = await _make_client(55003)
    other = await _make_client(55004)
    bot = await _make_bot(owner.id, 3)
    result = await get_subscriber_stats(bot.id, other.id)
    assert result is None


async def test_subscriber_stats_multiple_bots_isolated(fresh_db):
    from db.repository import get_subscriber_stats
    now = datetime.now(timezone.utc)
    client = await _make_client(55005)
    bot_a = await _make_bot(client.id, 4)
    bot_b = await _make_bot(client.id, 5)

    await _add_subscriber(bot_a.id, 2001, now - timedelta(hours=1))
    await _add_subscriber(bot_a.id, 2002, now - timedelta(hours=2))
    await _add_subscriber(bot_b.id, 3001, now - timedelta(hours=1))

    stats_a = await get_subscriber_stats(bot_a.id, client.id)
    stats_b = await get_subscriber_stats(bot_b.id, client.id)
    assert stats_a["total"] == 2
    assert stats_b["total"] == 1
