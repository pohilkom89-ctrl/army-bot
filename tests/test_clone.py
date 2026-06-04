"""Tests for bot cloning: clone_bot_config repository function."""


async def _make_client(telegram_id: int = 500):
    from db.repository import get_or_create_client
    return await get_or_create_client(telegram_id=telegram_id, username=None)


async def _make_bot(client_id: int, token: str = "tok_src") -> object:
    from db.repository import save_bot_config
    return await save_bot_config(
        client_id=client_id,
        bot_name="OriginalBot",
        bot_type="faq",
        bot_token=token,
        system_prompt="You are a helpful assistant.",
        config={"model_strategy": "auto", "architecture": {"handlers": []}},
    )


async def test_clone_creates_new_row(fresh_db):
    """clone_bot_config creates a distinct BotConfig row."""
    from db.repository import clone_bot_config, get_bot_by_id

    client = await _make_client()
    original = await _make_bot(client.id)

    clone = await clone_bot_config(original.id, client.id, "tok_clone")

    assert clone.id != original.id
    assert clone.bot_token == "tok_clone"
    assert clone.client_id == client.id


async def test_clone_copies_settings(fresh_db):
    """Clone inherits system_prompt, bot_type, config_json from source."""
    from db.repository import clone_bot_config

    client = await _make_client(telegram_id=501)
    original = await _make_bot(client.id, token="tok_src2")

    clone = await clone_bot_config(original.id, client.id, "tok_clone2")

    assert clone.system_prompt == original.system_prompt
    assert clone.bot_type == original.bot_type
    assert clone.config_json == original.config_json


async def test_clone_name_has_suffix(fresh_db):
    """Cloned bot name gets ' (копия)' appended."""
    from db.repository import clone_bot_config

    client = await _make_client(telegram_id=502)
    original = await _make_bot(client.id, token="tok_src3")

    clone = await clone_bot_config(original.id, client.id, "tok_clone3")

    assert clone.bot_name == "OriginalBot (копия)"


async def test_clone_wrong_owner_raises(fresh_db):
    """clone_bot_config raises ValueError if owner_client_id does not match."""
    from db.repository import clone_bot_config, get_or_create_client
    import pytest

    owner = await _make_client(telegram_id=503)
    other = await get_or_create_client(telegram_id=504, username=None)
    original = await _make_bot(owner.id, token="tok_src4")

    with pytest.raises(ValueError):
        await clone_bot_config(original.id, other.id, "tok_clone4")
