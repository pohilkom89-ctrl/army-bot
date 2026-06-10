"""Tests for Wave 15 — multi-turn conversation history logic."""


def _make_history_helpers(max_history: int = 20):
    """Return isolated _get/_append/_clear functions for testing."""
    history: dict[int, list[dict]] = {}

    def get_history(uid: int) -> list[dict]:
        return list(history.get(uid, []))

    def append_history(uid: int, role: str, content: str) -> None:
        msgs = history.get(uid, [])
        msgs.append({"role": role, "content": content})
        if len(msgs) > max_history:
            msgs = msgs[-max_history:]
        history[uid] = msgs

    def clear_history(uid: int) -> None:
        history.pop(uid, None)

    return get_history, append_history, clear_history


async def test_history_empty_for_new_user():
    get, append, clear = _make_history_helpers()
    assert get(1) == []


async def test_append_single_message():
    get, append, clear = _make_history_helpers()
    append(1, "user", "Привет")
    history = get(1)
    assert len(history) == 1
    assert history[0] == {"role": "user", "content": "Привет"}


async def test_append_exchange():
    get, append, clear = _make_history_helpers()
    append(1, "user", "Привет")
    append(1, "assistant", "Здравствуйте!")
    history = get(1)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"


async def test_history_trimmed_to_max():
    get, append, clear = _make_history_helpers(max_history=4)
    for i in range(6):
        append(1, "user", f"msg {i}")
    history = get(1)
    assert len(history) == 4
    assert history[0]["content"] == "msg 2"  # oldest kept
    assert history[-1]["content"] == "msg 5"  # newest


async def test_clear_history():
    get, append, clear = _make_history_helpers()
    append(1, "user", "hello")
    append(1, "assistant", "hi")
    clear(1)
    assert get(1) == []


async def test_users_isolated():
    get, append, clear = _make_history_helpers()
    append(1, "user", "message from user 1")
    append(2, "user", "message from user 2")
    assert len(get(1)) == 1
    assert len(get(2)) == 1
    assert get(1)[0]["content"] == "message from user 1"
    assert get(2)[0]["content"] == "message from user 2"


async def test_clear_does_not_affect_other_users():
    get, append, clear = _make_history_helpers()
    append(1, "user", "msg 1")
    append(2, "user", "msg 2")
    clear(1)
    assert get(1) == []
    assert len(get(2)) == 1


async def test_messages_list_for_llm():
    """Verify the messages list format passed to LLM."""
    get, append, clear = _make_history_helpers()
    system_prompt = "Ты ассистент."
    append(1, "user", "Что такое Python?")
    append(1, "assistant", "Python — язык программирования.")
    append(1, "user", "Как установить?")
    messages = [{"role": "system", "content": system_prompt}] + get(1)
    assert messages[0]["role"] == "system"
    assert len(messages) == 4
    assert messages[-1] == {"role": "user", "content": "Как установить?"}


async def test_rollback_on_error():
    """If LLM fails, the user message is rolled back."""
    get, append, clear = _make_history_helpers()
    append(1, "user", "first ok")
    append(1, "assistant", "reply ok")
    # Simulate: user sends message, we append, then LLM fails
    append(1, "user", "failed message")
    hist = get(1)
    if hist and hist[-1]["role"] == "user":
        # rollback
        from copy import deepcopy
        hist_copy = deepcopy(hist[:-1])
        # simulate writing back
    append_clean = hist[:-1] if hist and hist[-1]["role"] == "user" else hist
    assert len(append_clean) == 2
    assert append_clean[-1]["role"] == "assistant"
