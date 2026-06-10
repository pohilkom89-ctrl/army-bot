"""VK Community bot runtime — shipped into every VK bot container.

Architecture mirrors bot_templates.py (Telegram STANDARD_BOT_CODE):
same file-based config, same LLM (OpenRouter), same usage_reporter.
Uses vkbottle for VK Long Poll API.
"""
import asyncio
import base64
import io
import json as _json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from loguru import logger
from openai import AsyncOpenAI
from vkbottle import Bot, Message
from vkbottle.bot import rules
from usage_reporter import load_history, report_message, report_subscriber, report_usage

VK_TOKEN = os.getenv("VK_TOKEN")
if not VK_TOKEN:
    raise RuntimeError("VK_TOKEN env var is required")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY env var is required")

MODEL = os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")
WHISPER_MODEL = os.getenv("OPENROUTER_MODEL_WHISPER", "openai/whisper-1")
VISION_MODEL = os.getenv("OPENROUTER_MODEL_VISION", "google/gemini-flash-1.5")

SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8").strip()
_greeting_raw = Path("/app/greeting.txt").read_text(encoding="utf-8").strip()
GREETING = _greeting_raw if _greeting_raw else "Привет! Я готов помочь. Задайте ваш вопрос."

_blacklist_raw = Path("/app/blacklist.txt").read_text(encoding="utf-8")
BLACKLIST: set[int] = {
    int(line) for line in _blacklist_raw.splitlines() if line.strip().isdigit()
}

WEBHOOK_URL = Path("/app/webhook_url.txt").read_text(encoding="utf-8").strip()
TRIGGERS: dict[str, str] = _json.loads(Path("/app/triggers.json").read_text(encoding="utf-8"))
RATE_LIMIT_MAX: int = int(Path("/app/rate_limit.txt").read_text(encoding="utf-8").strip() or "0")

_RATE_WINDOW = 3600
_rate_counters: dict[int, list[float]] = {}


def _is_rate_limited(user_id: int) -> bool:
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


MAX_HISTORY = 20
_history: dict[int, list[dict]] = {}
_history_loaded: set[int] = set()


def _get_history(uid: int) -> list[dict]:
    return _history.get(uid, [])


def _append_history(uid: int, role: str, content: str) -> None:
    msgs = _history.get(uid, [])
    msgs.append({"role": role, "content": content})
    if len(msgs) > MAX_HISTORY:
        msgs = msgs[-MAX_HISTORY:]
    _history[uid] = msgs


def _clear_history(uid: int) -> None:
    _history.pop(uid, None)
    _history_loaded.add(uid)


async def _ensure_history_loaded(uid: int) -> None:
    if uid in _history_loaded:
        return
    _history_loaded.add(uid)
    if _history.get(uid):
        return
    try:
        msgs = await load_history(uid)
        if msgs:
            _history[uid] = msgs[-MAX_HISTORY:]
    except Exception:
        pass


openai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

bot = Bot(token=VK_TOKEN)


