"""Tests for Wave 31: get_deep_analytics."""
from datetime import datetime, timedelta, timezone


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"DeepBot{suffix}",
        bot_type="support",
        bot_token=f"888{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def _add_message(bot_id: int, telegram_id: int, role: str, text: str, ts: datetime):
    from db.database import get_session
    from db.models import BotMessage
    async with get_session() as session:
        session.add(BotMessage(
            bot_id=bot_id,
            telegram_id=telegram_id,
            role=role,
            text=text,
            created_at=ts,
        ))


async def test_deep_analytics_empty(fresh_db):
    """No activity → zeroes, empty top_questions."""
    from db.repository import get_deep_analytics
    client = await _make_client(90001)
    bot = await _make_bot(client.id, 1)
    result = await get_deep_analytics(bot.id, client.id)
    assert result == {
        "top_questions": [],
        "unique_today": 0,
        "unique_7d": 0,
        "unique_30d": 0,
        "avg_per_user": 0.0,
        "peak_hour": None,
    }


async def test_deep_analytics_wrong_owner_returns_none(fresh_db):
    from db.repository import get_deep_analytics
    owner = await _make_client(90002)
    other = await _make_client(90003)
    bot = await _make_bot(owner.id, 2)
    assert await get_deep_analytics(bot.id, other.id) is None


async def test_deep_analytics_unique_today(fresh_db):
    """unique_today counts only messages from today UTC midnight onward."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    client = await _make_client(90004)
    bot = await _make_bot(client.id, 3)
    # two users today
    await _add_message(bot.id, 1001, "user", "hi", now)
    await _add_message(bot.id, 1002, "user", "hello", now)
    # one user yesterday → not in today, but in 7d
    await _add_message(bot.id, 1003, "user", "hey", today_start - timedelta(hours=1))
    result = await get_deep_analytics(bot.id, client.id)
    assert result["unique_today"] == 2
    assert result["unique_7d"] == 3


async def test_deep_analytics_unique_periods(fresh_db):
    """7d and 30d thresholds are respected."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90005)
    bot = await _make_bot(client.id, 4)
    await _add_message(bot.id, 2001, "user", "a", now - timedelta(days=1))   # today-ish → 7d, 30d
    await _add_message(bot.id, 2002, "user", "b", now - timedelta(days=8))   # 30d only
    await _add_message(bot.id, 2003, "user", "c", now - timedelta(days=35))  # outside both
    result = await get_deep_analytics(bot.id, client.id)
    assert result["unique_7d"] == 1
    assert result["unique_30d"] == 2


async def test_deep_analytics_avg_per_user(fresh_db):
    """avg_per_user = total user messages / unique users."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90006)
    bot = await _make_bot(client.id, 5)
    # user 3001 sends 3 messages, user 3002 sends 1 → avg = 2.0
    for _ in range(3):
        await _add_message(bot.id, 3001, "user", "q", now)
    await _add_message(bot.id, 3002, "user", "q", now)
    # bot messages don't count
    await _add_message(bot.id, 3001, "bot", "answer", now)
    result = await get_deep_analytics(bot.id, client.id)
    assert result["avg_per_user"] == 2.0


async def test_deep_analytics_top_questions(fresh_db):
    """top_questions returns most-frequent texts descending, long texts excluded."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90007)
    bot = await _make_bot(client.id, 6)
    # "price?" asked 3 times, "help" asked 2 times, "ok" asked 1 time
    for _ in range(3):
        await _add_message(bot.id, 4001, "user", "price?", now)
    for _ in range(2):
        await _add_message(bot.id, 4002, "user", "help", now)
    await _add_message(bot.id, 4003, "user", "ok", now)
    # very long message (>200 chars) should be excluded
    long_text = "x" * 201
    for _ in range(10):
        await _add_message(bot.id, 4004, "user", long_text, now)
    result = await get_deep_analytics(bot.id, client.id)
    questions = result["top_questions"]
    assert questions[0] == ("price?", 3)
    assert questions[1] == ("help", 2)
    assert questions[2] == ("ok", 1)
    # long text not in top questions
    assert all(q != "x" * 201 for q, _ in questions)


async def test_deep_analytics_top_questions_case_insensitive(fresh_db):
    """top_questions groups by lowercase."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90008)
    bot = await _make_bot(client.id, 7)
    await _add_message(bot.id, 5001, "user", "Hello", now)
    await _add_message(bot.id, 5002, "user", "hello", now)
    await _add_message(bot.id, 5003, "user", "HELLO", now)
    result = await get_deep_analytics(bot.id, client.id)
    assert len(result["top_questions"]) == 1
    assert result["top_questions"][0] == ("hello", 3)


async def test_deep_analytics_peak_hour(fresh_db):
    """peak_hour is the hour (UTC) with most user messages."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90009)
    bot = await _make_bot(client.id, 8)
    # Insert 3 messages at hour 14 UTC and 1 at hour 9
    base_14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
    base_09 = now.replace(hour=9, minute=0, second=0, microsecond=0)
    for i in range(3):
        await _add_message(bot.id, 6001 + i, "user", "q", base_14)
    await _add_message(bot.id, 6010, "user", "q", base_09)
    result = await get_deep_analytics(bot.id, client.id)
    assert result["peak_hour"] == 14


async def test_deep_analytics_isolated_bots(fresh_db):
    """Deep analytics for one bot is not polluted by another bot's data."""
    from db.repository import get_deep_analytics
    now = datetime.now(timezone.utc)
    client = await _make_client(90010)
    bot_a = await _make_bot(client.id, 9)
    bot_b = await _make_bot(client.id, 10)
    await _add_message(bot_a.id, 7001, "user", "test", now)
    await _add_message(bot_b.id, 7002, "user", "test", now)
    await _add_message(bot_b.id, 7003, "user", "test", now)
    result_a = await get_deep_analytics(bot_a.id, client.id)
    result_b = await get_deep_analytics(bot_b.id, client.id)
    assert result_a["unique_7d"] == 1
    assert result_b["unique_7d"] == 2
