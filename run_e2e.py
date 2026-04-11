"""Headless end-to-end test for Bot Factory pipeline.

Exercises the full factory path without going through aiogram:
  init_db -> get_or_create_client -> run_pipeline -> save_bot_config -> file write
then re-queries the DB to confirm the BotConfig row exists.

Run with:
    DATABASE_URL=sqlite+aiosqlite:///./test_e2e.db .venv/Scripts/python.exe run_e2e.py

The DATABASE_URL override is required because this test runs without Docker
(no Postgres). SQLAlchemy models only use cross-dialect column types, so
SQLite is a drop-in for a smoke test.
"""
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import select

load_dotenv()  # does NOT override env vars already set (our DATABASE_URL wins)

# Import order matters: db.database reads DATABASE_URL at import time.
from db.database import get_session, init_db  # noqa: E402
from db.models import BotConfig  # noqa: E402
from db.repository import (  # noqa: E402
    get_or_create_client,
    log_tokens,
    save_bot_config,
)
from pipeline import run_pipeline  # noqa: E402

BOTS_DIR = Path("bots")

TEST_TG_ID = 999_999_999
TEST_USERNAME = "e2e_test_user"

ANSWERS = {
    "bot_type": "support",
    "purpose": "Отвечать на частые вопросы клиентов интернет-магазина в FAQ-стиле",
    "audience": "покупатели интернет-магазина электроники",
}

FAKE_BOT_TOKEN = "123456789:AAEaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRr"


async def main() -> int:
    print(f"[e2e] DATABASE_URL = {os.getenv('DATABASE_URL')}")
    print("[e2e] init_db ...")
    await init_db()

    print(f"[e2e] get_or_create_client tg_id={TEST_TG_ID}")
    client = await get_or_create_client(TEST_TG_ID, TEST_USERNAME)
    print(f"[e2e] client.id = {client.id}")

    print(f"[e2e] run_pipeline with answers = {ANSWERS}")
    t0 = time.perf_counter()
    spec = await asyncio.to_thread(run_pipeline, ANSWERS)
    elapsed = time.perf_counter() - t0
    print(f"[e2e] run_pipeline finished in {elapsed:.2f}s")
    print(f"[e2e] spec.requirements.bot_type = {spec.requirements.get('bot_type')}")
    print(f"[e2e] spec.bot_code length = {len(spec.bot_code)} bytes")
    print(f"[e2e] spec.token_logs = {spec.token_logs}")

    bot_dir = BOTS_DIR / str(client.id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    main_path = bot_dir / "main.py"
    main_path.write_text(spec.bot_code, encoding="utf-8")
    print(f"[e2e] wrote {main_path} ({main_path.stat().st_size} bytes)")

    bot_type = spec.requirements.get("bot_type", "other")
    saved_bot = await save_bot_config(
        client_id=client.id,
        bot_type=bot_type,
        bot_name=f"bot_{client.id}",
        system_prompt=spec.system_prompt,
        config={
            "requirements": spec.requirements,
            "architecture": spec.architecture,
        },
        bot_token=FAKE_BOT_TOKEN,
    )
    print(f"[e2e] save_bot_config returned id={saved_bot.id}")

    for entry in spec.token_logs:
        await log_tokens(
            client_id=client.id,
            bot_id=saved_bot.id,
            tokens_in=entry["tokens_in"],
            tokens_out=entry["tokens_out"],
            model=entry["model"],
        )
    print(f"[e2e] log_tokens written ({len(spec.token_logs)} rows)")

    # Verify row persistence by re-reading
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(BotConfig.id == saved_bot.id)
        )
        row = result.scalar_one_or_none()
        assert row is not None, "BotConfig row missing after save"
        print(
            f"[e2e] DB read-back: id={row.id} client_id={row.client_id} "
            f"bot_type={row.bot_type} bot_name={row.bot_name} "
            f"system_prompt_len={len(row.system_prompt)}"
        )

    print("\n=== FINAL USER MESSAGE (what /on_bot_token would send) ===")
    print(
        f"✅ Бот готов!\n\nТип: {bot_type}\nФайл сохранён.\n\n"
        "Для запуска оформите подписку /subscribe"
    )

    print("\n=== bots/{}/main.py — first 20 lines ===".format(client.id))
    lines = spec.bot_code.splitlines()
    for i, line in enumerate(lines[:20], 1):
        print(f"{i:3d}  {line}")
    if len(lines) > 20:
        print(f"... ({len(lines) - 20} more lines)")

    print("\n[e2e] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
