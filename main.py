import asyncio
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.token import TokenValidationError, validate_token
from loguru import logger

from billing import create_payment
from config import PLANS
from db.database import get_session, init_db  # noqa: F401
from db.repository import (
    anonymize_user,
    get_chat_history,
    get_client_bots,
    get_or_create_client,
    get_usage_stats,
    log_tokens,
    save_bot_config,
    save_chat_message,
    save_consent,
)
from pipeline import _token_accumulator, run_bot_query, run_pipeline
from templates.bot_questionnaires import QUESTIONNAIRES, is_sensitive_question
from webhook_server import start_webhook_server

BOTS_DIR = Path("bots")


CONSENT_TEXT = """Для создания бота мы обрабатываем ваш Telegram ID и username.
Данные хранятся на серверах в России, третьим лицам не передаются.
Вы можете удалить свои данные командой /delete_my_data

Нажмите Согласен чтобы продолжить."""


class IntakeStates(StatesGroup):
    consent = State()
    ask_type = State()
    answering = State()
    ask_bot_token = State()
    processing = State()


class InlineChatStates(StatesGroup):
    chatting = State()


def _bot_type_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{spec['name']} — {spec['description']}",
                callback_data=f"btype:{key}",
            )
        ]
        for key, spec in QUESTIONNAIRES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_question(q: dict, idx: int, total: int) -> str:
    hint = f"\n💡 {q['hint']}" if q.get("hint") else ""
    return f"Вопрос {idx}/{total}\n\n{q['text']}{hint}"


router = Router()


def _consent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Согласен"),
                KeyboardButton(text="Не согласен"),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    logger.info("intake: /start from tg_id={} username={}", user.id, user.username)
    await state.clear()
    await get_or_create_client(user.id, user.username)
    await state.set_state(IntakeStates.consent)
    await message.answer(CONSENT_TEXT, reply_markup=_consent_keyboard())


