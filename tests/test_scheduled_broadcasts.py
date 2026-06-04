"""Tests for Wave 5 scheduled broadcasts."""
from datetime import datetime, timedelta, timezone


def _utcnow():
    return datetime.now(timezone.utc)


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, bot_id_hint: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"TestBot{bot_id_hint}",
        bot_type="support",
        bot_token=f"111{bot_id_hint:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


# ---------------------------------------------------------------------------
# _parse_schedule_time (unit tests, no DB)
# ---------------------------------------------------------------------------

def test_parse_schedule_time_full_date():
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from main import _parse_schedule_time
    dt = _parse_schedule_time("25.06.2026 18:30")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.month == 6
    assert dt.day == 25
    assert dt.hour == 15  # 18:30 Msk = 15:30 UTC


def test_parse_schedule_time_short_date():
    from main import _parse_schedule_time
    # Use a date far in the future to avoid year rollover ambiguity
    dt = _parse_schedule_time("25.12 09:00")
    assert dt is not None
    assert dt.month == 12
    assert dt.day == 25
    assert dt.hour == 6  # 09:00 Msk = 06:00 UTC


def test_parse_schedule_time_invalid():
    from main import _parse_schedule_time
    assert _parse_schedule_time("garbage") is None
    assert _parse_schedule_time("32.13 99:99") is None
    assert _parse_schedule_time("") is None


# ---------------------------------------------------------------------------
# Repository functions (need DB)
# ---------------------------------------------------------------------------

async def test_create_scheduled_broadcast(fresh_db):
    from db.repository import create_scheduled_broadcast
    client = await _make_client(77001)
    bot = await _make_bot(client.id, 1)
    send_at = _utcnow() + timedelta(hours=1)
    row = await create_scheduled_broadcast(bot.id, client.id, "Hello subscribers!", send_at)
    assert row.id is not None
    assert row.status == "pending"
    assert row.message_text == "Hello subscribers!"


async def test_get_pending_broadcasts_returns_due(fresh_db):
    from db.repository import create_scheduled_broadcast, get_pending_broadcasts
    client = await _make_client(77002)
    bot = await _make_bot(client.id, 2)
    past = _utcnow() - timedelta(minutes=5)
    future = _utcnow() + timedelta(hours=2)
    await create_scheduled_broadcast(bot.id, client.id, "Past", past)
    await create_scheduled_broadcast(bot.id, client.id, "Future", future)
    pending = await get_pending_broadcasts(before=_utcnow())
    texts = [b.message_text for b in pending]
    assert "Past" in texts
    assert "Future" not in texts


async def test_mark_broadcast_sent(fresh_db):
    from db.repository import create_scheduled_broadcast, get_pending_broadcasts, mark_broadcast_sent
    client = await _make_client(77003)
    bot = await _make_bot(client.id, 3)
    past = _utcnow() - timedelta(minutes=1)
    row = await create_scheduled_broadcast(bot.id, client.id, "msg", past)
    await mark_broadcast_sent(row.id, sent_count=10, failed_count=2)
    # Should no longer appear in pending
    pending = await get_pending_broadcasts(before=_utcnow())
    ids = [b.id for b in pending]
    assert row.id not in ids


async def test_get_bot_scheduled_broadcasts(fresh_db):
    from db.repository import create_scheduled_broadcast, get_bot_scheduled_broadcasts
    client = await _make_client(77004)
    bot = await _make_bot(client.id, 4)
    for i in range(3):
        await create_scheduled_broadcast(bot.id, client.id, f"msg{i}", _utcnow() + timedelta(hours=i + 1))
    rows = await get_bot_scheduled_broadcasts(bot.id, client.id)
    assert len(rows) == 3
    # Should be sorted by send_at ascending
    times = [r.send_at for r in rows]
    assert times == sorted(times)


async def test_get_bot_scheduled_broadcasts_requires_owner(fresh_db):
    from db.repository import create_scheduled_broadcast, get_bot_scheduled_broadcasts
    owner = await _make_client(77005)
    other = await _make_client(77006)
    bot = await _make_bot(owner.id, 5)
    await create_scheduled_broadcast(bot.id, owner.id, "msg", _utcnow() + timedelta(hours=1))
    rows = await get_bot_scheduled_broadcasts(bot.id, other.id)
    assert rows == []


async def test_cancel_scheduled_broadcast(fresh_db):
    from db.repository import cancel_scheduled_broadcast, create_scheduled_broadcast, get_bot_scheduled_broadcasts
    client = await _make_client(77007)
    bot = await _make_bot(client.id, 7)
    row = await create_scheduled_broadcast(bot.id, client.id, "delete me", _utcnow() + timedelta(hours=1))
    deleted = await cancel_scheduled_broadcast(row.id, client.id)
    assert deleted is True
    remaining = await get_bot_scheduled_broadcasts(bot.id, client.id)
    assert all(r.id != row.id for r in remaining)


async def test_cancel_scheduled_broadcast_wrong_owner(fresh_db):
    from db.repository import cancel_scheduled_broadcast, create_scheduled_broadcast
    owner = await _make_client(77008)
    other = await _make_client(77009)
    bot = await _make_bot(owner.id, 8)
    row = await create_scheduled_broadcast(bot.id, owner.id, "msg", _utcnow() + timedelta(hours=1))
    deleted = await cancel_scheduled_broadcast(row.id, other.id)
    assert deleted is False
