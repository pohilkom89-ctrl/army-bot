"""Tests for tech debt 7 (bots_limit enforcement). Mocks the two DB
helpers _check_bots_limit relies on so we don't need a real database.
Admin bypass is exercised through the real config.is_admin path —
ADMIN_TELEGRAM_IDS is set in conftest.py to '11111,22222'."""


async def test_bots_limit_blocks_starter_at_limit(mocker):
    """1 bot already exists, starter cap is 1 → blocked."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_client_bots", return_value=1)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is False
    assert plan == "Старт"
    assert count == 1
    assert limit == 1


async def test_bots_limit_allows_starter_below_limit(mocker):
    """0 bots, starter cap is 1 → allowed."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_client_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is True
    assert plan == "Старт"
    assert count == 0
    assert limit == 1


async def test_bots_limit_no_subscription_fallback_starter(mocker):
    """No active subscription → defaults to starter limits (1 bot)."""
    mocker.patch("main.get_active_subscription", return_value=None)
    mocker.patch("main.count_client_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is True
    assert plan == "Старт"
    assert count == 0
    assert limit == 1


async def test_bots_limit_no_subscription_blocks_at_starter_cap(mocker):
    """No subscription + 1 bot → blocked (starter fallback enforced)."""
    mocker.patch("main.get_active_subscription", return_value=None)
    mocker.patch("main.count_client_bots", return_value=1)

    from main import _check_bots_limit

    allowed, _plan, _count, _limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is False


async def test_bots_limit_admin_bypass(mocker):
    """Admin (telegram_id in ADMIN_TELEGRAM_IDS) bypasses any limit
    regardless of bot count or subscription state. Mocks NOT called —
    the admin check short-circuits before any DB touch."""
    mock_get_sub = mocker.patch("main.get_active_subscription")
    mock_count = mocker.patch("main.count_client_bots")

    from main import _check_bots_limit

    # 11111 is in ADMIN_TELEGRAM_IDS (set in conftest.py)
    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=11111
    )
    assert allowed is True
    assert "админ" in plan.lower()
    # Short-circuit — DB shouldn't be touched
    mock_get_sub.assert_not_called()
    mock_count.assert_not_called()


async def test_bots_limit_pro_at_limit(mocker):
    """Pro tier cap is 3 → 3 bots blocks the next."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro"),
    )
    mocker.patch("main.count_client_bots", return_value=3)

    from main import _check_bots_limit

    allowed, plan, _count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is False
    assert plan == "Про"
    assert limit == 3


async def test_bots_limit_business_at_limit(mocker):
    """Business cap is 10 → 10 bots blocks. Even on the highest paid
    tier, an extra bot needs explicit upgrade or admin handling."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="business"),
    )
    mocker.patch("main.count_client_bots", return_value=10)

    from main import _check_bots_limit

    allowed, plan, _count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999
    )
    assert allowed is False
    assert plan == "Бизнес"
    assert limit == 10
