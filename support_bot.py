"""ArmyBots support bot.

Standalone bot answering customer questions about the ArmyBots service.
Run with: python support_bot.py

Every conversation is mirrored to SUPPORT_LOG_CHAT_ID.
Escalation (user asks for human OR low-confidence answer) goes to
SUPPORT_ESCALATION_CHAT_ID. Owner replies with /reply {user_id} {text}.
"""
import asyncio
import json
import re
import sys
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from loguru import logger
from openai import AsyncOpenAI

from settings import settings

if not settings.support_bot_token:
    logger.error("SUPPORT_BOT_TOKEN not set — support bot cannot start")
    sys.exit(1)

_ESCALATION_RE = re.compile(
    r"хочу.{0,15}(с\s+)?человек"
    r"|позов.{0,10}оператор"
    r"|свяжи.{0,10}человек"
    r"|живой\s+человек"
    r"|поддержка\s+человека"
    r"|оператор",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """Ты — вежливый помощник сервиса ArmyBots. Отвечай кратко и по делу, только на русском.

=== О сервисе ===
ArmyBots — SaaS-платформа для создания Telegram-ботов на базе AI без программирования.
Клиент отвечает на анкету, LLM генерирует систем-промпт, бот разворачивается автоматически.

=== Типы ботов ===
FAQ-бот, интернет-магазин, запись клиентов, HR (подбор персонала), коуч, образовательный,
риэлтор, юрист, психолог, сервисный центр, ивент-менеджер, финансовый советник,
викторина, недвижимость. Можно создать комбинированного бота из нескольких типов.

=== Тарифы ===
Старт — 490₽/мес (4700₽/год): 1 бот, 1 млн токенов/мес
Про    — 949₽/мес (9500₽/год): 3 бота, 5 млн токенов/мес, объединение до 2 ботов
Бизнес — 2990₽/мес (28700₽/год): 10 ботов, безлимит токенов, объединение до 5 ботов

=== Как создать бота ===
1. /start → дать согласие на обработку данных
2. Выбрать тип бота (или несколько типов на Про/Бизнес)
3. Ответить на анкету
4. Получить Telegram Bot Token через @BotFather и вставить его
5. Бот разворачивается автоматически (~1 минута)

=== Управление ботом ===
/mybots — список ваших ботов
/subscribe — управление подпиской
/usage — статистика токенов
/merge_bots — объединить существующих ботов (Про/Бизнес)
/delete_my_data — удалить все данные

=== Токены ===
Токены — единица расхода LLM-модели. Примерно 750 слов = 1000 токенов.
При достижении 20% остатка появляется предупреждение, при 0% бот блокируется до следующего месяца или смены тарифа.

=== Платежи ===
Оплата через ЮKassa (российский сервис). Карты РФ и другие способы. Данные карт не хранятся у нас.

=== Данные и безопасность ===
Данные хранятся на серверах в России (Beget). Соответствует 152-ФЗ.
/my_data — просмотр своих данных. /revoke_consent — отзыв согласия.

=== Инструкция для ответа ===
Если вопрос не относится к ArmyBots или ты не уверен — скажи об этом и предложи обратиться к специалисту.
ВАЖНО: отвечай ТОЛЬКО в формате JSON без markdown-блоков:
{"answer": "текст ответа клиенту", "confident": true}
confident=false если не уверен или вопрос вне FAQ."""

_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
_ai = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url=settings.openrouter_base_url,
)
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я помощник сервиса ArmyBots 🤖\n\n"
        "Задайте любой вопрос о сервисе — о тарифах, ботах, оплате или работе платформы.\n\n"
        "Если понадоблюсь живой специалист, напишите «хочу поговорить с человеком»."
    )


@router.message(Command("reply"))
async def cmd_reply(message: Message) -> None:
    if message.chat.id != settings.support_escalation_chat_id:
        return
    parts = (message.text or "").split(None, 2)
    if len(parts) < 3:
        await message.answer("Использование: /reply {user_id} {текст ответа}")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return
    reply_text = parts[2]
    try:
        await bot.send_message(target_id, f"Ответ от поддержки:\n\n{reply_text}")
        await message.answer(f"✅ Ответ отправлен пользователю {target_id}")
    except Exception:
        logger.exception("support: failed to send reply to user {}", target_id)
        await message.answer("❌ Не удалось отправить ответ")


@router.message(F.text)
async def on_message(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    text = message.text or ""

    if _ESCALATION_RE.search(text):
        await _escalate(message, reason="user_request")
        return

    history = list(_history[user.id])
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    answer = ""
    confident = True
    try:
        resp = await _ai.chat.completions.create(
            model=settings.openrouter_model_agents,
            messages=messages,
            max_tokens=600,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
        data = json.loads(raw)
        answer = str(data.get("answer", raw))
        confident = bool(data.get("confident", True))
    except json.JSONDecodeError:
        answer = raw or "Извините, не смог сформировать ответ. Попробуйте переформулировать вопрос."
        confident = False
    except Exception:
        logger.exception("support: LLM call failed for tg_id={}", user.id)
        answer = "Произошла ошибка. Попробуйте позже или напишите «хочу поговорить с человеком»."
        confident = False

    _history[user.id].append({"role": "user", "content": text})
    _history[user.id].append({"role": "assistant", "content": answer})

    await message.answer(answer)
    await _log_exchange(user, text, answer)

    if not confident:
        await _escalate(message, reason="low_confidence")


async def _log_exchange(user, user_text: str, bot_answer: str) -> None:
    if not settings.support_log_chat_id:
        return
    name = f"@{user.username}" if user.username else f"id:{user.id}"
    log = f"👤 {name} ({user.id})\n➤ {user_text}\n🤖 {bot_answer}"
    try:
        await bot.send_message(settings.support_log_chat_id, log[:4096])
    except Exception:
        logger.exception("support: failed to log exchange")


async def _escalate(message: Message, reason: str) -> None:
    user = message.from_user
    if user is None:
        return
    name = f"@{user.username}" if user.username else f"id:{user.id}"
    reason_label = (
        "пользователь запросил живого оператора"
        if reason == "user_request"
        else "бот не уверен в ответе"
    )
    escalation = (
        f"🆘 Эскалация — {reason_label}\n"
        f"Пользователь: {name} (ID: {user.id})\n"
        f"Сообщение: {message.text or ''}\n\n"
        f"Ответить клиенту: /reply {user.id} ваш_ответ"
    )
    try:
        if settings.support_escalation_chat_id:
            await bot.send_message(settings.support_escalation_chat_id, escalation)
        await message.answer(
            "Ваш вопрос передан специалисту. Ожидайте ответа — обычно в течение рабочего дня."
        )
    except Exception:
        logger.exception("support: failed to escalate")
        await message.answer("Не удалось передать специалисту. Попробуйте позже.")


bot = Bot(token=settings.support_bot_token)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


if __name__ == "__main__":
    logger.info("support bot: starting polling")
    asyncio.run(dp.start_polling(bot))
