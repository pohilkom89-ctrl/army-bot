"""Tests for Waves 23–25: onboarding tutorial, referral notification, bot avatar."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Wave 23: Onboarding tutorial ────────────────────────────────────────────

def test_onboarding_pages_count():
    from main import _ONBOARDING_PAGES, _HELP_TOTAL
    assert len(_ONBOARDING_PAGES) == _HELP_TOTAL
    assert _HELP_TOTAL >= 5


def test_onboarding_pages_are_tuples_with_title_and_body():
    from main import _ONBOARDING_PAGES
    for item in _ONBOARDING_PAGES:
        assert isinstance(item, tuple) and len(item) == 2
        title, body = item
        assert len(title) > 0
        assert len(body) > 20


def test_help_page_keyboard_first_page_no_back():
    from main import _help_page_keyboard, _HELP_TOTAL
    kb = _help_page_keyboard(0)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert not any("page:-1" in c for c in callbacks)
    assert any(f"page:1" in c for c in callbacks)


def test_help_page_keyboard_last_page_no_next():
    from main import _help_page_keyboard, _HELP_TOTAL
    last = _HELP_TOTAL - 1
    kb = _help_page_keyboard(last)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert not any(f"page:{_HELP_TOTAL}" in c for c in callbacks)
    assert any(f"page:{last - 1}" in c for c in callbacks)


def test_help_page_keyboard_last_page_has_close():
    from main import _help_page_keyboard, _HELP_TOTAL
    kb = _help_page_keyboard(_HELP_TOTAL - 1)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "help:close" in callbacks


def test_help_page_keyboard_middle_has_both_nav():
    from main import _help_page_keyboard, _HELP_TOTAL
    mid = _HELP_TOTAL // 2
    kb = _help_page_keyboard(mid)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(f"page:{mid - 1}" in c for c in callbacks)
    assert any(f"page:{mid + 1}" in c for c in callbacks)


@pytest.mark.asyncio
async def test_cb_help_page_edits_message():
    from main import cb_help_page
    callback = MagicMock()
    callback.data = "help:page:2"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()
    await cb_help_page(callback)
    callback.message.edit_text.assert_called_once()
    callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cb_help_noop_answers_only():
    from main import cb_help_noop
    callback = MagicMock()
    callback.answer = AsyncMock()
    await cb_help_noop(callback)
    callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cb_help_close_removes_keyboard():
    from main import cb_help_close
    callback = MagicMock()
    callback.message = AsyncMock()
    callback.answer = AsyncMock()
    await cb_help_close(callback)
    callback.message.edit_reply_markup.assert_called_once_with(reply_markup=None)


# ─── Wave 24: Referral reward notification ───────────────────────────────────

@pytest.mark.asyncio
async def test_apply_pending_referral_reward_returns_none_when_no_referral(fresh_db):
    from db.repository import apply_pending_referral_reward, get_or_create_client
    client = await get_or_create_client(88881, "no_referral_user")
    result = await apply_pending_referral_reward(client.id)
    assert result is None


@pytest.mark.asyncio
async def test_apply_pending_referral_reward_returns_telegram_id(fresh_db):
    from db.repository import (
        apply_pending_referral_reward, get_or_create_client, set_referred_by
    )
    referrer = await get_or_create_client(88882, "referrer_user")
    referee = await get_or_create_client(88883, "referee_user")
    await set_referred_by(referee.id, referrer.id)

    result = await apply_pending_referral_reward(referee.id)
    # Should return referrer's telegram_id (truthy)
    assert result == 88882


@pytest.mark.asyncio
async def test_apply_pending_referral_reward_idempotent(fresh_db):
    from db.repository import (
        apply_pending_referral_reward, get_or_create_client, set_referred_by
    )
    referrer = await get_or_create_client(88884, "referrer2")
    referee = await get_or_create_client(88885, "referee2")
    await set_referred_by(referee.id, referrer.id)

    first = await apply_pending_referral_reward(referee.id)
    second = await apply_pending_referral_reward(referee.id)
    assert first is not None
    assert second is None  # already rewarded


@pytest.mark.asyncio
async def test_handle_webhook_sends_notification_on_reward():
    """handle_webhook calls _notify_referral_reward when reward is applied."""
    from billing import handle_webhook

    mock_bot = AsyncMock()
    data = {
        "event": "payment.succeeded",
        "object": {"id": "pay_test_001", "status": "succeeded", "metadata": {
            "client_id": "1", "tier": "starter", "cycle": "monthly",
        }},
    }

    with patch("billing.find_subscription_by_payment_id", new_callable=AsyncMock, return_value=None), \
         patch("billing.create_subscription", new_callable=AsyncMock), \
         patch("billing.apply_pending_referral_reward", new_callable=AsyncMock, return_value=99999), \
         patch("billing._notify_referral_reward", new_callable=AsyncMock) as mock_notify, \
         patch("billing.asyncio.create_task") as mock_task:
        await handle_webhook(data, bot=mock_bot)

    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_handle_webhook_no_notification_without_reward():
    from billing import handle_webhook

    mock_bot = AsyncMock()
    data = {
        "event": "payment.succeeded",
        "object": {"id": "pay_test_002", "status": "succeeded", "metadata": {
            "client_id": "2", "tier": "starter", "cycle": "monthly",
        }},
    }

    with patch("billing.find_subscription_by_payment_id", new_callable=AsyncMock, return_value=None), \
         patch("billing.create_subscription", new_callable=AsyncMock), \
         patch("billing.apply_pending_referral_reward", new_callable=AsyncMock, return_value=None), \
         patch("billing.asyncio.create_task") as mock_task:
        await handle_webhook(data, bot=mock_bot)

    mock_task.assert_not_called()


# ─── Wave 25: Bot avatar ─────────────────────────────────────────────────────

def test_avatar_prompt_by_type():
    from main import _avatar_prompt
    bot = MagicMock()
    bot.bot_type = "support"
    bot.bot_name = "HelpBot"
    prompt = _avatar_prompt(bot)
    assert "support" in prompt.lower() or "customer" in prompt.lower()
    assert "icon" in prompt.lower()


def test_avatar_prompt_unknown_type_fallback():
    from main import _avatar_prompt
    bot = MagicMock()
    bot.bot_type = "unknown_type_xyz"
    prompt = _avatar_prompt(bot)
    assert "AI" in prompt or "bot" in prompt.lower()


def test_edit_menu_telegram_has_avatar_button():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(1, platform="telegram")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any("set_avatar" in c for c in callbacks)


def test_edit_menu_vk_no_avatar_button():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(1, platform="vk")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert not any("set_avatar" in c for c in callbacks)