@router.message(IntakeStates.consent, F.text == "Согласен")
async def on_consent_yes(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    try:
        await save_consent(user.id, CONSENT_TEXT)
    except Exception:
        logger.exception("intake: failed to save consent for tg_id={}", user.id)
        await message.answer(
            "Не удалось сохранить согласие. Попробуйте /start ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    logger.info("intake: consent saved for tg_id={}", user.id)
    await state.set_state(IntakeStates.ask_type)
    await message.answer(
        "Выберите тип бота:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Что именно вам нужно?",
        reply_markup=_bot_type_keyboard(),
    )


@router.message(IntakeStates.consent, F.text == "Не согласен")
async def on_consent_no(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Без согласия продолжить невозможно. /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(IntakeStates.ask_type, F.data.startswith("btype:"))
async def on_bot_type_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").split(":", 1)[1]
    if key not in QUESTIONNAIRES:
        await callback.answer("Неизвестный тип", show_alert=True)
        return

    spec = QUESTIONNAIRES[key]
    questions = spec["questions"]
    await state.update_data(
        bot_type=key,
        questionnaire_type=key,
        answers={},
        current_q=0,
        total_q=len(questions),
    )
    await state.set_state(IntakeStates.answering)

    if callback.message is not None:
        await callback.message.answer(
            f"Отлично, собираем «{spec['name']}». "
            f"Задам {len(questions)} вопросов — отвечайте коротко и по делу."
        )
        first_q = questions[0]
        await callback.message.answer(_format_question(first_q, 1, len(questions)))
    await callback.answer()


@router.message(IntakeStates.answering)
async def on_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_type = data.get("bot_type")
    questions = QUESTIONNAIRES[bot_type]["questions"]
    idx = data.get("current_q", 0)

    current_q = questions[idx]
    answer_text = (message.text or "").strip()
    answers = dict(data.get("answers") or {})
    answers[str(current_q["id"])] = {
        "question": current_q["text"],
        "answer": answer_text,
        "sensitive": is_sensitive_question(current_q["text"]),
    }

    next_idx = idx + 1
    await state.update_data(answers=answers, current_q=next_idx)

    if next_idx < len(questions):
        next_q = questions[next_idx]
        await message.answer(
            _format_question(next_q, next_idx + 1, len(questions))
        )
        return

    await state.set_state(IntakeStates.ask_bot_token)
    await message.answer(
        "Отлично, все вопросы собраны!\n\n"
        "Теперь создайте бота у @BotFather командой /newbot, "
        "получите токен и отправьте его сюда."
    )


@router.message(IntakeStates.ask_bot_token)
async def on_bot_token(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    bot_token = (message.text or "").strip()
    try:
        validate_token(bot_token)
    except TokenValidationError:
        await message.answer(
            "Это не похоже на токен бота. "
            "Проверьте формат (цифры:буквы) и отправьте ещё раз."
        )
        return

    data = await state.get_data()
    bot_type = data.get("bot_type")
    raw_answers: dict = data.get("answers") or {}

    # Defense-in-depth: strip sensitive answer *values* (API tokens, keys)
    # before they reach the LLM. Question text is preserved so the analyst
    # can mark the secret as "provided" via a placeholder flag.
    llm_answers = {}
    sensitive_count = 0
    for qid, entry in raw_answers.items():
        if entry.get("sensitive") and entry.get("answer"):
            sensitive_count += 1
            llm_answers[qid] = {
                "question": entry["question"],
                "answer": "<user provided secret, redacted>",
            }
        else:
            llm_answers[qid] = {
                "question": entry["question"],
                "answer": entry.get("answer", ""),
            }

    pipeline_input = {
        "bot_type": bot_type,
        "questionnaire_type": bot_type,
        "answers": llm_answers,
    }

    await state.set_state(IntakeStates.processing)
    await message.answer("Агенты приступили к работе, ожидайте ~60 секунд...")

    logger.info(
        "intake: pipeline launched for tg_id={} bot_type={} q_count={} sensitive={}",
        user.id,
        bot_type,
        len(llm_answers),
        sensitive_count,
    )
    try:
        spec = await asyncio.to_thread(run_pipeline, pipeline_input)

        client = await get_or_create_client(user.id, user.username)
        bot_dir = BOTS_DIR / str(client.id)
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "main.py").write_text(spec.bot_code, encoding="utf-8")

        resolved_type = spec.requirements.get("bot_type", bot_type or "other")
        saved_bot = await save_bot_config(
            client_id=client.id,
            bot_type=resolved_type,
            bot_name=f"bot_{client.id}",
            system_prompt=spec.system_prompt,
            config={
                "requirements": spec.requirements,
                "architecture": spec.architecture,
                # Raw questionnaire answers with unredacted secrets.
                # Stored only in DB, never passed to LLM.
                "questionnaire_answers": raw_answers,
            },
            bot_token=bot_token,
        )
        for entry in spec.token_logs:
            await log_tokens(
                client_id=client.id,
                bot_id=saved_bot.id,
                tokens_in=entry["tokens_in"],
                tokens_out=entry["tokens_out"],
                model=entry["model"],
            )
    except Exception:
        logger.exception("intake: pipeline failed for tg_id={}", user.id)
        await message.answer("Что-то пошло не так, попробуйте ещё раз /start")
        await state.clear()
        return

    logger.info(
        "intake: pipeline ok for client_id={} (code_len={} bytes)",
        client.id,
        len(spec.bot_code),
    )
    post_create_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Начать чат", callback_data="post_create:chat"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Управление", callback_data="post_create:mybots"
                )
            ],
        ]
    )
    await message.answer(
        f"✅ Бот готов!\n\nТип: {resolved_type}\nФайл сохранён.\n\n"
        "Для запуска оформите подписку /subscribe",
        reply_markup=post_create_kb,
    )
    await state.clear()


_RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _format_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _format_ru_date(dt) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month - 1]} {dt.year}"


