"""Tests for broadcasting: upsert_subscriber, count/get_subscribers,
and the on_broadcast_text handler."""


# ---------------------------------------------------------------------------
# repository functions
# ---------------------------------------------------------------------------

async def _make_bot(client_id: int = 1, token: str = "tok_br"):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id, bot_name="BroadcastBot", bot_type="faq",
        bot_token=token, system_prompt="p", config={},
    )


async def test_upsert_subscriber_new(fresh_db):
    """upsert_subscriber adds a new row."""
    from db.repository import upsert_subscriber, count_subscribers
    bot = await _make_bot()
    await upsert_subscriber(bot.id, telegram_id=111)
    assert await count_subscribers(bot.id) == 1


async def test_upsert_subscriber_idempotent(fresh_db):
    """upsert_subscriber is idempotent — inserting twice = 1 row."""
    from db.repository import upsert_subscriber, count_subscribers
    bot = await _make_bot(token="tok_br2")
    await upsert_subscriber(bot.id, telegram_id=222)
    await upsert_subscriber(bot.id, telegram_id=222)
    assert await count_subscribers(bot.id) == 1


async def test_upsert_multiple_subscribers(fresh_db):
    """Multiple distinct telegram_ids stored correctly."""
    from db.repository import upsert_subscriber, count_subscribers, get_subscriber_ids
    bot = await _make_bot(token="tok_br3")
    for tid in [10, 20, 30]:
        await upsert_subscriber(bot.id, telegram_id=tid)
    assert await count_subscribers(bot.id) == 3
    ids = await get_subscriber_ids(bot.id)
    assert set(ids) == {10, 20, 30}


async def test_count_subscribers_empty(fresh_db):
    """count_subscribers returns 0 for a bot with no subscribers."""
    from db.repository import count_subscribers
    bot = await _make_bot(token="tok_br4")
    assert await count_subscribers(bot.id) == 0


# ---------------------------------------------------------------------------
# on_broadcast_text handler
# ---------------------------------------------------------------------------

async def test_broadcast_no_subscribers(mocker):
    """Empty subscriber list → state cleared, 'no subscribers' message."""
    mocker.patch("main.get_bot_by_id", return_value=mocker.MagicMock(
        bot_name="TestBot", bot_token="tok"
    ))
    mocker.patch("main.get_subscriber_ids", return_value=[])

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1, "broadcast_bot_token": "tok"
    })
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=1)
    message.text = "Hello subscribers!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_text
    await on_broadcast_text(message, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited()


async def test_broadcast_lost_session(mocker):
    """Missing FSM data → state cleared, session-lost message."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={})
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=1)
    message.text = "Hello!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_text
    await on_broadcast_text(message, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_broadcast_empty_text(mocker):
    """Empty message text → prompt to retry, state NOT cleared."""
    state = mocker.AsyncMock()
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=1)
    message.text = "   "
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_text
    await on_broadcast_text(message, state)

    state.clear.assert_not_called()
    message.answer.assert_awaited_once()


async def test_broadcast_sends_and_reports(mocker):
    """Happy path: 3 subscribers, Bot.send_message called 3 times."""
    mocker.patch("main.get_subscriber_ids", return_value=[10, 20, 30])

    mock_bot_instance = mocker.AsyncMock()
    mock_bot_instance.send_message = mocker.AsyncMock()
    mock_bot_instance.session = mocker.AsyncMock()
    mocker.patch("main.Bot", return_value=mock_bot_instance)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1, "broadcast_bot_token": "tok"
    })
    message = mocker.MagicMock()
    message.from_user = mocker.MagicMock(id=1)
    message.text = "Big announcement!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_text
    await on_broadcast_text(message, state)

    assert mock_bot_instance.send_message.call_count == 3
    state.clear.assert_awaited_once()
    # Final summary message should mention sent count
    last_call = message.answer.call_args_list[-1].args[0]
    assert "3" in last_call
