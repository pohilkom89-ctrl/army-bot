"""Tests for Wave 22 — VK bot edit-menu (no quick_replies)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --- _edit_menu_keyboard hides quick_replies for VK ---

def test_edit_menu_telegram_has_quick_replies():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(1, platform="telegram")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any("quick_replies" in c for c in callbacks)


def test_edit_menu_vk_no_quick_replies():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(1, platform="vk")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert not any("quick_replies" in c for c in callbacks)


def test_edit_menu_default_has_quick_replies():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(42)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any("quick_replies" in c for c in callbacks)


def test_edit_menu_vk_has_all_other_items():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(5, platform="vk")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    for expected in ("edit_prompt", "edit_style", "edit_forbidden", "edit_scripts",
                     "edit_greeting", "blacklist", "edit_webhook", "triggers",
                     "rate_limit", "edit_name"):
        assert any(expected in c for c in callbacks), f"missing {expected} in VK edit menu"


def test_edit_menu_vk_has_back_button():
    from main import _edit_menu_keyboard
    kb = _edit_menu_keyboard(7, platform="vk")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"bot:manage:7" in callbacks


def test_edit_menu_row_count_telegram_vs_vk():
    from main import _edit_menu_keyboard
    kb_tg = _edit_menu_keyboard(1, platform="telegram")
    kb_vk = _edit_menu_keyboard(1, platform="vk")
    # VK hides quick_replies + avatar (2 Telegram-only buttons)
    assert len(kb_vk.inline_keyboard) == len(kb_tg.inline_keyboard) - 2


# --- cb_bot_resume dispatches to deploy_vk_bot for VK ---

@pytest.mark.asyncio
async def test_cb_bot_resume_calls_deploy_vk_bot_for_vk(fresh_db):
    from db.repository import save_bot_config, get_or_create_client

    client = await get_or_create_client(77771, "resume_vk_user")
    bot = await save_bot_config(
        client_id=client.id,
        bot_type="support",
        bot_name="my_vk_bot",
        system_prompt="Help",
        config={},
        bot_token="vk_token_abc",
        platform="vk",
    )

    callback = MagicMock()
    callback.from_user = MagicMock(id=77771, username="resume_vk_user")
    callback.data = f"bot:resume:{bot.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("main.get_or_create_client", new_callable=AsyncMock, return_value=client), \
         patch("main.set_bot_status", new_callable=AsyncMock, return_value=True), \
         patch("main.get_bot_by_id", new_callable=AsyncMock, return_value=bot), \
         patch("main.deploy_vk_bot", new_callable=AsyncMock) as mock_vk, \
         patch("main.deploy_bot", new_callable=AsyncMock) as mock_tg:
        from main import cb_bot_resume
        await cb_bot_resume(callback)

    mock_vk.assert_called_once_with(bot.id)
    mock_tg.assert_not_called()


@pytest.mark.asyncio
async def test_cb_bot_resume_calls_deploy_bot_for_telegram(fresh_db):
    from db.repository import save_bot_config, get_or_create_client

    client = await get_or_create_client(77772, "resume_tg_user")
    bot = await save_bot_config(
        client_id=client.id,
        bot_type="support",
        bot_name="my_tg_bot",
        system_prompt="Help",
        config={},
        bot_token="123:tg_token",
        platform="telegram",
    )

    callback = MagicMock()
    callback.from_user = MagicMock(id=77772, username="resume_tg_user")
    callback.data = f"bot:resume:{bot.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    with patch("main.get_or_create_client", new_callable=AsyncMock, return_value=client), \
         patch("main.set_bot_status", new_callable=AsyncMock, return_value=True), \
         patch("main.get_bot_by_id", new_callable=AsyncMock, return_value=bot), \
         patch("main.deploy_bot", new_callable=AsyncMock) as mock_tg, \
         patch("main.deploy_vk_bot", new_callable=AsyncMock) as mock_vk:
        from main import cb_bot_resume
        await cb_bot_resume(callback)

    mock_tg.assert_called_once_with(bot.id)
    mock_vk.assert_not_called()
