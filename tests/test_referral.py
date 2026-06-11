"""Tests for the Wave 4 referral program."""
from datetime import datetime, timedelta, timezone


async def _make_client(telegram_id: int = 88000):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def test_get_or_create_referral_code_creates_code(fresh_db):
    from db.repository import get_or_create_referral_code
    client = await _make_client(88001)
    code = await get_or_create_referral_code(client.id)
    assert isinstance(code, str)
    assert len(code) == 10


async def test_get_or_create_referral_code_idempotent(fresh_db):
    from db.repository import get_or_create_referral_code
    client = await _make_client(88002)
    code1 = await get_or_create_referral_code(client.id)
    code2 = await get_or_create_referral_code(client.id)
    assert code1 == code2


async def test_find_client_by_referral_code(fresh_db):
    from db.repository import find_client_by_referral_code, get_or_create_referral_code
    client = await _make_client(88003)
    code = await get_or_create_referral_code(client.id)
    found = await find_client_by_referral_code(code)
    assert found is not None
    assert found.id == client.id


async def test_find_client_by_referral_code_unknown(fresh_db):
    from db.repository import find_client_by_referral_code
    result = await find_client_by_referral_code("doesnotexist")
    assert result is None


async def test_set_referred_by_links_clients(fresh_db):
    from db.repository import set_referred_by
    referrer = await _make_client(88010)
    referee = await _make_client(88011)
    result = await set_referred_by(referee.id, referrer.id)
    assert result is True


async def test_set_referred_by_idempotent(fresh_db):
    from db.repository import set_referred_by
    referrer = await _make_client(88012)
    referee = await _make_client(88013)
    await set_referred_by(referee.id, referrer.id)
    result = await set_referred_by(referee.id, referrer.id)
    assert result is False


async def test_set_referred_by_prevents_self_referral(fresh_db):
    from db.repository import set_referred_by
    client = await _make_client(88014)
    result = await set_referred_by(client.id, client.id)
    assert result is False


async def test_get_referral_stats_empty(fresh_db):
    from db.repository import get_referral_stats
    client = await _make_client(88020)
    stats = await get_referral_stats(client.id)
    assert stats == {"total_referrals": 0, "rewards_earned": 0, "pending_rewards": 0}


async def test_get_referral_stats_counts_referrals(fresh_db):
    from db.repository import get_referral_stats, set_referred_by
    referrer = await _make_client(88021)
    for i in range(3):
        ref = await _make_client(88022 + i)
        await set_referred_by(ref.id, referrer.id)
    stats = await get_referral_stats(referrer.id)
    assert stats["total_referrals"] == 3
    assert stats["rewards_earned"] == 0
    assert stats["pending_rewards"] == 3


async def test_apply_pending_referral_reward_no_referrer(fresh_db):
    from db.repository import apply_pending_referral_reward
    client = await _make_client(88030)
    result = await apply_pending_referral_reward(client.id)
    assert result is None


async def test_apply_pending_referral_reward_creates_sub_for_referrer(fresh_db):
    from sqlalchemy import select
    from db.repository import apply_pending_referral_reward, set_referred_by
    from db.database import get_session
    from db.models import Subscription

    referrer = await _make_client(88031)
    referee = await _make_client(88032)
    await set_referred_by(referee.id, referrer.id)

    result = await apply_pending_referral_reward(referee.id)
    assert result is not None  # returns referrer's telegram_id

    async with get_session() as session:
        sub = await session.scalar(
            select(Subscription).where(
                Subscription.client_id == referrer.id,
                Subscription.plan == "referral_reward",
            )
        )
    assert sub is not None
    assert sub.tier == "pro"
    assert sub.status == "active"


async def test_apply_pending_referral_reward_extends_existing_sub(fresh_db):
    from db.repository import (
        apply_pending_referral_reward,
        create_subscription,
        get_active_subscription,
        set_referred_by,
    )
    from config import REFERRAL_REWARD_DAYS

    now = datetime.now(timezone.utc)
    referrer = await _make_client(88033)
    referee = await _make_client(88034)

    original_expires = now + timedelta(days=10)
    await create_subscription(
        client_id=referrer.id,
        payment_id="pay_extend_test",
        plan="monthly",
        status="active",
        tier="pro",
        started_at=now,
        expires_at=original_expires,
        tokens_reset_at=original_expires,
    )

    await set_referred_by(referee.id, referrer.id)
    result = await apply_pending_referral_reward(referee.id)
    assert result is not None  # returns referrer's telegram_id

    sub = await get_active_subscription(referrer.id)
    assert sub is not None
    expected_expires = original_expires + timedelta(days=REFERRAL_REWARD_DAYS)
    # SQLite returns naive datetimes; normalize before comparison
    actual = sub.expires_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    assert abs((actual - expected_expires).total_seconds()) < 2


async def test_apply_pending_referral_reward_idempotent(fresh_db):
    from db.repository import apply_pending_referral_reward, set_referred_by

    referrer = await _make_client(88035)
    referee = await _make_client(88036)
    await set_referred_by(referee.id, referrer.id)

    result1 = await apply_pending_referral_reward(referee.id)
    result2 = await apply_pending_referral_reward(referee.id)
    assert result1 is not None  # returns referrer's telegram_id on first apply
    assert result2 is None      # already rewarded
