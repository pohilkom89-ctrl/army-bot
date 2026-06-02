"""Tests for get_bot_analytics repository function."""
from datetime import datetime, timezone


async def _make_bot(client_id: int = 1, token: str = "tok_a"):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id, bot_name="TestBot", bot_type="faq",
        bot_token=token, system_prompt="p", config={},
    )


async def _add_messages(bot_id: int, client_ids: list[int], role: str = "user"):
    from db.database import get_session
    from db.models import ChatHistory
    async with get_session() as session:
        for cid in client_ids:
            session.add(ChatHistory(
                client_id=cid, bot_id=bot_id, role=role,
                content="hello", tokens_used=0,
                created_at=datetime.now(timezone.utc),
            ))


async def test_analytics_empty(fresh_db):
    """Bot with no messages → all counters zero, no peak hour."""
    from db.repository import get_bot_analytics
    bot = await _make_bot()
    result = await get_bot_analytics(bot.id, client_id=1)
    assert result is not None
    assert result["unique_users"] == 0
    assert result["total_messages"] == 0
    assert result["messages_7d"] == 0
    assert result["messages_30d"] == 0
    assert result["peak_hour"] is None
    assert result["avg_messages_per_user"] == 0.0


async def test_analytics_counts(fresh_db):
    """3 messages from 2 distinct users → correct aggregates."""
    from db.repository import get_bot_analytics
    bot = await _make_bot(token="tok_b")
    # user 10 sends 2 msgs, user 20 sends 1 msg
    await _add_messages(bot.id, [10, 10, 20])
    result = await get_bot_analytics(bot.id, client_id=1)
    assert result["unique_users"] == 2
    assert result["total_messages"] == 3
    assert result["avg_messages_per_user"] == 1.5


async def test_analytics_assistant_not_counted(fresh_db):
    """Assistant messages are not counted as user messages."""
    from db.repository import get_bot_analytics
    bot = await _make_bot(token="tok_c")
    await _add_messages(bot.id, [10], role="user")
    await _add_messages(bot.id, [10, 10, 10], role="assistant")
    result = await get_bot_analytics(bot.id, client_id=1)
    assert result["total_messages"] == 1
    assert result["unique_users"] == 1


async def test_analytics_wrong_owner(fresh_db):
    """Returns None for a bot not owned by the requesting client."""
    from db.repository import get_bot_analytics
    bot = await _make_bot(client_id=1, token="tok_d")
    result = await get_bot_analytics(bot.id, client_id=99)
    assert result is None


async def test_analytics_nonexistent_bot(fresh_db):
    """Returns None for a bot_id that doesn't exist."""
    from db.repository import get_bot_analytics
    result = await get_bot_analytics(bot_id=99999, client_id=1)
    assert result is None


async def test_analytics_peak_hour(fresh_db):
    """peak_hour is the hour bucket with the most user messages."""
    from db.database import get_session
    from db.models import ChatHistory
    from db.repository import get_bot_analytics
    bot = await _make_bot(token="tok_e")
    async with get_session() as session:
        for h, count in [(9, 5), (14, 3), (22, 1)]:
            for _ in range(count):
                session.add(ChatHistory(
                    client_id=1, bot_id=bot.id, role="user",
                    content="x", tokens_used=0,
                    created_at=datetime(2026, 1, 1, h, 0, tzinfo=timezone.utc),
                ))
    result = await get_bot_analytics(bot.id, client_id=1)
    assert result["peak_hour"] == 9