def _progress_bar(left: int, limit: int, width: int = 10) -> tuple[str, int]:
    if limit <= 0:
        return "░" * width, 0
    ratio = max(0.0, min(1.0, left / limit))
    filled = round(width * ratio)
    pct = round(100 * ratio)
    return "█" * filled + "░" * (width - filled), pct


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    client = await get_or_create_client(user.id, user.username)
    stats = await get_usage_stats(client.id)

    used = stats["tokens_used"]
    limit = stats["tokens_limit"]
    cost = stats["cost_usd_total"]
    reset_at = stats["reset_at"]
    tier = stats["tier"]

    reset_str = _format_ru_date(reset_at) if reset_at is not None else "—"
    tier_label = PLANS[tier]["name"] if tier in PLANS else "нет"

    if tier is None:
        await message.answer(
            "📊 Использование токенов:\n\n"
            f"Использовано: {_format_num(used)}\n"
            f"Потрачено: ${cost:.2f}\n\n"
            "У вас нет активной подписки → /subscribe"
        )
        return

    if limit is None:
        await message.answer(
            "📊 Использование токенов:\n\n"
            f"Использовано: {_format_num(used)}\n"
            "Осталось: ∞\n\n"
            f"Сброс: {reset_str}\n"
            f"Потрачено: ${cost:.2f}\n\n"
            f"Тариф: {tier_label} (безлимит)"
        )
        return

    tokens_left = max(0, limit - used)
    bar, pct = _progress_bar(tokens_left, limit)
    await message.answer(
        "📊 Использование токенов:\n\n"
        f"Использовано: {_format_num(used)} / {_format_num(limit)}\n"
        f"Осталось: {_format_num(tokens_left)} ({pct}%)\n"
        f"Прогресс: {bar} {pct}%\n\n"
        f"Сброс: {reset_str}\n"
        f"Потрачено: ${cost:.2f}\n\n"
        f"Тариф: {tier_label} → /subscribe для апгрейда"
    )


_TIER_ORDER = ("starter", "pro", "business")


def _subscribe_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tier in _TIER_ORDER:
        plan = PLANS[tier]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan['name']} — {plan['price_monthly']} ₽/мес",
                    callback_data=f"subscribe:{tier}:monthly",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="─────────────────", callback_data="subscribe:noop"
            )
        ]
    )
    for tier in _TIER_ORDER:
        plan = PLANS[tier]
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{plan['name']} — {_format_num(plan['price_yearly'])}"
                        " ₽/год (скидка 20%)"
                    ),
                    callback_data=f"subscribe:{tier}:yearly",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    await message.answer(
        "Выберите тариф подписки:", reply_markup=_subscribe_keyboard()
    )


@router.callback_query(F.data.startswith("subscribe:"))
async def on_subscribe_choice(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) == 2 and parts[1] == "noop":
        await callback.answer()
        return
    if len(parts) != 3:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    _, tier, cycle = parts
    if tier not in PLANS or cycle not in ("monthly", "yearly"):
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    try:
        client = await get_or_create_client(user.id, user.username)
        payment_url = await asyncio.to_thread(
            create_payment, client.id, tier, cycle
        )
    except Exception:
        logger.exception(
            "billing: failed to create payment for tg_id={} tier={} cycle={}",
            user.id,
            tier,
            cycle,
        )
        await callback.answer(
            "Не удалось создать платёж. Попробуйте позже.", show_alert=True
        )
        return

    logger.info(
        "billing: payment link sent client_id={} tier={} cycle={}",
        client.id,
        tier,
        cycle,
    )
    if callback.message is not None:
        await callback.message.answer(
            f"Оплатите подписку по ссылке:\n{payment_url}"
        )
    await callback.answer()


CHAT_HISTORY_LIMIT = 10
LOW_TOKENS_THRESHOLD = 0.2


async def _enter_chat_session(
    client_id: int, state: FSMContext, answer
) -> None:
    bots = await get_client_bots(client_id)
    active_bots = [b for b in bots if b.is_active]
    if not active_bots:
        await answer(
            "У вас нет активного бота. Создайте его командой /start"
        )
        await state.clear()
        return

    bot_cfg = active_bots[0]
    await state.set_state(InlineChatStates.chatting)
    await state.update_data(
        chat_bot_id=bot_cfg.id, chat_bot_name=bot_cfg.bot_name
    )
    await answer(
        f"Вы в режиме чата с ботом {bot_cfg.bot_name}.\n"
        f"Отправляйте сообщения. /exit чтобы выйти."
    )


@router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    await _enter_chat_session(client.id, state, message.answer)


@router.callback_query(F.data == "post_create:chat")
async def cb_post_create_chat(
    callback: CallbackQuery, state: FSMContext
) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    await _enter_chat_session(client.id, state, callback.message.answer)
    await callback.answer()


