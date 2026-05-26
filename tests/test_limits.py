"""Tests for bots-limit enforcement (simple_bots_limit / combo_bots_limit).

_check_bots_limit(is_combo=None)  — general pre-intake gate
_check_bots_limit(is_combo=False) — specific gate for single-type bots
_check_bots_limit(is_combo=True)  — specific gate for multi-type (combo) bots

DB helpers are mocked; admin bypass tested through the real config.is_admin path
(ADMIN_TELEGRAM_IDS is set in conftest.py to '11111,22222')."""

import pytest


# ---------------------------------------------------------------------------
# General (pre-intake) checks — is_combo=None
# ---------------------------------------------------------------------------


async def test_general_blocked_when_all_slots_full_starter(mocker):
    """Starter: 1 simple used, 0 combo limit → all slots full."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_simple_bots", return_value=1)
    mocker.patch("main.count_combo_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is False
    assert plan == "Старт"
    assert count == 1   # 1 simple + 0 combo
    assert limit == 1   # 1 simple_limit + 0 combo_limit


async def test_general_allowed_when_simple_slot_free_starter(mocker):
    """Starter: 0 simple used → allowed."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_simple_bots", return_value=0)
    mocker.patch("main.count_combo_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is True
    assert count == 0
    assert limit == 1


async def test_general_no_subscription_fallback_starter(mocker):
    """No active subscription → defaults to starter limits."""
    mocker.patch("main.get_active_subscription", return_value=None)
    mocker.patch("main.count_simple_bots", return_value=0)
    mocker.patch("main.count_combo_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is True
    assert plan == "Старт"
    assert limit == 1


async def test_general_blocked_no_subscription_at_starter_cap(mocker):
    """No subscription + 1 simple bot → blocked (starter fallback)."""
    mocker.patch("main.get_active_subscription", return_value=None)
    mocker.patch("main.count_simple_bots", return_value=1)
    mocker.patch("main.count_combo_bots", return_value=0)

    from main import _check_bots_limit

    allowed, _plan, _count, _limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is False


async def test_general_allowed_when_combo_slot_free_pro(mocker):
    """Pro: simple full (2/2) but combo has room (1/2) → still allowed."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro"),
    )
    mocker.patch("main.count_simple_bots", return_value=2)
    mocker.patch("main.count_combo_bots", return_value=1)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is True
    assert plan == "Про"
    assert count == 3   # 2 + 1
    assert limit == 4   # 2 + 2


async def test_general_blocked_all_slots_full_pro(mocker):
    """Pro: 2 simple + 2 combo → all slots full."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro"),
    )
    mocker.patch("main.count_simple_bots", return_value=2)
    mocker.patch("main.count_combo_bots", return_value=2)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is False
    assert plan == "Про"
    assert count == 4
    assert limit == 4


async def test_general_blocked_all_slots_full_business(mocker):
    """Business: 5 simple + 3 combo → all 8 slots full."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="business"),
    )
    mocker.patch("main.count_simple_bots", return_value=5)
    mocker.patch("main.count_combo_bots", return_value=3)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(client_id=1, telegram_id=999)
    assert allowed is False
    assert plan == "Бизнес"
    assert count == 8
    assert limit == 8


# ---------------------------------------------------------------------------
# Specific checks — is_combo=False (simple) and is_combo=True (combo)
# ---------------------------------------------------------------------------


async def test_specific_simple_blocked_starter(mocker):
    """Starter: simple slot used up → simple creation blocked."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_simple_bots", return_value=1)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999, is_combo=False
    )
    assert allowed is False
    assert plan == "Старт"
    assert count == 1
    assert limit == 1


async def test_specific_combo_blocked_starter(mocker):
    """Starter: combo_bots_limit=0 → combo always blocked."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter"),
    )
    mocker.patch("main.count_combo_bots", return_value=0)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999, is_combo=True
    )
    assert allowed is False
    assert limit == 0


async def test_specific_combo_allowed_pro(mocker):
    """Pro: 1 combo used of 2 → combo creation allowed."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro"),
    )
    mocker.patch("main.count_combo_bots", return_value=1)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999, is_combo=True
    )
    assert allowed is True
    assert plan == "Про"
    assert count == 1
    assert limit == 2


async def test_specific_combo_blocked_pro_at_limit(mocker):
    """Pro: 2 combo used of 2 → blocked even if simple slots free."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro"),
    )
    mocker.patch("main.count_combo_bots", return_value=2)

    from main import _check_bots_limit

    allowed, _plan, _count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999, is_combo=True
    )
    assert allowed is False
    assert limit == 2


async def test_specific_combo_business_limits(mocker):
    """Business: combo limit is 3, not 5 (simple limit)."""
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="business"),
    )
    mocker.patch("main.count_combo_bots", return_value=3)

    from main import _check_bots_limit

    allowed, plan, count, limit = await _check_bots_limit(
        client_id=1, telegram_id=999, is_combo=True
    )
    assert allowed is False
    assert plan == "Бизнес"
    assert limit == 3


# ---------------------------------------------------------------------------
# Admin bypass
# ---------------------------------------------------------------------------


async def test_admin_bypass(mocker):
    """Admin (telegram_id in ADMIN_TELEGRAM_IDS) bypasses all checks.
    DB helpers must NOT be called regardless of is_combo."""
    mock_sub = mocker.patch("main.get_active_subscription")
    mock_simple = mocker.patch("main.count_simple_bots")
    mock_combo = mocker.patch("main.count_combo_bots")

    from main import _check_bots_limit

    for is_combo in (None, False, True):
        allowed, plan, count, limit = await _check_bots_limit(
            client_id=1, telegram_id=11111, is_combo=is_combo
        )
        assert allowed is True
        assert "админ" in plan.lower()

    mock_sub.assert_not_called()
    mock_simple.assert_not_called()
    mock_combo.assert_not_called()
