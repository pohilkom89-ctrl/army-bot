"""Tests for Wave 21 — VK bot creation UI flow."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --- VK token validation ---

def _make_vk_validate(response_data: dict):
    """Build a mock aiohttp session that returns response_data as JSON."""
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session_get = MagicMock(return_value=mock_resp)
    mock_session = MagicMock()
    mock_session.get = mock_session_get
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_validate_vk_token_valid():
    from main import _validate_vk_token
    mock_session = _make_vk_validate({"response": [{"id": 123}]})
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await _validate_vk_token("valid_token_abc")
    assert result is True


@pytest.mark.asyncio
async def test_validate_vk_token_invalid():
    from main import _validate_vk_token
    mock_session = _make_vk_validate({"error": {"error_code": 5, "error_msg": "User authorization failed"}})
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await _validate_vk_token("bad_token")
    assert result is False


@pytest.mark.asyncio
async def test_validate_vk_token_network_error():
    from main import _validate_vk_token
    with patch("aiohttp.ClientSession", side_effect=Exception("network error")):
        result = await _validate_vk_token("any_token")
    assert result is False


# --- IntakeStates includes ask_vk_token ---

def test_intake_states_has_ask_vk_token():
    from main import IntakeStates
    assert hasattr(IntakeStates, "ask_vk_token")


# --- _bot_type_keyboard includes VK button ---

def test_bot_type_keyboard_has_vk_button():
    from main import _bot_type_keyboard
    kb = _bot_type_keyboard()
    all_callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert "vk:start" in all_callbacks


def test_bot_type_keyboard_vk_has_vk_text():
    from main import _bot_type_keyboard
    kb = _bot_type_keyboard()
    vk_texts = [
        btn.text
        for row in kb.inline_keyboard
        for btn in row
        if btn.callback_data == "vk:start"
    ]
    assert any("VK" in t or "ВКонтакте" in t for t in vk_texts)


# --- _bot_type_keyboard_vk does NOT include vk:start ---

def test_bot_type_keyboard_vk_no_vk_entry():
    from main import _bot_type_keyboard_vk
    kb = _bot_type_keyboard_vk()
    all_callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert "vk:start" not in all_callbacks


# --- _goto_token_or_process ---

@pytest.mark.asyncio
async def test_goto_token_or_process_telegram_asks_token():
    from main import _goto_token_or_process, ASK_TOKEN_PROMPT, IntakeStates

    message = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"platform": "telegram"})
    state.set_state = AsyncMock()

    with patch("main._send_managed_bot_button", side_effect=Exception("no API")):
        await _goto_token_or_process(message, state)

    state.set_state.assert_any_call(IntakeStates.ask_bot_token)
    message.answer.assert_called_once_with(ASK_TOKEN_PROMPT)


@pytest.mark.asyncio
async def test_goto_token_or_process_vk_runs_pipeline():
    from main import _goto_token_or_process, IntakeStates

    message = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"platform": "vk", "vk_token": "tok123"})
    state.set_state = AsyncMock()

    with patch("main._run_pipeline_and_save", new_callable=AsyncMock) as mock_run:
        await _goto_token_or_process(message, state)

    state.set_state.assert_called_once_with(IntakeStates.processing)
    mock_run.assert_called_once_with(message, state, "tok123", platform="vk")


@pytest.mark.asyncio
async def test_goto_token_or_process_no_platform_asks_token():
    """No platform key in FSM data defaults to Telegram flow."""
    from main import _goto_token_or_process, ASK_TOKEN_PROMPT, IntakeStates

    message = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    state.set_state = AsyncMock()

    with patch("main._send_managed_bot_button", side_effect=Exception("no API")):
        await _goto_token_or_process(message, state)

    state.set_state.assert_any_call(IntakeStates.ask_bot_token)
    message.answer.assert_called_once_with(ASK_TOKEN_PROMPT)


# --- _platform_icon ---

def test_platform_icon_telegram():
    from main import _platform_icon
    bot = MagicMock()
    bot.platform = "telegram"
    assert _platform_icon(bot) == "⚙️"


def test_platform_icon_vk():
    from main import _platform_icon
    bot = MagicMock()
    bot.platform = "vk"
    assert _platform_icon(bot) == "🔵"


def test_platform_icon_missing_attr_defaults_telegram():
    from main import _platform_icon
    bot = MagicMock(spec=[])  # no attributes
    assert _platform_icon(bot) == "⚙️"


# --- save_bot_config stores platform ---

@pytest.mark.asyncio
async def test_save_bot_config_stores_vk_platform(fresh_db):
    from db.repository import save_bot_config, get_or_create_client
    client = await get_or_create_client(99999, "vk_test_user")
    bot = await save_bot_config(
        client_id=client.id,
        bot_type="support",
        bot_name="vk_support_bot",
        system_prompt="Вы — поддержка.",
        config={},
        bot_token="vk_community_token_xyz",
        platform="vk",
    )
    assert bot.platform == "vk"
    assert bot.bot_token == "vk_community_token_xyz"


@pytest.mark.asyncio
async def test_save_bot_config_default_platform_is_telegram(fresh_db):
    from db.repository import save_bot_config, get_or_create_client
    client = await get_or_create_client(99998, "tg_test_user")
    bot = await save_bot_config(
        client_id=client.id,
        bot_type="support",
        bot_name="tg_support_bot",
        system_prompt="Help.",
        config={},
        bot_token="123456789:fake_tg_token",
    )
    assert bot.platform == "telegram"
