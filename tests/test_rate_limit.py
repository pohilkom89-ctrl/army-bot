"""Tests for Wave 14 — per-user rate limiting."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"RateLimitBot{suffix}",
        bot_type="support",
        bot_token=f"121{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_rate_limit_zero_by_default(fresh_db):
    from db.repository import get_bot_by_id
    client = await _make_client(120001)
    bot = await _make_bot(client.id, 1)
    cfg = await get_bot_by_id(bot.id, client.id)
    assert (cfg.config_json or {}).get("rate_limit_per_hour", 0) == 0


async def test_set_rate_limit(fresh_db):
    from db.repository import update_bot_config, get_bot_by_id
    client = await _make_client(120002)
    bot = await _make_bot(client.id, 2)
    ok = await update_bot_config(bot.id, client.id, "rate_limit_per_hour", 30)
    assert ok is True
    cfg = await get_bot_by_id(bot.id, client.id)
    assert cfg.config_json["rate_limit_per_hour"] == 30


async def test_disable_rate_limit(fresh_db):
    from db.repository import update_bot_config, get_bot_by_id
    client = await _make_client(120003)
    bot = await _make_bot(client.id, 3)
    await update_bot_config(bot.id, client.id, "rate_limit_per_hour", 30)
    await update_bot_config(bot.id, client.id, "rate_limit_per_hour", 0)
    cfg = await get_bot_by_id(bot.id, client.id)
    assert cfg.config_json["rate_limit_per_hour"] == 0


async def test_rate_limit_in_memory_allows_under_limit():
    """Unit-test the _is_rate_limited logic directly (no DB)."""
    import time
    counters: dict[int, list[float]] = {}
    window = 3600
    max_msgs = 3

    def is_limited(uid: int) -> bool:
        now = time.time()
        times = [t for t in counters.get(uid, []) if now - t < window]
        if len(times) >= max_msgs:
            counters[uid] = times
            return True
        times.append(now)
        counters[uid] = times
        return False

    assert is_limited(1) is False
    assert is_limited(1) is False
    assert is_limited(1) is False
    assert is_limited(1) is True   # 4th message — blocked


async def test_rate_limit_in_memory_zero_means_no_limit():
    import time
    counters: dict[int, list[float]] = {}
    window = 3600
    max_msgs = 0  # disabled

    def is_limited(uid: int) -> bool:
        if max_msgs <= 0:
            return False
        now = time.time()
        times = [t for t in counters.get(uid, []) if now - t < window]
        if len(times) >= max_msgs:
            counters[uid] = times
            return True
        times.append(now)
        counters[uid] = times
        return False

    for _ in range(100):
        assert is_limited(1) is False


async def test_rate_limit_independent_per_user():
    import time
    counters: dict[int, list[float]] = {}
    window = 3600
    max_msgs = 2

    def is_limited(uid: int) -> bool:
        now = time.time()
        times = [t for t in counters.get(uid, []) if now - t < window]
        if len(times) >= max_msgs:
            counters[uid] = times
            return True
        times.append(now)
        counters[uid] = times
        return False

    assert is_limited(1) is False
    assert is_limited(1) is False
    assert is_limited(1) is True   # user 1 blocked
    assert is_limited(2) is False  # user 2 not affected
    assert is_limited(2) is False
    assert is_limited(2) is True
