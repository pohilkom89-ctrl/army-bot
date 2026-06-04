"""Tests for the 7-day free trial activation logic."""
from datetime import timezone


async def _make_client(telegram_id: int = 999):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def test_trial_activates_for_new_user(fresh_db):
    """New user with no subscription gets a 7-day Pro trial."""
    from db.repository import activate_trial, get_active_subscription
    from config import TRIAL_DAYS, PLANS

    client = await _make_client()
    activated = await activate_trial(client.id)
    assert activated is True

    sub = await get_active_subscription(client.id)
    assert sub is not None
    assert sub.plan == "trial"
    assert sub.tier == "pro"
    assert sub.status == "active"
    assert sub.tokens_limit == PLANS["pro"]["tokens_limit"]

    duration = (sub.expires_at - sub.started_at).days
    assert duration == TRIAL_DAYS


async def test_trial_idempotent(fresh_db):
    """Calling activate_trial twice returns False the second time."""
    from db.repository import activate_trial

    client = await _make_client(telegram_id=1001)
    assert await activate_trial(client.id) is True
    assert await activate_trial(client.id) is False


async def test_trial_skips_if_active_subscription(fresh_db):
    """User with an active paid subscription does not get a trial."""
    from datetime import datetime, timedelta
    from db.repository import activate_trial, create_subscription

    client = await _make_client(telegram_id=1002)
    now = datetime.now(timezone.utc)
    await create_subscription(
        client_id=client.id,
        payment_id="pay_abc",
        plan="monthly",
        status="active",
        tier="starter",
        started_at=now,
        expires_at=now + timedelta(days=30),
        tokens_reset_at=now + timedelta(days=30),
    )
    activated = await activate_trial(client.id)
    assert activated is False


async def test_trial_plan_visible_in_usage_stats(fresh_db):
    """get_usage_stats returns plan='trial' so the /usage badge renders correctly."""
    from db.repository import activate_trial, get_usage_stats

    client = await _make_client(telegram_id=1003)
    await activate_trial(client.id)

    stats = await get_usage_stats(client.id)
    assert stats["plan"] == "trial"
    assert stats["tier"] == "pro"
