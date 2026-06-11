"""Tests for Wave 26: get_engagement_funnel."""
from datetime import datetime, timedelta, timezone


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"FunnelBot{suffix}",
        bot_type="support",
        bot_token=f"777{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def _add_message(bot_id: int, telegram_id: int, role: str, ts: datetime):
    from db.database import get_session
    from db.models import BotMessage
    async with get_session() as session:
        session.add(BotMessage(
            bot_id=bot_id,
            telegram_id=telegram_id,
            role=role,
            text="hello",
            created_at=ts,
        ))


async def _add_subscriber(bot_id: int, telegram_id: int):
    from db.database import get_session
    from db.models import BotSubscriber
    async with get_session() as session:
        session.add(BotSubscriber(
            bot_id=bot_id,
            telegram_id=telegram_id,
            joined_at=datetime.now(timezone.utc),
        ))


async def test_funnel_empty(fresh_db):
    """No activity → all zeroes."""
    from db.repository import get_engagement_funnel
    client = await _make_client(80001)
    bot = await _make_bot(client.id, 1)
    result = await get_engagement_funnel(bot.id, client.id)
    assert result == {"subscribers": 0, "messaged": 0, "returned": 0, "active_7d": 0}


async def test_funnel_wrong_owner_returns_none(fresh_db):
    from db.repository import get_engagement_funnel
    owner = await _make_client(80002)
    other = await _make_client(80003)
    bot = await _make_bot(owner.id, 2)
    result = await get_engagement_funnel(bot.id, other.id)
    assert result is None


async def test_funnel_subscribers_counted(fresh_db):
    """Subscribers from BotSubscriber table."""
    from db.repository import get_engagement_funnel
    client = await _make_client(80004)
    bot = await _make_bot(client.id, 3)
    await _add_subscriber(bot.id, 1001)
    await _add_subscriber(bot.id, 1002)
    result = await get_engagement_funnel(bot.id, client.id)
    assert result["subscribers"] == 2
    assert result["messaged"] == 0


async def test_funnel_messaged(fresh_db):
    """Users who sent at least one message are counted in messaged."""
    from db.repository import get_engagement_funnel
    now = datetime.now(timezone.utc)
    client = await _make_client(80005)
    bot = await _make_bot(client.id, 4)
    await _add_subscriber(bot.id, 2001)
    await _add_subscriber(bot.id, 2002)
    await _add_subscriber(bot.id, 2003)
    # Only 2001 and 2002 wrote; bot replies don't count
    await _add_message(bot.id, 2001, "user", now)
    await _add_message(bot.id, 2001, "bot", now)
    await _add_message(bot.id, 2002, "user", now)
    result = await get_engagement_funnel(bot.id, client.id)
    assert result["subscribers"] == 3
    assert result["messaged"] == 2


async def test_funnel_returned(fresh_db):
    """returned = users who messaged on 2+ distinct calendar days."""
    from db.repository import get_engagement_funnel
    now = datetime.now(timezone.utc)
    client = await _make_client(80006)
    bot = await _make_bot(client.id, 5)
    # user 3001: messaged on 2 days → returned
    await _add_message(bot.id, 3001, "user", now - timedelta(days=3))
    await _add_message(bot.id, 3001, "user", now - timedelta(days=1))
    # user 3002: messaged on 1 day only → not returned
    await _add_message(bot.id, 3002, "user", now - timedelta(hours=2))
    await _add_message(bot.id, 3002, "user", now - timedelta(hours=1))
    result = await get_engagement_funnel(bot.id, client.id)
    assert result["messaged"] == 2
    assert result["returned"] == 1


async def test_funnel_active_7d(fresh_db):
    """active_7d = distinct users who messaged within last 7 days."""
    from db.repository import get_engagement_funnel
    now = datetime.now(timezone.utc)
    client = await _make_client(80007)
    bot = await _make_bot(client.id, 6)
    await _add_message(bot.id, 4001, "user", now - timedelta(days=2))   # within 7d
    await _add_message(bot.id, 4002, "user", now - timedelta(days=5))   # within 7d
    await _add_message(bot.id, 4003, "user", now - timedelta(days=10))  # outside 7d
    result = await get_engagement_funnel(bot.id, client.id)
    assert result["messaged"] == 3
    assert result["active_7d"] == 2


async def test_funnel_isolated_bots(fresh_db):
    """Funnel for one bot is not polluted by another bot's data."""
    from db.repository import get_engagement_funnel
    now = datetime.now(timezone.utc)
    client = await _make_client(80008)
    bot_a = await _make_bot(client.id, 7)
    bot_b = await _make_bot(client.id, 8)
    await _add_subscriber(bot_a.id, 5001)
    await _add_message(bot_a.id, 5001, "user", now)
    await _add_subscriber(bot_b.id, 5002)
    await _add_subscriber(bot_b.id, 5003)
    result_a = await get_engagement_funnel(bot_a.id, client.id)
    result_b = await get_engagement_funnel(bot_b.id, client.id)
    assert result_a["subscribers"] == 1
    assert result_a["messaged"] == 1
    assert result_b["subscribers"] == 2
    assert result_b["messaged"] == 0
