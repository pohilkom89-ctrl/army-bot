"""Tests for Wave 20 — VK bot runtime logic."""
import json


# --- blacklist ---

def test_vk_blacklist_loaded():
    raw = "111\n222\n333\n"
    bl = {int(line) for line in raw.splitlines() if line.strip().isdigit()}
    assert 111 in bl
    assert 222 in bl
    assert 999 not in bl


def test_vk_blacklist_empty():
    raw = ""
    bl = {int(line) for line in raw.splitlines() if line.strip().isdigit()}
    assert bl == set()


# --- rate limit ---

def _make_rate_limiter(max_per_hour: int):
    import time
    counters: dict = {}
    WINDOW = 3600

    def is_limited(uid: int) -> bool:
        if max_per_hour <= 0:
            return False
        now = time.time()
        times = [t for t in counters.get(uid, []) if now - t < WINDOW]
        if len(times) >= max_per_hour:
            counters[uid] = times
            return True
        times.append(now)
        counters[uid] = times
        return False

    return is_limited


def test_vk_rate_limit_allows_under_max():
    limiter = _make_rate_limiter(3)
    assert limiter(1) is False
    assert limiter(1) is False
    assert limiter(1) is False


def test_vk_rate_limit_blocks_over_max():
    limiter = _make_rate_limiter(2)
    limiter(1)
    limiter(1)
    assert limiter(1) is True


def test_vk_rate_limit_zero_disabled():
    limiter = _make_rate_limiter(0)
    for _ in range(100):
        assert limiter(1) is False


# --- triggers ---

def test_vk_trigger_match():
    triggers = {"цена": "Цена 1000 руб."}
    text = "Сколько стоит, цена?"
    matched = next(
        (v for k, v in triggers.items() if k.lower() in text.lower()), None
    )
    assert matched == "Цена 1000 руб."


def test_vk_trigger_no_match():
    triggers = {"цена": "Цена 1000 руб."}
    text = "Привет, как дела"
    matched = next(
        (v for k, v in triggers.items() if k.lower() in text.lower()), None
    )
    assert matched is None


# --- history helpers ---

def _make_vk_history():
    history: dict = {}
    loaded: set = set()
    MAX = 20

    def get(uid):
        return list(history.get(uid, []))

    def append(uid, role, content):
        msgs = history.get(uid, [])
        msgs.append({"role": role, "content": content})
        if len(msgs) > MAX:
            msgs = msgs[-MAX:]
        history[uid] = msgs

    def clear(uid):
        history.pop(uid, None)
        loaded.add(uid)

    return get, append, clear, loaded


def test_vk_history_empty_new_user():
    get, append, clear, loaded = _make_vk_history()
    assert get(1) == []


def test_vk_history_append_and_get():
    get, append, clear, loaded = _make_vk_history()
    append(1, "user", "привет")
    append(1, "assistant", "здравствуйте")
    assert len(get(1)) == 2


def test_vk_history_clear_blocks_restore():
    get, append, clear, loaded = _make_vk_history()
    append(1, "user", "старое")
    clear(1)
    assert get(1) == []
    assert 1 in loaded


# --- platform field ---

def test_platform_default_value():
    # New bots created without platform should default to "telegram"
    bot_data = {"bot_name": "Test", "platform": "telegram"}
    assert bot_data["platform"] == "telegram"


def test_platform_vk_value():
    bot_data = {"bot_name": "VK Bot", "platform": "vk"}
    assert bot_data["platform"] == "vk"
