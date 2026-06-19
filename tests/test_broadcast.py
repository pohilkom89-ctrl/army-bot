"""Tests for broadcasting: repository functions and Wave 37 multi-step flow."""


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
# on_broadcast_content handler (Wave 37: text + photo support)
# ---------------------------------------------------------------------------

async def test_broadcast_content_text_stores_and_asks_confirm(mocker):
    """Text message → store content, move to confirming, show preview."""
    mocker.patch("main.get_subscriber_ids", return_value=[1, 2, 3])

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={"broadcast_bot_id": 1})
    message = mocker.MagicMock()
    message.photo = None
    message.text = "Big announcement!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_content, BroadcastStates
    await on_broadcast_content(message, state)

    state.set_state.assert_awaited_with(BroadcastStates.confirming)
    message.answer.assert_awaited_once()
    preview_text = message.answer.call_args.args[0]
    assert "Предпросмотр" in preview_text
    assert "3" in preview_text


async def test_broadcast_content_empty_text_prompts_retry(mocker):
    """Empty text → prompt to retry, state NOT changed."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={"broadcast_bot_id": 1})
    message = mocker.MagicMock()
    message.photo = None
    message.text = "   "
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_content
    await on_broadcast_content(message, state)

    state.clear.assert_not_called()
    state.set_state.assert_not_called()
    message.answer.assert_awaited_once()


async def test_broadcast_content_lost_session_clears(mocker):
    """Missing broadcast_bot_id → state cleared."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={})
    message = mocker.MagicMock()
    message.photo = None
    message.text = "Hello!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_content
    await on_broadcast_content(message, state)

    state.clear.assert_awaited_once()


async def test_broadcast_content_photo_stores_file_id(mocker):
    """Photo message → stores photo file_id and caption."""
    mocker.patch("main.get_subscriber_ids", return_value=[1])

    photo_size = mocker.MagicMock()
    photo_size.file_id = "photo_abc_123"

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={"broadcast_bot_id": 1})
    message = mocker.MagicMock()
    message.photo = [photo_size]
    message.caption = "Look at this!"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_content, BroadcastStates
    await on_broadcast_content(message, state)

    update_calls = [str(c) for c in state.update_data.call_args_list]
    combined = " ".join(update_calls)
    assert "photo_abc_123" in combined
    assert "photo" in combined
    state.set_state.assert_awaited_with(BroadcastStates.confirming)


# ---------------------------------------------------------------------------
# on_broadcast_url handler
# ---------------------------------------------------------------------------

