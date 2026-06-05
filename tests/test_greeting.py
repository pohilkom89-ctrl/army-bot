"""Tests for Wave 6 greeting/welcome message."""
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# deployer.write_bot_greeting
# ---------------------------------------------------------------------------

def test_write_bot_greeting_creates_file(tmp_path, monkeypatch):
    from deployer import write_bot_greeting, BOTS_DIR
    monkeypatch.setattr("deployer.BOTS_DIR", tmp_path)
    write_bot_greeting(42, "Добро пожаловать!")
    greeting_file = tmp_path / "42" / "greeting.txt"
    assert greeting_file.exists()
    assert greeting_file.read_text(encoding="utf-8") == "Добро пожаловать!"


def test_write_bot_greeting_empty_string(tmp_path, monkeypatch):
    from deployer import write_bot_greeting
    monkeypatch.setattr("deployer.BOTS_DIR", tmp_path)
    write_bot_greeting(43, "")
    greeting_file = tmp_path / "43" / "greeting.txt"
    assert greeting_file.exists()
    assert greeting_file.read_text(encoding="utf-8") == ""


def test_write_bot_greeting_overwrites(tmp_path, monkeypatch):
    from deployer import write_bot_greeting
    monkeypatch.setattr("deployer.BOTS_DIR", tmp_path)
    write_bot_greeting(44, "Первое")
    write_bot_greeting(44, "Второе")
    greeting_file = tmp_path / "44" / "greeting.txt"
    assert greeting_file.read_text(encoding="utf-8") == "Второе"


def test_prepare_bot_files_creates_greeting_txt(tmp_path, monkeypatch):
    """prepare_bot_files should always create greeting.txt so Dockerfile COPY succeeds."""
    import shutil
    monkeypatch.setattr("deployer.BOTS_DIR", tmp_path)
    # Create a minimal runtime dir with usage_reporter.py
    runtime_dir = tmp_path / "_runtime"
    runtime_dir.mkdir()
    (runtime_dir / "usage_reporter.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("deployer.RUNTIME_DIR", runtime_dir)

    from deployer import prepare_bot_files
    prepare_bot_files("# bot code", 99)

    assert (tmp_path / "99" / "greeting.txt").exists()


# ---------------------------------------------------------------------------
# STANDARD_BOT_CODE uses greeting
# ---------------------------------------------------------------------------

def test_standard_bot_code_reads_greeting():
    """STANDARD_BOT_CODE should reference greeting.txt, not hardcode the greeting."""
    from bot_templates import STANDARD_BOT_CODE
    assert "greeting.txt" in STANDARD_BOT_CODE
    assert "GREETING" in STANDARD_BOT_CODE


def test_standard_bot_code_has_greeting_fallback():
    """STANDARD_BOT_CODE should have a fallback for empty greeting.txt."""
    from bot_templates import STANDARD_BOT_CODE
    # There should be a conditional — empty file falls back to default
    assert "_greeting_raw if _greeting_raw else" in STANDARD_BOT_CODE


# ---------------------------------------------------------------------------
# update_bot_config stores greeting (DB)
# ---------------------------------------------------------------------------

async def test_update_bot_config_stores_greeting(fresh_db):
    from db.repository import get_or_create_client, save_bot_config, update_bot_config, get_bot_by_id

    client = await get_or_create_client(telegram_id=66001, username=None)
    bot = await save_bot_config(
        client_id=client.id,
        bot_name="TestGreetBot",
        bot_type="support",
        bot_token="222000001:FAKE",
        system_prompt="Test",
        config={},
    )

    ok = await update_bot_config(bot.id, client.id, "greeting", "Привет, добро пожаловать!")
    assert ok is True

    updated = await get_bot_by_id(bot.id, client.id)
    assert updated.config_json.get("greeting") == "Привет, добро пожаловать!"
