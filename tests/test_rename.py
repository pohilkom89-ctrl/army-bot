"""Tests for bot rename functionality (rename_bot repository + on_edit_rename handler)."""
import pytest


# ---------------------------------------------------------------------------
# repository.rename_bot
# ---------------------------------------------------------------------------

async def test_rename_bot_success(fresh_db):
    """rename_bot updates bot_name and returns True for the owning client."""
    from db.repository import save_bot_config, get_bot_by_id, rename_bot

    cfg = await save_bot_config(
        client_id=1, bot_name="OldName", bot_type="faq",
        bot_token="tok1", system_prompt="p", config={},
    )
    ok = await rename_bot(cfg.id, client_id=1, new_name="NewName")
    assert ok is True

    updated = await get_bot_by_id(cfg.id, client_id=1)
    assert updated.bot_name == "NewName"


async def test_rename_bot_wrong_owner(fresh_db):
    """rename_bot returns False when client_id doesn't own the bot."""
    from db.repository import save_bot_config, rename_bot

    cfg = await save_bot_config(
        client_id=1, bot_name="OldName", bot_type="faq",
        bot_token="tok2", system_prompt="p", config={},
    )
    ok = await rename_bot(cfg.id, client_id=99, new_name="Hacked")
    assert ok is False


async def test_rename_bot_nonexistent(fresh_db):
    """rename_bot returns False for a bot_id that doesn't exist."""
    from db.repository import rename_bot

    ok = await rename_bot(bot_id=99999, client_id=1, new_name="Ghost")
    assert ok is False


# ---------------------------------------------------------------------------
# on_edit_rename handler (unit — DB mocked)
# ---------------------------------------------------------------------------

async def test_on_edit_rename_success(mocker):
    """Happy path: valid name → rename_bot called → state cleared → confirmation sent."""
    mocker.patch("main.get_or_create_client", return_value=mocker.MagicMock(id=1))
    mocker.patch("main.rename_bot", return_value=True)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={"edit_bot_id": 42})
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=100, username="user")
    message.text = "MyNewBot"
    message.answer = mocker.AsyncMock()

    from main import on_edit_rename
    await on_edit_rename(message, state)

    import main as m
    m.rename_bot.assert_awaited_once_with(42, 1, "MyNewBot")
    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert "MyNewBot" in message.answer.call_args.args[0]


async def test_on_edit_rename_empty(mocker):
    """Empty name → prompt to retry, state NOT cleared."""
    state = mocker.AsyncMock()
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=100)
    message.text = "   "
    message.answer = mocker.AsyncMock()

    from main import on_edit_rename
    await on_edit_rename(message, state)

    state.clear.assert_not_called()
    message.answer.assert_awaited_once()


async def test_on_edit_rename_too_long(mocker):
    """Name > 64 chars → error message, state NOT cleared."""
    state = mocker.AsyncMock()
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=100)
    message.text = "A" * 65
    message.answer = mocker.AsyncMock()

    from main import on_edit_rename
    await on_edit_rename(message, state)

    state.clear.assert_not_called()
    message.answer.assert_awaited_once()


async def test_on_edit_rename_lost_session(mocker):
    """Missing edit_bot_id in FSM data → clear state, prompt to restart."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={})
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=100)
    message.text = "ValidName"
    message.answer = mocker.AsyncMock()

    from main import on_edit_rename
    await on_edit_rename(message, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_on_edit_rename_bot_not_found(mocker):
    """rename_bot returns False → clear state, error message."""
    mocker.patch("main.get_or_create_client", return_value=mocker.MagicMock(id=1))
    mocker.patch("main.rename_bot", return_value=False)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={"edit_bot_id": 42})
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=100, username="u")
    message.text = "ValidName"
    message.answer = mocker.AsyncMock()

    from main import on_edit_rename
    await on_edit_rename(message, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