async def _fire_webhook(user_id: int, username: str, text: str) -> None:
    if not WEBHOOK_URL:
        return
    payload = {
        "bot_id": os.getenv("BOT_ID", ""),
        "telegram_id": user_id,
        "username": username,
        "first_name": "",
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception:
        logger.warning("webhook POST failed url={}", WEBHOOK_URL)


async def _llm_reply(uid: int, user_text: str, username: str) -> str:
    """Call LLM and return reply text. Appends to history. Raises on error."""
    _append_history(uid, "user", user_text)
    asyncio.create_task(report_subscriber(uid))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + _get_history(uid)
    response = await openai_client.chat.completions.create(
        model=MODEL, max_tokens=2048, messages=messages
    )
    asyncio.create_task(report_usage(response.usage, MODEL))
    reply = response.choices[0].message.content or ""
    _append_history(uid, "assistant", reply)
    asyncio.create_task(report_message(uid, username, "bot", reply))
    return reply


@bot.on.message(text="Начать")
@bot.on.message(text="/start")
async def cmd_start(message: Message) -> None:
    uid = message.from_id
    _clear_history(uid)
    await message.answer(GREETING)


@bot.on.message(text="/reset")
async def cmd_reset(message: Message) -> None:
    uid = message.from_id
    _clear_history(uid)
    await message.answer("История диалога сброшена. Начнём заново!")


@bot.on.message()
async def on_message(message: Message) -> None:
    uid = message.from_id
    if uid in BLACKLIST:
        return
    if _is_rate_limited(uid):
        return
    await _ensure_history_loaded(uid)

    # Voice message (audio attachment)
    if message.attachments:
        for att in message.attachments:
            if att.type.value == "audio_message":
                await _handle_voice(message, att)
                return
        for att in message.attachments:
            if att.type.value == "photo":
                await _handle_photo(message, att)
                return

    user_text = (message.text or "").strip()
    if not user_text:
        return

    username = str(uid)
    asyncio.create_task(_fire_webhook(uid, username, user_text))

    text_lower = user_text.lower()
    for keyword, response in TRIGGERS.items():
        if keyword.lower() in text_lower:
            await message.answer(response)
            return

    asyncio.create_task(report_message(uid, username, "user", user_text))
    try:
        reply = await _llm_reply(uid, user_text, username)
        await message.answer(reply)
    except Exception:
        logger.exception("on_message: LLM failed user_id={}", uid)
        hist = _history.get(uid, [])
        if hist and hist[-1]["role"] == "user":
            _history[uid] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


async def _handle_voice(message: Message, att) -> None:
    uid = message.from_id
    username = str(uid)
    try:
        audio_url = att.audio_message.link_ogg
        async with aiohttp.ClientSession() as session:
            async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                audio_bytes = await resp.read()
        bio = io.BytesIO(audio_bytes)
        bio.name = "voice.ogg"
        transcript = await openai_client.audio.transcriptions.create(
            model=WHISPER_MODEL, file=bio
        )
        user_text = (transcript.text or "").strip()
    except Exception:
        logger.exception("_handle_voice: transcription failed user_id={}", uid)
        await message.answer("Не удалось распознать голосовое. Напишите текстом.")
        return
    if not user_text:
        await message.answer("Голосовое пустое или не распознано.")
        return
    asyncio.create_task(_fire_webhook(uid, username, f"[🎤] {user_text}"))
    text_lower = user_text.lower()
    for keyword, response in TRIGGERS.items():
        if keyword.lower() in text_lower:
            await message.answer(response)
            return
    asyncio.create_task(report_message(uid, username, "user", f"[🎤] {user_text}"))
    try:
        reply = await _llm_reply(uid, user_text, username)
        await message.answer(reply)
    except Exception:
        logger.exception("_handle_voice: LLM failed user_id={}", uid)
        hist = _history.get(uid, [])
        if hist and hist[-1]["role"] == "user":
            _history[uid] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


async def _handle_photo(message: Message, att) -> None:
    uid = message.from_id
    username = str(uid)
    try:
        sizes = att.photo.sizes
        best = max(sizes, key=lambda s: s.width * s.height)
        async with aiohttp.ClientSession() as session:
            async with session.get(best.url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                img_bytes = await resp.read()
        b64 = base64.b64encode(img_bytes).decode()
    except Exception:
        logger.exception("_handle_photo: download failed user_id={}", uid)
        await message.answer("Не удалось обработать изображение.")
        return
    caption = (message.text or "").strip()
    history_label = f"[Фото] {caption}" if caption else "[Фото]"
    prior_history = _get_history(uid)
    _append_history(uid, "user", history_label)
    asyncio.create_task(report_subscriber(uid))
    asyncio.create_task(report_message(uid, username, "user", history_label))
    image_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": caption if caption else "Что на этом изображении?"},
    ]
    try:
        msgs = (
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + prior_history
            + [{"role": "user", "content": image_content}]
        )
        response = await openai_client.chat.completions.create(
            model=VISION_MODEL, max_tokens=2048, messages=msgs
        )
        asyncio.create_task(report_usage(response.usage, VISION_MODEL))
        reply = response.choices[0].message.content or ""
        _append_history(uid, "assistant", reply)
        asyncio.create_task(report_message(uid, username, "bot", reply))
        await message.answer(reply)
    except Exception:
        logger.exception("_handle_photo: LLM failed user_id={}", uid)
        hist = _history.get(uid, [])
        if hist and hist[-1]["role"] == "user":
            _history[uid] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


if __name__ == "__main__":
    logger.info("VK bot starting")
    bot.run_forever()
