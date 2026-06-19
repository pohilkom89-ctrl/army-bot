"""Tests for Wave 11 — webhook URL per bot."""


async def _make_client(telegram_id: int):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, suffix: int = 1):
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name=f"WebhookBot{suffix}",
        bot_type="support",
        bot_token=f"777{suffix:06d}:FAKE",
        system_prompt="Test",
        config={},
    )


async def test_webhook_url_empty_by_default(fresh_db):
    from db.repository import get_bot_by_id
    client = await _make_client(99001)
    bot = await _make_bot(client.id, 1)
    bot_cfg = await get_bot_by_id(bot.id, client.id)
    url = (bot_cfg.config_json or {}).get("webhook_url", "")
    assert url == ""


async def test_set_webhook_url(fresh_db):
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99002)
    bot = await _make_bot(client.id, 2)
    ok = await update_bot_config(bot.id, client.id, "webhook_url", "https://example.com/hook")
    assert ok is True
    bot_cfg = await get_bot_by_id(bot.id, client.id)
    assert bot_cfg.config_json["webhook_url"] == "https://example.com/hook"


async def test_clear_webhook_url(fresh_db):
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99003)
    bot = await _make_bot(client.id, 3)
    await update_bot_config(bot.id, client.id, "webhook_url", "https://example.com/hook")
    await update_bot_config(bot.id, client.id, "webhook_url", "")
    bot_cfg = await get_bot_by_id(bot.id, client.id)
    assert bot_cfg.config_json["webhook_url"] == ""


async def test_set_webhook_wrong_owner_returns_false(fresh_db):
    from db.repository import update_bot_config
    owner = await _make_client(99004)
    other = await _make_client(99005)
    bot = await _make_bot(owner.id, 4)
    ok = await update_bot_config(bot.id, other.id, "webhook_url", "https://evil.com")
    assert ok is False


async def test_webhook_url_does_not_affect_other_config(fresh_db):
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99006)
    bot = await _make_bot(client.id, 5)
    await update_bot_config(bot.id, client.id, "greeting", "Привет!")
    await update_bot_config(bot.id, client.id, "webhook_url", "https://example.com/hook")
    bot_cfg = await get_bot_by_id(bot.id, client.id)
    assert bot_cfg.config_json["greeting"] == "Привет!"
    assert bot_cfg.config_json["webhook_url"] == "https://example.com/hook"


async def test_bots_have_independent_webhook_urls(fresh_db):
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99007)
    bot_a = await _make_bot(client.id, 6)
    bot_b = await _make_bot(client.id, 7)
    await update_bot_config(bot_a.id, client.id, "webhook_url", "https://a.com/hook")
    cfg_a = await get_bot_by_id(bot_a.id, client.id)
    cfg_b = await get_bot_by_id(bot_b.id, client.id)
    assert cfg_a.config_json["webhook_url"] == "https://a.com/hook"
    assert cfg_b.config_json.get("webhook_url", "") == ""


async def test_crm_type_saved_in_config(fresh_db):
    """crm_type is stored independently from webhook_url."""
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99008)
    bot = await _make_bot(client.id, 8)
    await update_bot_config(bot.id, client.id, "webhook_url", "https://domain.bitrix24.ru/rest/1/hash/")
    await update_bot_config(bot.id, client.id, "crm_type", "bitrix24")
    cfg = await get_bot_by_id(bot.id, client.id)
    assert cfg.config_json["crm_type"] == "bitrix24"
    assert "bitrix24" in cfg.config_json["webhook_url"]


async def test_crm_type_reset_on_webhook_disable(fresh_db):
    """When webhook is cleared, crm_type resets to generic."""
    from db.repository import get_bot_by_id, update_bot_config
    client = await _make_client(99009)
    bot = await _make_bot(client.id, 9)
    await update_bot_config(bot.id, client.id, "webhook_url", "https://domain.bitrix24.ru/rest/1/hash/")
    await update_bot_config(bot.id, client.id, "crm_type", "bitrix24")
    await update_bot_config(bot.id, client.id, "webhook_url", "")
    await update_bot_config(bot.id, client.id, "crm_type", "generic")
    cfg = await get_bot_by_id(bot.id, client.id)
    assert cfg.config_json["crm_type"] == "generic"
    assert cfg.config_json["webhook_url"] == ""


def test_write_bot_crm_type(tmp_path, monkeypatch):
    """write_bot_crm_type writes crm_type.txt in the bot directory."""
    from deployer import write_bot_crm_type, BOTS_DIR
    import deployer
    monkeypatch.setattr(deployer, "BOTS_DIR", tmp_path)
    write_bot_crm_type(42, "bitrix24")
    assert (tmp_path / "42" / "crm_type.txt").read_text() == "bitrix24"


def test_write_bot_crm_type_defaults_to_generic(tmp_path, monkeypatch):
    """Empty string defaults to 'generic'."""
    from deployer import write_bot_crm_type
    import deployer
    monkeypatch.setattr(deployer, "BOTS_DIR", tmp_path)
    write_bot_crm_type(43, "")
    assert (tmp_path / "43" / "crm_type.txt").read_text() == "generic"
