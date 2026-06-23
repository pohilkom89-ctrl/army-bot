"""MAX Messenger bot runtime — shipped into every MAX bot container.

Uses MAX Bot API long-polling (no external library, pure aiohttp).
Token obtained from @metabot in MAX app.
API reference: https://icq.com/botapi/
"""
import asyncio
import json as _json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from loguru import logger
from openai import AsyncOpenAI
from usage_reporter import load_history, report_message, report_subscriber, report_usage

MAX_API_BASE = os.getenv("MAX_API_BASE_URL", "https://api.icq.net/bot/v1")
MAX_TOKEN = os.getenv("MAX_TOKEN")
if not MAX_TOKEN:
    raise RuntimeError("MAX_TOKEN env var is required")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY env var is required")

MODEL = os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")

SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8").strip()
_greeting_raw = Path("/app/greeting.txt").read_text(encoding="utf-8").strip()
GREETING = _greeting_raw if _greeting_raw else "Привет! Я готов помочь. Задайте ваш вопрос."

_blacklist_raw = Path("/app/blacklist.txt").read_text(encoding="utf-8")
BLACKLIST: set[str] = {line.strip() for line in _blacklist_raw.splitlines() if line.strip()}

TRIGGERS: dict[str, str] = _json.loads(Path("/app/triggers.json").read_text(encoding="utf-8"))
RATE_LIMIT_MAX: int = int(Path("/app/rate_limit.txt").read_text(encoding="utf-8").strip() or "0")
WEBHOOK_URL = Path("/app/webhook_url.txt").read_text(encoding="utf-8").strip()

_RATE_WINDOW = 3600
_rate_counters: dict[str, list[float]] = {}

MAX_HISTORY = 20
_history: dict[str, list[dict]] = {}
_history_loaded: set[str] = set()


def _is_rate_limited(user_id: str) -> bool:
    if RATE_LIMIT_MAX <= 0:
        return False
    now = time.time()
    times = [t for t in _rate_counters.get(user_id, []) if now - t < _RATE_WINDOW]
    if len(times) >= RATE_LIMIT_MAX:
        _rate_counters[user_id] = times
        return True
    times.append(now)
    _rate_counters[user_id] = times
    return False


def _get_history(uid: str) -> list[dict]:
    return _history.get(uid, [])


def _append_history(uid: str, role: str, content: str) -> None:
    msgs = _history.get(uid, [])
    msgs.append({"role": role, "content": content})
    if len(msgs) > MAX_HISTORY:
        msgs = msgs[-MAX_HISTORY:]
    _history[uid] = msgs


def _clear_history(uid: str) -> None:
    _history.pop(uid, None)
    _history_loaded.add(uid)


async def _ensure_history_loaded(uid: str, uid_int: int) -> None:
    if uid in _history_loaded:
        return
    _history_loaded.add(uid)
    try:
        msgs = await load_history(uid_int)
        if msgs:
            _history[uid] = msgs[-MAX_HISTORY:]
    except Exception:
        pass


openai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)


async def _send_text(session: aiohttp.ClientSession, chat_id: str, text: str) -> None:
    try:
        async with session.get(
            f"{MAX_API_BASE}/messages/sendText",
            params={"token": MAX_TOKEN, "chatId": chat_id, "text": text},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            await resp.read()
    except Exception:
        logger.exception("max: sendText failed chat_id={}", chat_id)


async def _fire_webhook(user_id: str, text: str) -> None:
    if not WEBHOOK_URL:
        return
    payload = {
        "bot_id": os.getenv("BOT_ID", ""),
        "telegram_id": user_id,
        "username": user_id,
        "first_name": "",
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5))
    except Exception:
        logger.warning("max: webhook POST failed url={}", WEBHOOK_URL)


async def _llm_reply(uid: str, user_text: str) -> str:
    _append_history(uid, "user", user_text)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + _get_history(uid)
    response = await openai_client.chat.completions.create(
        model=MODEL, max_tokens=2048, messages=messages
    )
    asyncio.create_task(report_usage(response.usage, MODEL))
    reply = response.choices[0].message.content or ""
    _append_history(uid, "assistant", reply)
    return reply


async def _handle_event(session: aiohttp.ClientSession, event: dict) -> None:
    if event.get("type") != "newMessage":
        return
    payload = event.get("payload", {})
    chat_id = payload.get("chat", {}).get("chatId", "")
    user_id = payload.get("from", {}).get("userId", "")
    text = payload.get("text", "").strip()

    if not chat_id or not user_id:
        return
    if user_id in BLACKLIST:
        return
    if _is_rate_limited(user_id):
        return

    if text in ("/start", "Начать", "/reset"):
        _clear_history(user_id)
        msg = GREETING if text != "/reset" else "История диалога сброшена. Начнём заново!"
        await _send_text(session, chat_id, msg)
        return

    if not text:
        return

    try:
        uid_int = int(user_id)
    except ValueError:
        uid_int = 0

    await _ensure_history_loaded(user_id, uid_int)
    asyncio.create_task(report_subscriber(uid_int))
    asyncio.create_task(_fire_webhook(user_id, text))

    text_lower = text.lower()
    for keyword, response in TRIGGERS.items():
        if keyword.lower() in text_lower:
            await _send_text(session, chat_id, response)
            return

    asyncio.create_task(report_message(uid_int, user_id, "user", text))
    try:
        reply = await _llm_reply(user_id, text)
        asyncio.create_task(report_message(uid_int, user_id, "bot", reply))
        await _send_text(session, chat_id, reply)
    except Exception:
        logger.exception("max: LLM failed user_id={}", user_id)
        hist = _history.get(user_id, [])
        if hist and hist[-1]["role"] == "user":
            _history[user_id] = hist[:-1]
        await _send_text(session, chat_id, "Произошла ошибка. Попробуйте ещё раз.")


async def main() -> None:
    last_event_id = 0
    logger.info("MAX bot starting api_base={}", MAX_API_BASE)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{MAX_API_BASE}/events/get",
                    params={"token": MAX_TOKEN, "lastEventId": last_event_id, "pollTime": 30},
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json()
                for event in data.get("events", []):
                    eid = event.get("eventId", 0)
                    if eid > last_event_id:
                        last_event_id = eid
                    asyncio.create_task(_handle_event(session, event))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("max: polling error, retry in 5s")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