async def test_broadcast_url_parses_pipe_format(mocker):
    """URL|text format → stored correctly."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1, "broadcast_sub_count": 5,
        "broadcast_content_type": "text", "broadcast_text": "Hi",
        "broadcast_caption": "", "broadcast_btn_url": "", "broadcast_btn_text": "",
    })
    message = mocker.MagicMock()
    message.text = "https://armybots.ru|Попробовать"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_url, BroadcastStates
    await on_broadcast_url(message, state)

    calls = {k: v for call in state.update_data.call_args_list for k, v in call.kwargs.items()}
    assert calls.get("broadcast_btn_url") == "https://armybots.ru"
    assert calls.get("broadcast_btn_text") == "Попробовать"
    state.set_state.assert_awaited_with(BroadcastStates.confirming)


async def test_broadcast_url_skip(mocker):
    """'/skip' → empty URL stored."""
    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1, "broadcast_sub_count": 1,
        "broadcast_content_type": "text", "broadcast_text": "Hi",
        "broadcast_caption": "", "broadcast_btn_url": "", "broadcast_btn_text": "",
    })
    message = mocker.MagicMock()
    message.text = "/skip"
    message.answer = mocker.AsyncMock()

    from main import on_broadcast_url
    await on_broadcast_url(message, state)

    calls = {k: v for call in state.update_data.call_args_list for k, v in call.kwargs.items()}
    assert calls.get("broadcast_btn_url") == ""


# ---------------------------------------------------------------------------
# cb_broadcast_do_send handler
# ---------------------------------------------------------------------------

async def test_broadcast_do_send_text_calls_send_message(mocker):
    """Happy path text broadcast: send_message called once per subscriber."""
    mocker.patch("main.get_subscriber_ids", return_value=[10, 20, 30])

    mock_bot_instance = mocker.AsyncMock()
    mock_bot_instance.send_message = mocker.AsyncMock()
    mock_bot_instance.session = mocker.AsyncMock()
    mocker.patch("main.Bot", return_value=mock_bot_instance)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1,
        "broadcast_bot_token": "tok",
        "broadcast_content_type": "text",
        "broadcast_text": "Hello!",
        "broadcast_photo_id": None,
        "broadcast_caption": "",
        "broadcast_btn_url": "",
        "broadcast_btn_text": "",
    })
    callback = mocker.MagicMock()
    callback.message = mocker.MagicMock()
    callback.message.answer = mocker.AsyncMock()
    callback.answer = mocker.AsyncMock()

    from main import cb_broadcast_do_send
    await cb_broadcast_do_send(callback, state)

    assert mock_bot_instance.send_message.call_count == 3
    state.clear.assert_awaited_once()
    last_msg = callback.message.answer.call_args_list[-1].args[0]
    assert "3" in last_msg


async def test_broadcast_do_send_photo_calls_send_photo(mocker):
    """Photo broadcast: send_photo used instead of send_message."""
    mocker.patch("main.get_subscriber_ids", return_value=[1, 2])

    mock_bot_instance = mocker.AsyncMock()
    mock_bot_instance.send_photo = mocker.AsyncMock()
    mock_bot_instance.session = mocker.AsyncMock()
    mocker.patch("main.Bot", return_value=mock_bot_instance)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1,
        "broadcast_bot_token": "tok",
        "broadcast_content_type": "photo",
        "broadcast_text": "",
        "broadcast_photo_id": "file_xyz",
        "broadcast_caption": "Look!",
        "broadcast_btn_url": "",
        "broadcast_btn_text": "",
    })
    callback = mocker.MagicMock()
    callback.message = mocker.MagicMock()
    callback.message.answer = mocker.AsyncMock()
    callback.answer = mocker.AsyncMock()

    from main import cb_broadcast_do_send
    await cb_broadcast_do_send(callback, state)

    assert mock_bot_instance.send_photo.call_count == 2
    assert mock_bot_instance.send_message.call_count == 0


async def test_broadcast_do_send_with_url_button(mocker):
    """Broadcast with URL button passes reply_markup to send_message."""
    mocker.patch("main.get_subscriber_ids", return_value=[1])

    mock_bot_instance = mocker.AsyncMock()
    mock_bot_instance.send_message = mocker.AsyncMock()
    mock_bot_instance.session = mocker.AsyncMock()
    mocker.patch("main.Bot", return_value=mock_bot_instance)

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1,
        "broadcast_bot_token": "tok",
        "broadcast_content_type": "text",
        "broadcast_text": "Check our site!",
        "broadcast_photo_id": None,
        "broadcast_caption": "",
        "broadcast_btn_url": "https://armybots.ru",
        "broadcast_btn_text": "Открыть",
    })
    callback = mocker.MagicMock()
    callback.message = mocker.MagicMock()
    callback.message.answer = mocker.AsyncMock()
    callback.answer = mocker.AsyncMock()

    from main import cb_broadcast_do_send
    await cb_broadcast_do_send(callback, state)

    call_kwargs = mock_bot_instance.send_message.call_args.kwargs
    assert call_kwargs.get("reply_markup") is not None


async def test_broadcast_do_send_no_subscribers_aborts(mocker):
    """No subscribers → abort without sending."""
    mocker.patch("main.get_subscriber_ids", return_value=[])

    state = mocker.AsyncMock()
    state.get_data = mocker.AsyncMock(return_value={
        "broadcast_bot_id": 1, "broadcast_bot_token": "tok",
    })
    callback = mocker.MagicMock()
    callback.answer = mocker.AsyncMock()
    callback.message = mocker.MagicMock()
    callback.message.answer = mocker.AsyncMock()

    from main import cb_broadcast_do_send
    await cb_broadcast_do_send(callback, state)

    state.clear.assert_awaited_once()
    callback.answer.assert_awaited()
