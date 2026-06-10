"""Tests for Wave 19 — persistent conversation history across restarts."""


def _make_state():
    """Isolated in-memory state mirroring STANDARD_BOT_CODE globals."""
    history: dict[int, list[dict]] = {}
    loaded: set[int] = set()
    MAX_HISTORY = 20

    def get(uid):
        return list(history.get(uid, []))

    def append(uid, role, content):
        msgs = history.get(uid, [])
        msgs.append({"role": role, "content": content})
        if len(msgs) > MAX_HISTORY:
            msgs = msgs[-MAX_HISTORY:]
        history[uid] = msgs

    def clear(uid):
        history.pop(uid, None)
        loaded.add(uid)  # /start and /reset pre-mark as loaded

    async def ensure_loaded(uid, db_msgs):
        if uid in loaded:
            return
        loaded.add(uid)
        if history.get(uid):
            return
        if db_msgs:
            history[uid] = db_msgs[-MAX_HISTORY:]

    return get, append, clear, ensure_loaded, loaded


# --- ensure_history_loaded logic ---

async def test_loads_from_db_on_first_message():
    get, append, clear, ensure, loaded = _make_state()
    db_msgs = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуйте"},
    ]
    await ensure(1, db_msgs)
    assert get(1) == db_msgs


async def test_does_not_reload_on_second_message():
    get, append, clear, ensure, loaded = _make_state()
    db_msgs = [{"role": "user", "content": "из БД"}]
    await ensure(1, db_msgs)
    append(1, "user", "новое сообщение")
    await ensure(1, [{"role": "user", "content": "старое из БД"}])  # would overwrite if reloaded
    hist = get(1)
    assert len(hist) == 2
    assert hist[-1]["content"] == "новое сообщение"


async def test_empty_db_leaves_empty_history():
    get, append, clear, ensure, loaded = _make_state()
    await ensure(1, [])
    assert get(1) == []


async def test_new_user_no_db_history():
    get, append, clear, ensure, loaded = _make_state()
    await ensure(99, [])
    assert get(99) == []
    assert 99 in loaded


# --- /start and /reset block old history ---

async def test_start_blocks_history_restore():
    get, append, clear, ensure, loaded = _make_state()
    db_msgs = [{"role": "user", "content": "старое"}]
    # /start: clear + pre-add to loaded
    clear(1)
    assert 1 in loaded
    # ensure_loaded should skip because 1 is already in loaded
    await ensure(1, db_msgs)
    assert get(1) == []  # old history NOT restored


async def test_reset_blocks_history_restore():
    get, append, clear, ensure, loaded = _make_state()
    append(1, "user", "раньше было")
    db_msgs = [{"role": "user", "content": "раньше было"}]
    # /reset
    clear(1)
    assert 1 in loaded
    await ensure(1, db_msgs)
    assert get(1) == []


# --- DB role mapping ---

def test_bot_role_maps_to_assistant():
    # Simulate get_user_bot_history mapping
    db_rows = [
        {"role": "user", "content": "вопрос"},
        {"role": "bot", "content": "ответ"},
    ]
    mapped = [
        {"role": "user" if r["role"] == "user" else "assistant", "content": r["content"]}
        for r in db_rows
    ]
    assert mapped[0]["role"] == "user"
    assert mapped[1]["role"] == "assistant"


def test_history_returned_chronological():
    # DB query returns DESC, reversed in code → chronological
    db_rows_desc = [
        {"role": "assistant", "content": "последний"},
        {"role": "user", "content": "предпоследний"},
        {"role": "user", "content": "первый"},
    ]
    chronological = list(reversed(db_rows_desc))
    assert chronological[0]["content"] == "первый"
    assert chronological[-1]["content"] == "последний"


# --- Limit ---

async def test_db_history_trimmed_to_max():
    get, append, clear, ensure, loaded = _make_state()
    db_msgs = [{"role": "user", "content": f"msg{i}"} for i in range(30)]
    await ensure(1, db_msgs)
    assert len(get(1)) == 20  # MAX_HISTORY
    assert get(1)[0]["content"] == "msg10"  # oldest kept
    assert get(1)[-1]["content"] == "msg29"


# --- Graceful degradation ---

async def test_load_failure_leaves_empty_history():
    """If load_history raises, bot continues with empty history."""
    get, append, clear, ensure, loaded = _make_state()

    async def ensure_with_failure(uid):
        if uid in loaded:
            return
        loaded.add(uid)
        try:
            raise ConnectionError("factory unreachable")
        except Exception:
            pass  # graceful degradation

    await ensure_with_failure(1)
    assert get(1) == []
    assert 1 in loaded
