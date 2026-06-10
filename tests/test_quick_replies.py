"""Tests for Wave 16 — quick reply buttons logic."""


def _make_quick_reply_helpers():
    """Return isolated get/set functions for testing (no DB)."""
    store: dict[int, list[str]] = {}

    async def get(bot_id: int, client_id: int):
        return list(store.get(bot_id, []))

    async def set_(bot_id: int, client_id: int, buttons: list[str]) -> bool:
        if len(buttons) > 10:
            return False
        store[bot_id] = list(buttons)
        return True

    return get, set_


async def test_empty_by_default():
    get, set_ = _make_quick_reply_helpers()
    result = await get(1, 1)
    assert result == []


async def test_set_and_get():
    get, set_ = _make_quick_reply_helpers()
    ok = await set_(1, 1, ["Цена", "Контакты"])
    assert ok is True
    assert await get(1, 1) == ["Цена", "Контакты"]


async def test_append_button():
    get, set_ = _make_quick_reply_helpers()
    await set_(1, 1, ["Цена"])
    buttons = await get(1, 1)
    buttons.append("Контакты")
    ok = await set_(1, 1, buttons)
    assert ok is True
    assert await get(1, 1) == ["Цена", "Контакты"]


async def test_remove_button():
    get, set_ = _make_quick_reply_helpers()
    await set_(1, 1, ["Цена", "Контакты", "О нас"])
    buttons = await get(1, 1)
    buttons.pop(1)
    await set_(1, 1, buttons)
    assert await get(1, 1) == ["Цена", "О нас"]


async def test_limit_10():
    get, set_ = _make_quick_reply_helpers()
    ok = await set_(1, 1, [f"btn{i}" for i in range(11)])
    assert ok is False
    assert await get(1, 1) == []


async def test_exactly_10_allowed():
    get, set_ = _make_quick_reply_helpers()
    ok = await set_(1, 1, [f"btn{i}" for i in range(10)])
    assert ok is True
    assert len(await get(1, 1)) == 10


async def test_set_empty_clears():
    get, set_ = _make_quick_reply_helpers()
    await set_(1, 1, ["Цена", "Контакты"])
    ok = await set_(1, 1, [])
    assert ok is True
    assert await get(1, 1) == []


async def test_bots_isolated():
    get, set_ = _make_quick_reply_helpers()
    await set_(1, 1, ["bot1_btn"])
    await set_(2, 1, ["bot2_btn"])
    assert await get(1, 1) == ["bot1_btn"]
    assert await get(2, 1) == ["bot2_btn"]


def _make_keyboard(buttons: list[str]):
    """Replicate the keyboard layout logic from STANDARD_BOT_CODE."""
    if not buttons:
        return None  # ReplyKeyboardRemove
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return rows


def test_keyboard_empty():
    assert _make_keyboard([]) is None


def test_keyboard_single():
    rows = _make_keyboard(["Цена"])
    assert rows == [["Цена"]]


def test_keyboard_two_per_row():
    rows = _make_keyboard(["Цена", "Контакты", "О нас", "Отзывы"])
    assert rows == [["Цена", "Контакты"], ["О нас", "Отзывы"]]


def test_keyboard_odd():
    rows = _make_keyboard(["Цена", "Контакты", "О нас"])
    assert rows == [["Цена", "Контакты"], ["О нас"]]
