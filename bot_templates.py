"""Pre-built bot templates for instant deployment.

Each template ships with a high-quality system prompt and deploys in seconds
— no pipeline run required. The deployed bot uses STANDARD_BOT_CODE which
reads its system_prompt from /app/system_prompt.txt at runtime, so owners
can still edit the prompt via /mybots → Редактировать.
"""

from typing import TypedDict


class BotTemplate(TypedDict):
    name: str          # display name shown in the list
    emoji: str         # single emoji for the button
    bot_type: str      # matches QUESTIONNAIRES key
    description: str   # one-liner for the list view
    preview: str       # multi-line description shown on template detail screen
    system_prompt: str # injected into /app/system_prompt.txt


TEMPLATES: dict[str, BotTemplate] = {
    "shop": {
        "name": "Интернет-магазин",
        "emoji": "🛍",
        "bot_type": "seller",
        "description": "Консультант по товарам, помогает выбрать и купить",
        "preview": (
            "🛍 Бот-консультант для интернет-магазина\n\n"
            "• Отвечает на вопросы о товарах\n"
            "• Помогает подобрать нужный вариант\n"
            "• Работает с возражениями\n"
            "• Объясняет доставку и оплату\n\n"
            "Идеально для: интернет-магазины, маркетплейсы, розничные магазины"
        ),
        "system_prompt": (
            "Ты — вежливый и компетентный консультант интернет-магазина. "
            "Твоя цель — помочь покупателю выбрать подходящий товар, ответить на вопросы "
            "о характеристиках, доставке, оплате и возврате. "
            "Ты хорошо знаешь ассортимент магазина и умеешь работать с возражениями. "
            "Общайся дружелюбно, кратко и по делу. "
            "Если вопрос выходит за рамки твоих знаний — предложи обратиться к менеджеру."
        ),
    },
    "faq": {
        "name": "FAQ / Поддержка",
        "emoji": "🎓",
        "bot_type": "support",
        "description": "Отвечает на частые вопросы о компании или услуге",
        "preview": (
            "🎓 Бот технической поддержки / FAQ\n\n"
            "• Отвечает на типовые вопросы клиентов\n"
            "• Объясняет как пользоваться продуктом\n"
            "• Помогает решить распространённые проблемы\n"
            "• Перенаправляет сложные вопросы к специалисту\n\n"
            "Идеально для: SaaS, сервисные компании, онлайн-курсы"
        ),
        "system_prompt": (
            "Ты — специалист службы поддержки. "
            "Отвечай на вопросы клиентов чётко, полно и понятно. "
            "Если вопрос типовой — дай развёрнутый ответ. "
            "Если нужна дополнительная информация от клиента — вежливо уточни. "
            "Если вопрос требует участия человека-специалиста — сообщи об этом и "
            "попроси клиента написать на email поддержки или позвонить. "
            "Всегда оставайся дружелюбным и терпеливым."
        ),
    },
    "fitness": {
        "name": "Фитнес-коуч",
        "emoji": "💪",
        "bot_type": "coach",
        "description": "Персональные советы по тренировкам и питанию",
        "preview": (
            "💪 Персональный фитнес-коуч\n\n"
            "• Составляет программы тренировок\n"
            "• Даёт советы по питанию\n"
            "• Объясняет технику упражнений\n"
            "• Помогает сохранить мотивацию\n\n"
            "Идеально для: фитнес-клубы, тренеры, wellness-приложения"
        ),
        "system_prompt": (
            "Ты — опытный персональный фитнес-тренер и нутрициолог. "
            "Помогаешь людям достигать спортивных целей: похудение, набор массы, "
            "улучшение выносливости, здоровый образ жизни. "
            "Составляешь планы тренировок и рационы питания под конкретного человека. "
            "Объясняешь технику упражнений, рассказываешь о пользе и рисках. "
            "Поддерживаешь мотивацию, отвечаешь на вопросы о спортивном питании. "
            "Всегда рекомендуешь проконсультироваться с врачом при наличии заболеваний."
        ),
    },
    "tutor": {
        "name": "Репетитор",
        "emoji": "📚",
        "bot_type": "edu",
        "description": "Объясняет темы, проверяет знания, создаёт тесты",
        "preview": (
            "📚 Репетитор / Образовательный бот\n\n"
            "• Объясняет темы простым языком\n"
            "• Отвечает на вопросы по учёбе\n"
            "• Создаёт тесты и задания\n"
            "• Проверяет ответы и даёт обратную связь\n\n"
            "Идеально для: онлайн-школы, репетиторы, EdTech проекты"
        ),
        "system_prompt": (
            "Ты — терпеливый и знающий репетитор. "
            "Умеешь объяснять сложные темы простым и понятным языком. "
            "Если ученик не понимает — объясняешь по-другому, приводишь примеры, "
            "использую аналогии из жизни. "
            "Можешь создавать тесты и задания по любой теме, проверять ответы "
            "и давать развёрнутую обратную связь. "
            "Поощряешь любопытство и задаёшь наводящие вопросы вместо готовых ответов, "
            "когда это помогает лучше усвоить материал."
        ),
    },
    "booking": {
        "name": "Запись на услуги",
        "emoji": "📅",
        "bot_type": "service_orders",
        "description": "Принимает заявки и отвечает на вопросы об услугах",
        "preview": (
            "📅 Бот для записи на услуги\n\n"
            "• Рассказывает об услугах и ценах\n"
            "• Принимает заявки на запись\n"
            "• Отвечает на вопросы о процессе\n"
            "• Информирует о подготовке\n\n"
            "Идеально для: салоны красоты, медицинские клиники, мастера"
        ),
        "system_prompt": (
            "Ты — администратор, который помогает клиентам записаться на услуги. "
            "Вежливо рассказываешь об услугах, ценах и времени. "
            "Собираешь информацию для записи: имя, контакт, желаемую дату и время. "
            "Отвечаешь на вопросы о подготовке к процедурам и что ожидать. "
            "Подтверждаешь запись и объясняешь что будет дальше. "
            "Всегда уточняй детали, которые помогут подготовиться к визиту."
        ),
    },
    "hr": {
        "name": "HR-помощник",
        "emoji": "👔",
        "bot_type": "hr",
        "description": "Отвечает на вопросы о вакансиях и компании",
        "preview": (
            "👔 HR-бот для работодателей\n\n"
            "• Рассказывает о вакансиях и требованиях\n"
            "• Отвечает на вопросы о компании и культуре\n"
            "• Помогает кандидатам подготовиться к интервью\n"
            "• Собирает первичную информацию о соискателях\n\n"
            "Идеально для: компании любого размера, HR-агентства"
        ),
        "system_prompt": (
            "Ты — HR-специалист компании. "
            "Рассказываешь потенциальным кандидатам о вакансиях, требованиях и условиях работы. "
            "Объясняешь корпоративную культуру, ценности и преимущества работы в компании. "
            "Отвечаешь на вопросы о процессе найма, собеседованиях и испытательном сроке. "
            "Помогаешь кандидатам подготовиться к интервью. "
            "Собираешь базовую информацию о соискателях: имя, опыт, контакт. "
            "Всегда дружелюбен и профессионален."
        ),
    },
}