@router.callback_query(F.data == "post_create:mybots")
async def cb_post_create_mybots(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer(
            "Управление ботами: /mybots"
        )
    await callback.answer()


@router.message(Command("mybots"))
async def cmd_mybots(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    bots = await get_client_bots(client.id)
    if not bots:
        await message.answer(
            "У вас нет ботов. Создайте бота командой /start"
        )
        return
    lines = ["🤖 Ваши боты:\n"]
    for b in bots:
        status = "✅" if b.is_active else "⏸"
        lines.append(f"{status} {b.bot_name} — {b.bot_type}")
    lines.append("\n/chat — общаться с активным ботом")
    await message.answer("\n".join(lines))


@router.message(Command("exit"), InlineChatStates.chatting)
async def cmd_exit_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Вы вышли из режима чата. /chat чтобы вернуться.")


def _format_tokens_footer(stats: dict) -> str:
    tier = stats.get("tier")
    if tier is None:
        return ""
    limit = stats.get("tokens_limit")
    used = stats.get("tokens_used") or 0
    if limit is None:
        return "\n\n💬 Осталось: ∞ (безлимит)"
    left = max(0, limit - used)
    footer = f"\n\n💬 Осталось: {_format_num(left)} токенов"
    if limit > 0 and left < limit * LOW_TOKENS_THRESHOLD:
        footer += "\n⚠️ Токены заканчиваются — /subscribe для апгрейда"
    return footer


@router.message(InlineChatStates.chatting)
async def on_chat_message(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        return

    data = await state.get_data()
    bot_id = data.get("chat_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer(
            "Сессия потеряна. /chat чтобы начать заново."
        )
        return

    client = await get_or_create_client(user.id, user.username)
    bots = await get_client_bots(client.id)
    bot_cfg = next(
        (b for b in bots if b.id == bot_id and b.is_active), None
    )
    if bot_cfg is None:
        await state.clear()
        await message.answer(
            "Бот больше не активен. /chat чтобы выбрать другой."
        )
        return

    history = await get_chat_history(
        client.id, bot_id, limit=CHAT_HISTORY_LIMIT
    )
    context_lines = []
    for h in history:
        prefix = "Клиент" if h["role"] == "user" else "Бот"
        context_lines.append(f"{prefix}: {h['content']}")
    context_str = "\n".join(context_lines)

    token_logs: list = []
    token_ctx = _token_accumulator.set(token_logs)
    try:
        reply = await asyncio.to_thread(
            run_bot_query, bot_cfg.system_prompt, text, context_str
        )
    except Exception:
        logger.exception(
            "chat: run_bot_query failed client_id={} bot_id={}",
            client.id,
            bot_id,
        )
        await message.answer(
            "Что-то пошло не так. Попробуйте ещё раз или /exit."
        )
        return
    finally:
        _token_accumulator.reset(token_ctx)

    tokens_total = sum(
        e["tokens_in"] + e["tokens_out"] for e in token_logs
    )
    for entry in token_logs:
        await log_tokens(
            client_id=client.id,
            bot_id=bot_id,
            tokens_in=entry["tokens_in"],
            tokens_out=entry["tokens_out"],
            model=entry["model"],
        )

    await save_chat_message(client.id, bot_id, "user", text, 0)
    await save_chat_message(
        client.id, bot_id, "assistant", reply, tokens_total
    )

    stats = await get_usage_stats(client.id)
    footer = _format_tokens_footer(stats)

    await message.answer(reply + footer)


@router.message(Command("delete_my_data"))
async def cmd_delete(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    try:
        await anonymize_user(user.id)
    except Exception:
        logger.exception("intake: anonymize failed for tg_id={}", user.id)
        await message.answer(
            "Не удалось удалить данные. Попробуйте позже.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    logger.info("intake: user anonymized tg_id={}", user.id)
    await state.clear()
    await message.answer(
        "Ваши данные удалены.", reply_markup=ReplyKeyboardRemove()
    )


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

bot = Bot(token=BOT_TOKEN)
storage = RedisStorage.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
dp = Dispatcher(storage=storage)
dp.include_router(router)


async def main():
    await init_db()
    logger.info("База данных инициализирована")
    logger.info("Бот запущен")
    await asyncio.gather(
        dp.start_polling(bot),
        start_webhook_server(),
    )


if __name__ == "__main__":
    asyncio.run(main())
