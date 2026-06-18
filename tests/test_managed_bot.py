"""Tests for Wave 30: Telegram Managed Bots API (Bot API 9.6)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── managed_bots.py unit tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_managed_bot_button_payload(mocker):
    """send_managed_bot_button must POST correct JSON to Telegram."""
    captured = {}

    class FakeResp:
        async def json(self):
            return {"ok": True}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class FakeSession:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured["data"] = kwargs.get("data") or kwargs.get("json")
            return FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    mocker.patch("aiohttp.ClientSession", return_value=FakeSession())

    from managed_bots import send_managed_bot_button
    await send_managed_bot_button("123:TOKEN", chat_id=42, request_id=1)

    assert "sendMessage" in captured["url"]
    markup = json.loads(captured["data"]["reply_markup"])
    btn = markup["keyboard"][0][0]
    assert btn["request_managed_bot"]["request_id"] == 1
    assert "🤖" in btn["text"]


@pytest.mark.asyncio
async def test_send_managed_bot_button_optional_fields(mocker):
    captured = {}

    class FakeResp:
        async def json(self):
            return {"ok": True}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class FakeSession:
        def post(self, url, **kwargs):
            captured["data"] = kwargs.get("data") or kwargs.get("json")
            return FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    mocker.patch("aiohttp.ClientSession", return_value=FakeSession())

    from managed_bots import send_managed_bot_button
    await send_managed_bot_button(
        "123:TOKEN", chat_id=42, request_id=7,
        suggested_name="MyBot", suggested_username="my_bot"
    )

    markup = json.loads(captured["data"]["reply_markup"])
    field = markup["keyboard"][0][0]["request_managed_bot"]
    assert field["suggested_name"] == "MyBot"
    assert field["suggested_username"] == "my_bot"


@pytest.mark.asyncio
async def test_send_managed_bot_button_raises_on_error(mocker):
    class FakeResp:
        async def json(self):
            return {"ok": False, "description": "Forbidden"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class FakeSession:
        def post(self, *a, **kw):
            return FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    mocker.patch("aiohttp.ClientSession", return_value=FakeSession())

    from managed_bots import send_managed_bot_button
    with pytest.raises(RuntimeError, match="Forbidden"):
        await send_managed_bot_button("123:TOKEN", chat_id=42, request_id=1)


@pytest.mark.asyncio
async def test_get_managed_bot_token_returns_string(mocker):
    class FakeResp:
        async def json(self):
            return {"ok": True, "result": "987654321:CHILD_TOKEN"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class FakeSession:
        def post(self, url, **kwargs):
            self._url = url
            self._payload = kwargs.get("json")
            return FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    fake = FakeSession()
    mocker.patch("aiohttp.ClientSession", return_value=fake)

    from managed_bots import get_managed_bot_token
    token = await get_managed_bot_token("123:PARENT", bot_user_id=987654321)

    assert token == "987654321:CHILD_TOKEN"
    assert "getManagedBotToken" in fake._url
    assert fake._payload["user_id"] == 987654321


@pytest.mark.asyncio
async def test_get_managed_bot_token_raises_on_error(mocker):
    class FakeResp:
        async def json(self):
            return {"ok": False, "description": "Bot not found"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class FakeSession:
        def post(self, *a, **kw):
            return FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    mocker.patch("aiohttp.ClientSession", return_value=FakeSession())

    from managed_bots import get_managed_bot_token
    with pytest.raises(RuntimeError, match="Bot not found"):
        await get_managed_bot_token("123:TOKEN", bot_user_id=1)


# ── ManagedBotMiddleware unit tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_middleware_passes_through_regular_updates(mocker):
    """Non-managed_bot updates must flow through unchanged."""
    from main import ManagedBotMiddleware

    handler = AsyncMock(return_value="ok")
    event = MagicMock()
    event.model_extra = {}  # no managed_bot field

    mw = ManagedBotMiddleware()
    result = await mw(handler, event, {})

    handler.assert_called_once()
    assert result == "ok"


@pytest.mark.asyncio
async def test_middleware_ignores_wrong_state(mocker):
    """managed_bot update for user not in waiting_managed_bot state is ignored."""
    from main import ManagedBotMiddleware

    handler = AsyncMock()
    event = MagicMock()
    event.model_extra = {
        "managed_bot": {
            "user": {"id": 99, "username": "tester"},
            "bot_user": {"id": 12345},
        }
    }

    mock_fsm = AsyncMock()
    mock_fsm.get_state = AsyncMock(return_value="IntakeStates:answering")

    mocker.patch("main.FSMContext", return_value=mock_fsm)
    mocker.patch("main.StorageKey")

    mw = ManagedBotMiddleware()
    await mw(handler, event, {})

    handler.assert_not_called()
    mock_fsm.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_runs_pipeline_on_managed_bot_update(mocker):
    """On valid managed_bot update in correct state, gets token and runs pipeline."""
    from main import ManagedBotMiddleware, IntakeStates

    handler = AsyncMock()
    event = MagicMock()
    event.model_extra = {
        "managed_bot": {
            "user": {"id": 42, "username": "creator"},
            "bot_user": {"id": 99999},
        }
    }

    mock_fsm = AsyncMock()
    mock_fsm.get_state = AsyncMock(
        return_value=IntakeStates.waiting_managed_bot.state
    )

    mocker.patch("main.FSMContext", return_value=mock_fsm)
    mocker.patch("main.StorageKey")
    mocker.patch("main._get_managed_bot_token", new=AsyncMock(return_value="99999:CHILD"))
    mock_core = mocker.patch("main._run_pipeline_core", new=AsyncMock())

    mw = ManagedBotMiddleware()
    await mw(handler, event, {})

    handler.assert_not_called()
    mock_fsm.set_state.assert_any_call(IntakeStates.processing)
    mock_core.assert_called_once()
    call_kwargs = mock_core.call_args
    assert call_kwargs.kwargs["bot_token"] == "99999:CHILD"
    assert call_kwargs.kwargs["user_id"] == 42
    assert call_kwargs.kwargs["username"] == "creator"
    assert call_kwargs.kwargs["platform"] == "telegram"


@pytest.mark.asyncio
async def test_middleware_fallback_on_token_fetch_failure(mocker):
    """If getManagedBotToken fails, user is prompted for manual token."""
    from main import ManagedBotMiddleware, IntakeStates

    handler = AsyncMock()
    event = MagicMock()
    event.model_extra = {
        "managed_bot": {
            "user": {"id": 42, "username": "creator"},
            "bot_user": {"id": 99999},
        }
    }

    mock_fsm = AsyncMock()
    mock_fsm.get_state = AsyncMock(
        return_value=IntakeStates.waiting_managed_bot.state
    )

    mocker.patch("main.FSMContext", return_value=mock_fsm)
    mocker.patch("main.StorageKey")
    mocker.patch(
        "main._get_managed_bot_token",
        new=AsyncMock(side_effect=RuntimeError("API error")),
    )
    mock_send = mocker.patch("main.bot.send_message", new=AsyncMock())

    mw = ManagedBotMiddleware()
    await mw(handler, event, {})

    mock_send.assert_called_once()
    mock_fsm.set_state.assert_called_with(IntakeStates.ask_bot_token)


# ── _goto_token_or_process integration tests ──────────────────────────────────

@pytest.mark.asyncio
async def test_goto_token_sends_managed_bot_button(mocker):
    """For Telegram platform, _goto_token_or_process must send the managed-bot button."""
    mock_msg = AsyncMock()
    mock_msg.from_user = MagicMock(id=42, username="user42")

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})

    mock_send_btn = mocker.patch(
        "main._send_managed_bot_button", new=AsyncMock()
    )

    from main import _goto_token_or_process
    await _goto_token_or_process(mock_msg, state)

    mock_send_btn.assert_called_once()
    state.set_state.assert_called_once()
    call_arg = state.set_state.call_args[0][0]
    from main import IntakeStates
    assert call_arg == IntakeStates.waiting_managed_bot


@pytest.mark.asyncio
async def test_goto_token_fallback_on_button_failure(mocker):
    """If send_managed_bot_button raises, fall back to ASK_TOKEN_PROMPT."""
    mock_msg = AsyncMock()
    mock_msg.from_user = MagicMock(id=42, username="user42")

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})

    mocker.patch(
        "main._send_managed_bot_button",
        new=AsyncMock(side_effect=RuntimeError("network error")),
    )

    from main import _goto_token_or_process, IntakeStates
    await _goto_token_or_process(mock_msg, state)

    # Should fall back to ask_bot_token state and send prompt
    state.set_state.assert_called_with(IntakeStates.ask_bot_token)
    mock_msg.answer.assert_called_once()


@pytest.mark.asyncio
async def test_goto_token_vk_runs_pipeline_directly(mocker):
    """VK platform must run pipeline directly without managed-bot button."""
    mock_msg = AsyncMock()
    mock_msg.from_user = MagicMock(id=42, username="user42")

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"platform": "vk", "vk_token": "vk_tok"})

    mock_pipeline = mocker.patch("main._run_pipeline_and_save", new=AsyncMock())
    mock_btn = mocker.patch("main._send_managed_bot_button", new=AsyncMock())

    from main import _goto_token_or_process, IntakeStates
    await _goto_token_or_process(mock_msg, state)

    mock_pipeline.assert_called_once_with(
        mock_msg, state, "vk_tok", platform="vk"
    )
    mock_btn.assert_not_called()


# ── IntakeStates structure ────────────────────────────────────────────────────

def test_waiting_managed_bot_state_exists():
    from main import IntakeStates
    assert hasattr(IntakeStates, "waiting_managed_bot")


def test_bot_id_derived_from_token():
    import main
    token = main.BOT_TOKEN
    expected_id = int(token.split(":")[0])
    assert main.BOT_ID == expected_id