STANDARD_BOT_CODE = '''\
import asyncio
import base64
import io
import os
from datetime import datetime, timezone
from pathlib import Path

import json as _json
import time

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from loguru import logger
from openai import AsyncOpenAI
from usage_reporter import load_history, report_message, report_subscriber, report_usage

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

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
QUICK_REPLIES: list[str] = _json.loads(Path("/app/quick_replies.json").read_text(encoding="utf-8"))


def _make_reply_keyboard() -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if not QUICK_REPLIES:
        return ReplyKeyboardRemove()
    rows = [QUICK_REPLIES[i:i + 2] for i in range(0, len(QUICK_REPLIES), 2)]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=btn) for btn in row] for row in rows],
        resize_keyboard=True,
        persistent=True,
    )
_RATE_WINDOW = 3600  # 1 hour sliding window
_rate_counters: dict[int, list[float]] = {}


def _is_rate_limited(telegram_id: int) -> bool:
    if RATE_LIMIT_MAX <= 0:
        return False
    now = time.time()
    times = [t for t in _rate_counters.get(telegram_id, []) if now - t < _RATE_WINDOW]
    if len(times) >= RATE_LIMIT_MAX:
        _rate_counters[telegram_id] = times
        return True
    times.append(now)
    _rate_counters[telegram_id] = times
    return False

MAX_HISTORY = 20  # messages per user (10 exchanges)
_history: dict[int, list[dict]] = {}
_history_loaded: set[int] = set()  # users whose history was fetched from DB this session


async def _ensure_history_loaded(telegram_id: int) -> None:
    """Lazy-load conversation history from factory DB on first message per user.

    Skipped if already loaded this session (in-memory flag). On /start and
    /reset the user is pre-added to _history_loaded so old history is not
    restored when the user explicitly wants a fresh start.
    """
    if telegram_id in _history_loaded:
        return
    _history_loaded.add(telegram_id)
    if _history.get(telegram_id):
        return
    try:
        msgs = await load_history(telegram_id)
        if msgs:
            _history[telegram_id] = msgs[-MAX_HISTORY:]
    except Exception:
        pass


def _get_history(telegram_id: int) -> list[dict]:
    return _history.get(telegram_id, [])


def _append_history(telegram_id: int, role: str, content: str) -> None:
    msgs = _history.get(telegram_id, [])
    msgs.append({"role": role, "content": content})
    if len(msgs) > MAX_HISTORY:
        msgs = msgs[-MAX_HISTORY:]
    _history[telegram_id] = msgs


def _clear_history(telegram_id: int) -> None:
    _history.pop(telegram_id, None)


openai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)
dp = Dispatcher()


async def _fire_webhook(message: Message) -> None:
    if not WEBHOOK_URL:
        return
    user = message.from_user
    payload = {
        "bot_id": os.getenv("BOT_ID", ""),
        "telegram_id": user.id if user else None,
        "username": user.username if user else None,
        "first_name": user.first_name if user else None,
        "text": message.text or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5))
    except Exception:
        logger.warning("webhook POST failed url={}", WEBHOOK_URL)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    asyncio.create_task(report_subscriber(message.from_user.id))
    _clear_history(message.from_user.id)
    _history_loaded.add(message.from_user.id)
    await message.answer(GREETING, reply_markup=_make_reply_keyboard())


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    _clear_history(message.from_user.id)
    _history_loaded.add(message.from_user.id)
    await message.answer("История диалога сброшена. Начнём заново!")


@dp.message(F.photo)
async def on_photo(message: Message) -> None:
    if message.from_user and message.from_user.id in BLACKLIST:
        return
    if message.from_user and _is_rate_limited(message.from_user.id):
        return
    user = message.from_user
    if user is None:
        return
    await _ensure_history_loaded(user.id)
    try:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        bio = await message.bot.download_file(file_info.file_path)
        b64 = base64.b64encode(bio.read()).decode()
    except Exception:
        logger.exception("on_photo: download failed user_id={}", user.id)
        await message.answer("Не удалось обработать изображение. Попробуйте ещё раз.")
        return
    caption = (message.caption or "").strip()
    history_label = f"[Фото] {caption}" if caption else "[Фото]"
    prior_history = _get_history(user.id)
    _append_history(user.id, "user", history_label)
    asyncio.create_task(_fire_webhook(message))
    asyncio.create_task(report_subscriber(user.id))
    asyncio.create_task(report_message(user.id, user.username, "user", history_label))
    image_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": caption if caption else "Что на этом изображении?"},
    ]
    try:
        messages = (
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + prior_history
            + [{"role": "user", "content": image_content}]
        )
        response = await openai_client.chat.completions.create(
            model=VISION_MODEL,
            max_tokens=2048,
            messages=messages,
        )
        asyncio.create_task(report_usage(response.usage, VISION_MODEL))
        reply = response.choices[0].message.content or ""
        _append_history(user.id, "assistant", reply)
        asyncio.create_task(report_message(user.id, user.username, "bot", reply))
        await message.answer(reply)
    except Exception:
        logger.exception("on_photo: LLM failed user_id={}", user.id)
        hist = _history.get(user.id, [])
        if hist and hist[-1]["role"] == "user":
            _history[user.id] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


@dp.message(F.voice)
async def on_voice(message: Message) -> None:
    if message.from_user and message.from_user.id in BLACKLIST:
        return
    if message.from_user and _is_rate_limited(message.from_user.id):
        return
    user = message.from_user
    if user is None:
        return
    await _ensure_history_loaded(user.id)
    try:
        file_info = await message.bot.get_file(message.voice.file_id)
        bio = await message.bot.download_file(file_info.file_path)
        bio.name = "voice.ogg"
        transcript = await openai_client.audio.transcriptions.create(
            model=WHISPER_MODEL, file=bio
        )
        user_text = (transcript.text or "").strip()
    except Exception:
        logger.exception("on_voice: transcription failed user_id={}", user.id)
        await message.answer("Не удалось распознать голосовое сообщение. Напишите текстом.")
        return
    if not user_text:
        await message.answer("Голосовое сообщение пустое или не распознано.")
        return
    asyncio.create_task(_fire_webhook(message))
    text_lower = user_text.lower()
    for keyword, response in TRIGGERS.items():
        if keyword.lower() in text_lower:
            await message.answer(response)
            return
    asyncio.create_task(report_message(user.id, user.username, "user", f"[🎤] {user_text}"))
    _append_history(user.id, "user", user_text)
    try:
        asyncio.create_task(report_subscriber(user.id))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + _get_history(user.id)
        response = await openai_client.chat.completions.create(
            model=MODEL,
            max_tokens=2048,
            messages=messages,
        )
        asyncio.create_task(report_usage(response.usage, MODEL))
        reply = response.choices[0].message.content or ""
        _append_history(user.id, "assistant", reply)
        asyncio.create_task(report_message(user.id, user.username, "bot", reply))
        await message.answer(reply)
    except Exception:
        logger.exception("on_voice: LLM failed user_id={}", user.id)
        hist = _history.get(user.id, [])
        if hist and hist[-1]["role"] == "user":
            _history[user.id] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


@dp.message()
async def on_message(message: Message) -> None:
    if message.from_user and message.from_user.id in BLACKLIST:
        return
    if message.from_user and _is_rate_limited(message.from_user.id):
        return
    user = message.from_user
    if user is None:
        return
    await _ensure_history_loaded(user.id)
    asyncio.create_task(_fire_webhook(message))
    text_lower = (message.text or "").lower()
    for keyword, response in TRIGGERS.items():
        if keyword.lower() in text_lower:
            await message.answer(response)
            return
    user_text = message.text or ""
    asyncio.create_task(report_message(user.id, user.username, "user", user_text))
    _append_history(user.id, "user", user_text)
    try:
        asyncio.create_task(report_subscriber(user.id))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + _get_history(user.id)
        response = await openai_client.chat.completions.create(
            model=MODEL,
            max_tokens=2048,
            messages=messages,
        )
        asyncio.create_task(report_usage(response.usage, MODEL))
        reply = response.choices[0].message.content or ""
        _append_history(user.id, "assistant", reply)
        asyncio.create_task(report_message(user.id, user.username, "bot", reply))
        await message.answer(reply)
    except Exception:
        logger.exception("on_message failed for user_id={}", user.id)
        # Roll back the user message on error so history stays consistent
        hist = _history.get(user.id, [])
        if hist and hist[-1]["role"] == "user":
            _history[user.id] = hist[:-1]
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot starting (template bot)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
'''


def get_template(key: str) -> BotTemplate | None:
    return TEMPLATES.get(key)
